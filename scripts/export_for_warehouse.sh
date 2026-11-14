#!/usr/bin/env bash
# Export Wethr SQLite tables to CSV for loading into BigQuery/Snowflake.
# Read-only against the live DB. Skips market_snapshots (too large) unless INCLUDE_MARKET_SNAPSHOTS=1.
# Skips tables whose CSV already exists unless FORCE=1. Retries on "database is locked"
# (the collector writes to the live DB) and keeps going if a table fails.
set -uo pipefail

DB="${DB:-$(dirname "$0")/../data/wethr.db}"
OUT="${OUT:-$HOME/wethr-export}"
RETRIES="${RETRIES:-6}"
mkdir -p "$OUT"

tables=$(sqlite3 -readonly "$DB" \
  "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")

failed=()
for t in $tables; do
  if [[ "$t" == "market_snapshots" && "${INCLUDE_MARKET_SNAPSHOTS:-0}" != "1" ]]; then
    echo "skip  $t (large)"; continue
  fi
  if [[ -f "$OUT/$t.csv" && "${FORCE:-0}" != "1" ]]; then
    echo "skip  $t (already exported)"; continue
  fi
  ok=0
  for ((i = 1; i <= RETRIES; i++)); do
    echo "export $t (attempt $i)"
    if sqlite3 -readonly -header -csv "$DB" ".timeout 60000" "SELECT * FROM \"$t\";" > "$OUT/$t.csv.tmp"; then
      mv "$OUT/$t.csv.tmp" "$OUT/$t.csv"; ok=1; break
    fi
    rm -f "$OUT/$t.csv.tmp"; sleep $((i * 10))
  done
  [[ $ok == 1 ]] || { echo "FAILED $t"; failed+=("$t"); }
done

( cd "$OUT" && wc -l -- *.csv && du -sh . )
if (( ${#failed[@]} )); then echo "FAILED TABLES: ${failed[*]}"; exit 1; fi
echo "DONE: all tables exported"
