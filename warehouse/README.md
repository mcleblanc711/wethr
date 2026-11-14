# Wethr analytics warehouse

Wethr's collector writes everything to one SQLite file (`data/wethr.db`, ~11 GB,
~17M rows across the snapshot tables). This folder takes a snapshot of that file
into Snowflake, models it with dbt, and exposes clean tables to AI and BI tools.

```text
 data/wethr.db (SQLite, live, WAL)
        │  scripts/export_parquet.py
        │    VACUUM INTO snapshot → DuckDB → typed, zstd Parquet (one file per table)
        ▼
 ~/wethr-export/parquet/*.parquet
        │  warehouse/snowflake/load.sh
        │    PUT → internal stage → CREATE TABLE USING TEMPLATE(INFER_SCHEMA) → COPY INTO
        ▼
 WETHR.RAW            (as-loaded; WETHR_LOADER)
        │  warehouse/dbt  (dbt build; WETHR_TRANSFORMER)
        │    staging views → tested marts
        ▼
 WETHR.ANALYTICS      fct_trades · daily_pnl_by_epoch · calibration_by_bracket · market_spread_daily
        │  WETHR_READER (SELECT only)
        ▼
 Claude Dashboards · ChatGPT Data · Claude Code (MCP) · Snowsight / any BI tool
```

| Path | What it is |
|---|---|
| `../scripts/export_parquet.py` | Consistent SQLite snapshot → Parquet, with row-count reconciliation |
| `snowflake/setup.sql` | One-time account setup: warehouse, credit cap, database, roles, service user |
| `snowflake/load.sh` | Upload + (re)create every RAW table from Parquet |
| `snowflake/ai_connectors.sql` | Read-only AI user, managed MCP server, OAuth integration for claude.ai |
| `snowflake/mcp_tools_config.yaml` | Allowlist for the local MCP server used from Claude Code |
| `dbt/` | Staging + mart models with tests |

Design choices worth being able to explain:

- **Snapshot, not live reads.** `VACUUM INTO` copies the DB in one read
  transaction, so the export is consistent while the collector keeps writing.
  (`sqlite3 .backup` restarts every time another process writes.)
- **Parquet over CSV.** Keeps types (timestamps, ints, floats), is ~5–10× smaller,
  and every warehouse loads it natively. CSV export stays as a fallback
  (`scripts/export_for_warehouse.sh`).
- **RAW vs ANALYTICS.** RAW mirrors the source exactly so loads are dumb and
  repeatable; business rules (what a "win" is, epoch bankrolls, calibration
  grain) live in version-controlled, tested dbt models.
- **Least privilege.** Separate roles for loading, transforming and reading. AI
  tools get `WETHR_READER` only, so they can't modify data no matter what SQL
  they generate.
- **Cost guard.** XSMALL warehouse, 60 s auto-suspend, a 10-minute statement
  timeout, and a 5-credit/month resource monitor that suspends the warehouse.
  The monitor only caps warehouse compute; cloud-services and serverless credits
  (e.g. the managed MCP server) are not covered, so check Admin → Cost Management too.

## Setup, step by step

### 0. Export (local, free)

```bash
uv run scripts/export_parquet.py            # ~15–25 min; writes ~/wethr-export/
uv run scripts/export_parquet.py --reuse-snapshot --tables trades   # re-export one table
```

Check the summary table at the end: every `sqlite rows` must equal `parquet rows`.

### 1. Snowflake account (browser)

1. Sign up at signup.snowflake.com: **Standard** edition, **AWS**, a US region.
   The trial is 30 days / $400 of credit; add a card before it ends to keep the account.
2. Snowsight → Projects → Worksheets → `+` → paste `snowflake/setup.sql` → Run All.
3. Note your account identifier: Snowsight → bottom-left account menu → Connect
   a tool → "Account identifier" (format `ORGNAME-ACCOUNTNAME`).

### 2. Key pair for the service user (terminal)

```bash
mkdir -p ~/.snowflake/keys && chmod 700 ~/.snowflake/keys
openssl genrsa 2048 | openssl pkcs8 -topk8 -nocrypt -out ~/.snowflake/keys/wethr_svc_rsa_key.p8
openssl rsa -in ~/.snowflake/keys/wethr_svc_rsa_key.p8 -pubout -out ~/.snowflake/keys/wethr_svc_rsa_key.pub
chmod 600 ~/.snowflake/keys/*
grep -v -- '-----' ~/.snowflake/keys/wethr_svc_rsa_key.pub | tr -d '\n'; echo
```

Paste the printed key into the commented `ALTER USER WETHR_SVC SET RSA_PUBLIC_KEY = '...'`
line of `setup.sql` and run just that line in Snowsight (as USERADMIN or ACCOUNTADMIN).

### 3. Snowflake CLI + load

```bash
uv tool install snowflake-cli
mkdir -p ~/.snowflake && cat >> ~/.snowflake/config.toml <<'EOF'
[connections.wethr]
account = "ORGNAME-ACCOUNTNAME"
user = "WETHR_SVC"
authenticator = "SNOWFLAKE_JWT"
private_key_file = "~/.snowflake/keys/wethr_svc_rsa_key.p8"
role = "WETHR_LOADER"
warehouse = "WETHR_WH"
database = "WETHR"
schema = "RAW"
EOF
chmod 600 ~/.snowflake/config.toml
snow connection test -c wethr

DRY_RUN=1 warehouse/snowflake/load.sh | less    # read the SQL first
warehouse/snowflake/load.sh
```

The final query lists `ROW_COUNT` per table; compare with the export summary.

Then check the timestamp types landed as time-zone-aware:

```bash
snow sql -c wethr -q "DESCRIBE TABLE WETHR.RAW.TRADES" | grep -i _AT
```

`CREATED_AT` / `SETTLED_AT` should be `TIMESTAMP_LTZ` or `TIMESTAMP_TZ`. If they are
`TIMESTAMP_NTZ`, the staging models' `convert_timezone('UTC', ...)` reads them in the
session time zone; `setup.sql` pins the service user to UTC so that is still correct,
but any other login with a different `TIMEZONE` would see shifted dates.

### 4. dbt

```bash
cd warehouse/dbt
cp profiles.example.yml ~/.dbt/profiles.yml     # then fill in the account identifier
uv run --with dbt-snowflake dbt deps
uv run --with dbt-snowflake dbt debug           # connection check
uv run --with dbt-snowflake dbt build           # 9 models + 23 tests
uv run --with dbt-snowflake dbt docs generate && uv run --with dbt-snowflake dbt docs serve   # lineage graph
```

`assert_epoch_pnl_matches_trades` checks the dashboard P/L matches the raw ledger
to the cent, which is the same number `get_stats()` in `collector/src/paper_trader.py` reports.

### 5. AI connectors

Run `snowflake/ai_connectors.sql` in Snowsight (read it first; replace the password).

**Claude Code (this terminal)**, through the open-source Snowflake-Labs MCP server.
Add a read-only connection, then register the server:

```bash
cat >> ~/.snowflake/config.toml <<'EOF'
[connections.wethr_reader]
account = "ORGNAME-ACCOUNTNAME"
user = "WETHR_AI"
password = "<the WETHR_AI password>"
role = "WETHR_READER"
warehouse = "WETHR_WH"
database = "WETHR"
schema = "ANALYTICS"
EOF

chmod 600 ~/.snowflake/config.toml   # it now holds a password; key-pair auth also works here

claude mcp add snowflake --scope user -- uvx snowflake-labs-mcp \
  --service-config-file "$PWD/warehouse/snowflake/mcp_tools_config.yaml" \
  --connection-name wethr_reader
```

**Claude Dashboards / claude.ai.** Settings → Connectors → Snowflake. Use the client
ID/secret printed by `ai_connectors.sql` and the MCP URL
`https://<account_url>/api/v2/databases/WETHR/schemas/ANALYTICS/mcp-servers/WETHR_MCP`.
Sign in as `WETHR_AI`. Ask "how many rows are in fct_trades?" before asking for a dashboard.

**ChatGPT.** The Data agent (`@Data`) is reported to need a Business/Enterprise
plan. On other plans, add the same MCP URL as a custom connector in Developer Mode,
with a second OAuth integration that uses ChatGPT's redirect URI.

## Refreshing

```bash
uv run scripts/export_parquet.py && warehouse/snowflake/load.sh \
  && (cd warehouse/dbt && uv run --with dbt-snowflake dbt build)
```

Each step fully replaces its output. An incremental load (append rows with
`id > max(id)` per table, then `dbt build` with incremental models) is the
natural next project.
