#!/usr/bin/env bash
# Load ~/wethr-export/parquet/*.parquet into WETHR.RAW on Snowflake.
#
# Prereqs: warehouse/snowflake/setup.sql has been run, the Snowflake CLI is
# installed (`uv tool install snowflake-cli`) and a connection named "wethr"
# (or $SNOW_CONNECTION) exists in ~/.snowflake/config.toml. See warehouse/README.md.
#
# Each run replaces every RAW table (one-shot snapshot semantics):
#   1. PUT the Parquet files to the internal stage (no re-compression).
#   2. Per file: CREATE OR REPLACE TABLE ... USING TEMPLATE (INFER_SCHEMA) so the
#      column types come from the Parquet schema, then COPY INTO by column name.
#   3. Print Snowflake row counts next to the local Parquet counts.
#
# DRY_RUN=1 prints the generated SQL instead of running it.
set -euo pipefail

PARQUET_DIR="${PARQUET_DIR:-$HOME/wethr-export/parquet}"
CONN="${SNOW_CONNECTION:-wethr}"
ROLE="${SNOW_ROLE:-WETHR_LOADER}"
STAGE="WETHR.RAW.WETHR_STAGE"
FMT="WETHR.RAW.PARQUET_FMT"

shopt -s nullglob
files=("$PARQUET_DIR"/*.parquet)
(( ${#files[@]} )) || { echo "no Parquet files in $PARQUET_DIR; run scripts/export_parquet.py first" >&2; exit 1; }

sql_file="$(mktemp --suffix=.sql)"
trap 'rm -f "$sql_file"' EXIT

{
  echo "USE ROLE $ROLE;"
  echo "USE WAREHOUSE WETHR_WH;"
  echo "USE SCHEMA WETHR.RAW;"
  echo "PUT 'file://$PARQUET_DIR/*.parquet' @$STAGE AUTO_COMPRESS = FALSE OVERWRITE = TRUE PARALLEL = 8;"
  for f in "${files[@]}"; do
    name="$(basename "$f" .parquet)"
    table="${name^^}"
    cat <<SQL

-- $name
CREATE OR REPLACE TABLE WETHR.RAW.$table
  USING TEMPLATE (
    SELECT ARRAY_AGG(OBJECT_CONSTRUCT('COLUMN_NAME', COLUMN_NAME, 'TYPE', TYPE, 'NULLABLE', NULLABLE))
           WITHIN GROUP (ORDER BY ORDER_ID)
    FROM TABLE(INFER_SCHEMA(
      LOCATION => '@$STAGE/$name.parquet',
      FILE_FORMAT => '$FMT',
      IGNORE_CASE => TRUE)));
COPY INTO WETHR.RAW.$table
  FROM @$STAGE
  FILES = ('$name.parquet')
  FILE_FORMAT = (FORMAT_NAME = '$FMT')
  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
  ON_ERROR = ABORT_STATEMENT;
SQL
  done
  echo
  echo "SELECT TABLE_NAME, ROW_COUNT, ROUND(BYTES / 1e6, 1) AS MB"
  echo "FROM WETHR.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'RAW' ORDER BY TABLE_NAME;"
} > "$sql_file"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  cat "$sql_file"; exit 0
fi

echo "loading ${#files[@]} files from $PARQUET_DIR via connection '$CONN' as $ROLE"
snow sql -c "$CONN" -f "$sql_file"

echo; echo "compare ROW_COUNT above with the parquet rows column in ${PARQUET_DIR%/parquet}/export_parquet.log"
