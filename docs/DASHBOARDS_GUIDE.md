# Wethr → warehouse → AI dashboards

Goal: practise working with bigger data on real warehouse tooling, then compare
how AI tools (Claude, ChatGPT) and classic BI handle the same questions.

**Setup steps live in [`warehouse/README.md`](../warehouse/README.md).** This page
covers why this stack, what else exists, and the exercises to do once data is loaded.

> Pricing and plan details below were checked in Nov 2026 against third-party
> write-ups and vendor docs. Recheck the vendors' pricing pages before relying on them.

## 1. Why Snowflake + dbt

Data size: `data/wethr.db` is ~11 GB, mostly `prediction_snapshots` (~10M rows),
`forecast_snapshots` (~4.5M) and `market_snapshots` (~2.4M).

| | BigQuery | Snowflake |
|---|---|---|
| Mental model | Serverless; pay for bytes each query scans | Start a "virtual warehouse"; pay per second it runs |
| Cost after free | Storage ~$0.02/GB-mo (logical, uncompressed); queries ~$6.25/TB after 1 TB/mo free | XS ≈ 1 credit/hr ≈ $2/hr, per second, 60 s min; storage ~$23/TB-mo compressed. Light use ≈ $5–10/mo |
| Free path | Sandbox: 10 GB storage, tables expire after 60 days, no card | 30-day trial, $400 credit |
| Setup | Low | Medium: roles, warehouses, stages |
| Teaches | Partitioning, clustering, scan-cost awareness | RBAC, compute/storage separation, stages + COPY, resource monitors, Time Travel, zero-copy clones |
| Job posts | ~5% of data-engineering postings | ~10% (about 2× BigQuery), across many industries |

Snowflake shows up in more job posts, and its concepts (roles, compute sizing,
cost control) come up in interviews. **dbt** appears alongside either warehouse in
roughly 35–40% of postings, and turns "I loaded a CSV" into tested, versioned
models. BigQuery is a good second pass: load the same Parquet with `bq load
--source_format=PARQUET` and compare.

### Other options considered

| Option | Verdict |
|---|---|
| **Supabase** | Hosted Postgres for app backends. Free tier is 500 MB (Pro: $25/mo for 8 GB), and as a row store it doesn't teach columnar analytics. Good if Wethr ever needs a web app or API. |
| **MotherDuck** | Cloud DuckDB, 10 GB free plus an official MCP server. Great for cheap analytics, but Claude Dashboards and ChatGPT Data don't list it. |
| **Local DuckDB** | Free, no account. `duckdb` on `~/wethr-export/parquet/*.parquet` is the quickest way to feel columnar speed. |
| **ClickHouse Cloud** | Very fast for time-series snapshot data and supported by both AI tools; more ops concepts to learn. |
| **Databricks Free Edition** | Spark/Delta/notebooks: a lakehouse, heavier than this data needs. |

## 2. AI and BI tools to compare

| Tool | How it connects | Notes |
|---|---|---|
| **Claude Code + MCP** | `snowflake-labs-mcp` via `claude mcp add` | Runs in this terminal; read-only by role and by allowlist |
| **Claude Dashboards** (beta, Oct 2026) | Settings → Connectors → Snowflake (OAuth + managed MCP server) | Click any number to see the query behind it |
| **ChatGPT Data agent** (`@Data`, Sep 2026) | Native connector on Business/Enterprise; custom MCP connector otherwise | Check which ChatGPT plan you have |
| **Snowflake Cortex Analyst** | Built in; needs a semantic model/view | Trial accounts cap Cortex at ~10 credits/day |
| **Snowsight dashboards** | Built in | Free with the account; the baseline to beat |
| **Metabase OSS** | Docker, next to the n8n stack | Classic BI, self-hosted |
| **Evidence.dev** | SQL + Markdown in git | Dashboards as code, nice for a portfolio |
| **Hex** | Hosted notebooks, free tier | SQL + Python + charts |

## 3. Exercises

### A. Same questions, three tools

Ask Claude Dashboards, ChatGPT and Snowsight (hand-written SQL) the same questions.
Record whether each answer was right, the SQL it ran, and the credits used
(`QUERY_HISTORY`, below).

1. Cumulative paper P/L by strategy epoch. The answer key is `ANALYTICS.DAILY_PNL_BY_EPOCH`,
   which a dbt test reconciles with the raw ledger.
2. Win rate and average entry price for settled trades only, by city, with sample sizes.
3. Is the model calibrated? Predicted vs observed hit rate by probability bucket.
4. How has the market spread changed over time per city?

Then grant `WETHR_READER` access to `RAW` (commented lines in `setup.sql`) and ask
again. AI tools on raw snapshot tables tend to make grain mistakes (many-to-many
joins, counting open trades as losses, mixing epochs), and seeing that happen
teaches why the modelling layer exists.

### B. Pruning and the query profile

```sql
-- Same question, with and without a date filter. Compare in Query Profile:
-- "Partitions scanned" vs "Partitions total", and bytes scanned.
SELECT city, AVG(spread) FROM WETHR.RAW.MARKET_SNAPSHOTS GROUP BY city;
SELECT city, AVG(spread) FROM WETHR.RAW.MARKET_SNAPSHOTS
WHERE target_date >= DATEADD(day, -7, CURRENT_DATE) GROUP BY city;

-- How well is the table naturally clustered on target_date? (rows load in capture order)
SELECT SYSTEM$CLUSTERING_INFORMATION('WETHR.RAW.MARKET_SNAPSHOTS', '(target_date)');
SELECT SYSTEM$CLUSTERING_INFORMATION('WETHR.RAW.MARKET_SNAPSHOTS', '(city)');
```

Don't turn on automatic clustering (`ALTER TABLE ... CLUSTER BY`) at this size. It
runs on serverless credits, which the resource monitor doesn't cap.

### C. Cost accounting

```sql
SELECT query_text, warehouse_size, total_elapsed_time / 1000 AS secs,
       bytes_scanned / 1e6 AS mb_scanned, role_name, user_name
FROM TABLE(WETHR.INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => 50))
ORDER BY start_time DESC;

-- Credits per day (ACCOUNT_USAGE lags up to ~3 h)
SELECT DATE(start_time) AS day, SUM(credits_used) AS credits
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE warehouse_name = 'WETHR_WH' GROUP BY 1 ORDER BY 1 DESC;
```

Filter `QUERY_HISTORY` by `user_name = 'WETHR_AI'` to see exactly what the AI tools ran.

### D. Snowflake-only features

- **Zero-copy clone:** `CREATE DATABASE WETHR_DEV CLONE WETHR;` gives an instant,
  free copy to break things in.
- **Time Travel:** `SELECT COUNT(*) FROM WETHR.RAW.TRADES AT(OFFSET => -3600);`
  queries the table as it was an hour ago (1-day retention on Standard).

## 4. Data gotchas (Wethr-specific)

- **Grain** (see `docs/SQL_GUIDE.md` §3): snapshot tables are histories. Join on
  IDs, not just `city` and `target_date`. `calibration_by_bracket` keeps one
  prediction per model version per bracket (the latest).
- **Epochs:** P/L is split by `strategy_version` (`legacy-v0`, `2026-09-recal`). A
  dashboard that ignores epochs mixes old and new strategy results.
- **Open trades** are neither wins nor losses (`fct_trades.status = 'open'`).
- **Time zones:** staging models convert every timestamp to UTC (`*_at_utc`). `target_date`
  is the market's local calendar date, not UTC.
- **JSON-in-text:** `prediction_snapshots.forecast_snapshot_ids_json` is a string;
  use `PARSE_JSON` / `LATERAL FLATTEN` to unpack it.
- `settings` (bankroll values) is loaded into RAW but not exposed to `WETHR_READER`.

## 5. Dashboard prompt (Claude Dashboards)

> Using WETHR.ANALYTICS, build a dashboard for my Polymarket daily-high-temperature
> paper-trading bot: cumulative P/L and bankroll by strategy epoch
> (daily_pnl_by_epoch); win rate and average entry price by city for settled trades
> with sample sizes (fct_trades where is_settled); a calibration chart of
> mean_predicted vs observed_rate per model version, marking small_sample buckets
> (calibration_by_bracket); and median spread over time by city
> (market_spread_daily). Keep it readable on a phone. Show the query behind each
> chart and list any assumptions at the bottom.
