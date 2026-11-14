# Learning SQL with Wethr

This is a hands-on SQL guide built around Wethr's SQLite database. It assumes
you already use SQL through Excel and can recognize a basic `SELECT`, but want
to become comfortable reasoning about data, writing multi-step analysis, and
debugging queries without leaning on spreadsheet mechanics.

The goal is not to memorize syntax. It is to learn a repeatable way to answer a
question:

1. What does one row represent?
2. Which rows do I need?
3. How should those rows be combined?
4. What should the result's grain be?
5. How can I prove the result is not silently duplicated or incomplete?

All examples use SQLite and the live database at `data/wethr.db`. Most of the
core SQL transfers directly to SQL Server, PostgreSQL, DuckDB, and the SQL you
send through Excel. SQLite-specific date and JSON syntax is called out where it
appears.

## 1. Set up a safe practice session

From the repository root:

```bash
sqlite3 -readonly -header -column data/wethr.db
```

Then enter these SQLite shell commands:

```text
.timeout 5000
.tables
.schema trades
.indexes trades
.timer on
```

Exit with `.quit`.

The `-readonly` flag matters: this is the live application database, and the
snapshot tables contain millions of rows. Start exploratory queries with a date
filter and a `LIMIT`. Do not practise `UPDATE`, `DELETE`, or schema changes on
this file. If you later want to learn data modification, make a disposable copy
of the database first.

SQL statements end in a semicolon. Commands beginning with a dot are features
of the `sqlite3` shell, not SQL, so they will not work in Excel's query editor.

## 2. Translate the Excel mental model

| Excel idea | SQL idea | Wethr example |
|---|---|---|
| Workbook table | Database table | `trades` |
| Row | Row/record | One paper trade |
| Column | Column/field | `entry_price` |
| Filter | `WHERE` | `WHERE settled = 1` |
| Sort | `ORDER BY` | `ORDER BY created_at DESC` |
| Remove duplicates | `DISTINCT` | `SELECT DISTINCT city` |
| PivotTable | `GROUP BY` plus aggregates | P&L by city |
| `SUMIFS` / `COUNTIFS` | Conditional aggregation | Settled and winning counts |
| `IF` | `CASE` | Label a trade as win/loss/open |
| `XLOOKUP` | `JOIN` | Attach market data to a prediction |
| Power Query steps | CTEs | Filter, enrich, then summarize |
| Running total | Window function | Cumulative P&L |

The biggest conceptual difference is that a SQL table has no inherent row
order. If order matters, say so with `ORDER BY`.

SQL is also declarative: describe the result you want rather than a cell-by-cell
procedure. A useful reading order is:

```text
FROM/JOIN -> WHERE -> GROUP BY -> HAVING -> SELECT -> ORDER BY -> LIMIT
```

That is closer to SQL's logical processing order than the written order of a
query, and it explains many otherwise surprising errors.

## 3. Know the Wethr data model

Before querying, identify the **grain**: what one row means. Grain is the most
important idea in analytical SQL.

### Core reporting tables

| Table | One row represents | Useful key |
|---|---|---|
| `trades` | One paper trade | `id` |
| `signals` | Latest legacy signal for one city/date/bracket | `city`, `target_date`, `bracket_label` |
| `daily_stats` | One application summary day | `date` |
| `historical_forecasts` | One legacy model forecast for a city/date/lead | `city`, `target_date`, `lead_days`, `model` |
| `historical_observations` | One legacy observed maximum for a city/date | `city`, `target_date` |

### Calibration-grade ledger

| Table | One row represents | Main relationship |
|---|---|---|
| `forecast_snapshots` | One versioned model forecast sighting | IDs are listed in a prediction's JSON array |
| `market_snapshots` | One market bracket observed at one instant | `prediction_snapshots.market_snapshot_id` |
| `prediction_snapshots` | One model prediction against one market snapshot | Model and market foreign keys |
| `station_observations` | One weather-station reading/revision | Rolled into daily observations |
| `daily_observations` | One derived daily maximum/revision | May be linked from a resolution |
| `market_resolutions` | One collected market resolution | `reconciled_daily_observation_id` |
| `model_versions` | One immutable model version | Referenced by predictions and trades |
| `model_transitions` | One promotion or rollback | From/to model IDs |
| `collection_runs` | One collector run and its coverage | Stand-alone run record |

The most useful relationship chain is:

```text
forecast_snapshots --(IDs stored as JSON)-->
                                         prediction_snapshots
market_snapshots ------------------------^       |
                                                 v
                                               trades

daily_observations <--------------------- market_resolutions
```

Important caveat: snapshot tables are histories. A city/date/bracket can occur
many times. Joining snapshots only on `city` and `target_date` creates a
many-to-many join and can inflate a result dramatically. Prefer IDs, or first
reduce each source to the exact grain you need.

## 4. First queries: select, filter, sort, limit

Avoid `SELECT *` in reusable work. Naming columns documents your intent, keeps
Excel imports smaller, and prevents a later schema addition from changing your
output unexpectedly.

```sql
SELECT
    id,
    created_at,
    city,
    target_date,
    bracket_label,
    side,
    entry_price,
    model_prob,
    settled,
    pnl
FROM trades
ORDER BY id DESC
LIMIT 20;
```

Filtering text, numbers, and missing values:

```sql
SELECT
    id,
    city,
    target_date,
    bracket_label,
    side,
    edge,
    pnl
FROM trades
WHERE target_date >= '2026-09-01'
  AND city IN ('nyc', 'chicago', 'toronto')
  AND ABS(edge) >= 0.10
  AND pnl IS NOT NULL
ORDER BY target_date DESC, ABS(edge) DESC;
```

Points to notice:

- SQL strings and ISO dates use single quotes.
- `IN (...)` is cleaner than a chain of `OR` conditions.
- Use `IS NULL` and `IS NOT NULL`, never `= NULL`.
- Parenthesize mixed `AND` and `OR` conditions; `AND` is evaluated first.
- `LIMIT` is SQLite/PostgreSQL syntax. SQL Server commonly uses `TOP`.

Try these without looking ahead:

1. Return the 10 most recent unsettled trades.
2. List the distinct cities in `trades` alphabetically.
3. Find settled Toronto trades with negative P&L.

<details>
<summary>Solutions</summary>

```sql
SELECT id, created_at, city, target_date, bracket_label, side
FROM trades
WHERE settled = 0
ORDER BY created_at DESC, id DESC
LIMIT 10;

SELECT DISTINCT city
FROM trades
ORDER BY city;

SELECT id, target_date, bracket_label, side, pnl
FROM trades
WHERE city = 'toronto'
  AND settled = 1
  AND pnl < 0
ORDER BY target_date DESC;
```

</details>

## 5. Build useful columns with expressions and `CASE`

Aliases name a result column. `CASE` is SQL's general-purpose conditional.

```sql
SELECT
    id,
    city,
    target_date,
    model_prob,
    market_prob,
    ROUND((model_prob - market_prob) * 100, 1) AS probability_gap_points,
    CASE
        WHEN settled = 0 THEN 'open'
        WHEN pnl > 0 THEN 'win'
        WHEN pnl < 0 THEN 'loss'
        ELSE 'flat'
    END AS trade_result,
    ROUND(COALESCE(pnl, 0), 2) AS pnl_for_total
FROM trades
ORDER BY id DESC
LIMIT 25;
```

`COALESCE(pnl, 0)` returns the first non-`NULL` value. It is useful when a
missing value really should behave like zero, but use it deliberately: an open
trade's unknown P&L and a settled trade's zero P&L mean different things.

Wethr stores booleans as integers: `1` is true and `0` is false. It stores most
timestamps and dates as ISO-formatted text. ISO dates sort chronologically as
text when their format and timezone are consistent.

## 6. Summaries: `GROUP BY`, aggregates, and pivots

This is the SQL equivalent of a PivotTable showing performance by city:

```sql
SELECT
    city,
    COUNT(*) AS trades_placed,
    SUM(CASE WHEN settled = 1 THEN 1 ELSE 0 END) AS trades_settled,
    SUM(CASE WHEN settled = 1 AND pnl > 0 THEN 1 ELSE 0 END) AS winning_trades,
    ROUND(SUM(COALESCE(pnl, 0)), 2) AS total_pnl,
    ROUND(AVG(CASE WHEN settled = 1 THEN pnl END), 2) AS avg_settled_pnl
FROM trades
GROUP BY city
ORDER BY total_pnl DESC;
```

The `CASE` inside an aggregate is conditional aggregation—the closest SQL
equivalent to `SUMIFS` and `COUNTIFS`.

Notice that the `AVG` expression returns `NULL` for open trades. Aggregate
functions ignore `NULL`, so it averages only settled trades. Replacing those
values with zero would answer a different question.

Use `WHERE` to filter input rows and `HAVING` to filter completed groups:

```sql
SELECT
    strategy_version,
    city,
    COUNT(*) AS settled_trades,
    ROUND(SUM(pnl), 2) AS total_pnl
FROM trades
WHERE settled = 1
GROUP BY strategy_version, city
HAVING COUNT(*) >= 10
ORDER BY strategy_version, total_pnl DESC;
```

### A calibration table from signals

A well-calibrated model should resolve near 20% when it predicts roughly 20%.
This query bins probabilities into ten-point buckets:

```sql
SELECT
    CAST(model_prob * 10 AS INTEGER) / 10.0 AS probability_bucket,
    COUNT(*) AS signals,
    ROUND(AVG(model_prob), 3) AS avg_prediction,
    ROUND(AVG(outcome), 3) AS observed_rate,
    ROUND(AVG(brier_score), 4) AS avg_brier_score
FROM signals
WHERE outcome IS NOT NULL
GROUP BY probability_bucket
ORDER BY probability_bucket;
```

Do not compare tiny buckets as if they were equally reliable. Always include
the sample count.

## 7. Joins: SQL's version of `XLOOKUP`

A join matches rows between tables. Unlike `XLOOKUP`, it can return multiple
matches. That is powerful and dangerous.

### A safe one-to-one/many-to-one join

Attach the observed temperature to each legacy forecast:

```sql
SELECT
    f.city,
    f.target_date,
    f.model,
    f.lead_days,
    ROUND(f.ens_mean_c, 2) AS forecast_c,
    o.observed_max_c,
    ROUND(f.ens_mean_c - o.observed_max_c, 2) AS error_c,
    ROUND(ABS(f.ens_mean_c - o.observed_max_c), 2) AS absolute_error_c
FROM historical_forecasts AS f
JOIN historical_observations AS o
  ON o.city = f.city
 AND o.target_date = f.target_date
WHERE f.quality_status = 'legacy_unverified'
ORDER BY f.target_date DESC, f.city, f.lead_days, f.model
LIMIT 100;
```

The observation table has at most one row per city/date, so each forecast can
match at most one observation.

### A foreign-key join in the calibration ledger

Attach market context to predictions using the exact snapshot ID:

```sql
SELECT
    p.generated_at,
    m.city,
    m.target_date,
    m.bracket_label,
    p.model_version_id,
    ROUND(p.bracket_probability, 3) AS model_probability,
    p.executable_side,
    p.executable_ask,
    ROUND(p.edge, 3) AS edge,
    m.spread,
    m.volume
FROM prediction_snapshots AS p
JOIN market_snapshots AS m
  ON m.id = p.market_snapshot_id
WHERE m.target_date >= date('now', '-7 days')
ORDER BY p.id DESC
LIMIT 100;
```

### Keep unmatched rows with `LEFT JOIN`

Audit whether resolutions have reconciled weather observations:

```sql
SELECT
    r.id AS resolution_id,
    r.city,
    r.target_date,
    r.winning_label,
    r.reconciliation_status,
    d.station_id,
    d.max_temperature_c,
    d.quality_status AS observation_quality
FROM market_resolutions AS r
LEFT JOIN daily_observations AS d
  ON d.id = r.reconciled_daily_observation_id
WHERE r.target_date >= date('now', '-14 days')
ORDER BY r.target_date DESC, r.city;
```

If a condition on `d` is placed in the `WHERE` clause, unmatched rows disappear
and the query behaves like an inner join. Put conditions in the join when you
need to preserve the left side:

```sql
LEFT JOIN daily_observations AS d
  ON d.id = r.reconciled_daily_observation_id
 AND d.quality_status = 'ok'
```

### Prove a join's cardinality

Before and after a join, count rows and distinct IDs:

```sql
SELECT
    COUNT(*) AS result_rows,
    COUNT(DISTINCT p.id) AS distinct_predictions,
    COUNT(DISTINCT m.id) AS distinct_markets
FROM prediction_snapshots AS p
JOIN market_snapshots AS m
  ON m.id = p.market_snapshot_id
WHERE m.target_date = date('now');
```

Here, multiple model predictions can legitimately point to one market snapshot,
so `result_rows` can exceed `distinct_markets`. It should not exceed the number
of matching predictions.

## 8. CTEs: make a query read like Power Query steps

A common table expression (CTE) gives a temporary name to one stage of a query.
It makes multi-step reasoning easier to inspect.

```sql
WITH settled AS (
    SELECT
        id,
        city,
        target_date,
        strategy_version,
        size_usd,
        pnl
    FROM trades
    WHERE settled = 1
),
city_performance AS (
    SELECT
        strategy_version,
        city,
        COUNT(*) AS trades,
        SUM(size_usd) AS capital_deployed,
        SUM(pnl) AS total_pnl
    FROM settled
    GROUP BY strategy_version, city
)
SELECT
    strategy_version,
    city,
    trades,
    ROUND(capital_deployed, 2) AS capital_deployed,
    ROUND(total_pnl, 2) AS total_pnl,
    ROUND(100.0 * total_pnl / NULLIF(capital_deployed, 0), 2) AS return_pct
FROM city_performance
ORDER BY strategy_version, return_pct DESC;
```

`NULLIF(capital_deployed, 0)` prevents division by zero. `100.0` forces decimal
arithmetic; in SQLite, dividing two integers can produce integer division.

CTEs are not automatically faster. Their main first benefit is clarity. Once a
query is correct, inspect its plan and tune it if needed.

## 9. Window functions: calculate without collapsing rows

`GROUP BY` collapses several rows into one. A window function calculates across
related rows while keeping each original row visible.

### Running P&L

```sql
SELECT
    id,
    settled_at,
    city,
    pnl,
    ROUND(
        SUM(pnl) OVER (
            ORDER BY settled_at, id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ),
        2
    ) AS cumulative_pnl
FROM trades
WHERE settled = 1
ORDER BY settled_at, id;
```

### Rank cities within each strategy

```sql
WITH city_pnl AS (
    SELECT
        strategy_version,
        city,
        COUNT(*) AS trades,
        SUM(pnl) AS total_pnl
    FROM trades
    WHERE settled = 1
    GROUP BY strategy_version, city
)
SELECT
    strategy_version,
    city,
    trades,
    ROUND(total_pnl, 2) AS total_pnl,
    DENSE_RANK() OVER (
        PARTITION BY strategy_version
        ORDER BY total_pnl DESC
    ) AS pnl_rank
FROM city_pnl
ORDER BY strategy_version, pnl_rank, city;
```

### Select the latest snapshot per market

This is one of the most reusable time-series patterns:

```sql
WITH recent_slice AS (
    SELECT
        id,
        condition_id,
        captured_at,
        city,
        target_date,
        bracket_label,
        yes_best_bid,
        yes_best_ask,
        volume
    FROM market_snapshots
    WHERE id > (
        SELECT MAX(id) - 5000
        FROM market_snapshots
    )
      AND target_date BETWEEN date('now') AND date('now', '+2 days')
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY condition_id
            ORDER BY captured_at DESC, id DESC
        ) AS recency_rank
    FROM recent_slice
)
SELECT
    id,
    captured_at,
    city,
    target_date,
    bracket_label,
    yes_best_bid,
    yes_best_ask,
    volume
FROM ranked
WHERE recency_rank = 1
ORDER BY target_date, city, bracket_label;
```

Filter the large table before ranking to keep the work bounded. This example
means "the latest appearance within the last 5,000 inserted snapshots," not
"the latest appearance anywhere in history." A market absent from that recent
slice will be omitted. That explicit scope makes the query fast enough for
exploration and documents its tradeoff.

## 10. Dates and times in SQLite

SQLite has no dedicated timestamp storage type in this schema; Wethr uses ISO
text. SQLite's date functions parse the strings when needed.

```sql
SELECT
    created_at,
    date(created_at) AS created_date_utc,
    time(created_at) AS created_time_utc,
    julianday('now') - julianday(created_at) AS age_days
FROM trades
ORDER BY id DESC
LIMIT 10;
```

Useful SQLite expressions:

```sql
date('now')
date('now', '-7 days')
datetime('now', '-24 hours')
strftime('%Y-%m', created_at)
julianday(settled_at) - julianday(created_at)
```

Group trades by month:

```sql
SELECT
    strftime('%Y-%m', target_date) AS target_month,
    COUNT(*) AS trades,
    ROUND(SUM(COALESCE(pnl, 0)), 2) AS pnl
FROM trades
GROUP BY target_month
ORDER BY target_month;
```

Time-zone warning: many ledger timestamps end in `Z` and are UTC; `target_date`
is the market's local calendar date. Do not assume that extracting the UTC date
from a timestamp gives the same target day.

In SQL Server, date syntax commonly uses `GETDATE()` and `DATEADD`; PostgreSQL
uses interval expressions. The analytical idea transfers, but the functions do
not.

## 11. JSON: normalize a list stored inside a column

`prediction_snapshots.forecast_snapshot_ids_json` contains a JSON array of the
forecast snapshot IDs used by a prediction. SQLite's `json_each` turns that
array into rows:

```sql
WITH recent_predictions AS (
    SELECT
        id,
        generated_at,
        model_version_id,
        forecast_snapshot_ids_json
    FROM prediction_snapshots
    WHERE julianday(generated_at) >= julianday('now', '-1 day')
    ORDER BY id DESC
    LIMIT 100
)
SELECT
    p.id AS prediction_id,
    p.generated_at,
    p.model_version_id,
    f.id AS forecast_id,
    f.city,
    f.target_date,
    f.provider,
    f.model,
    f.lead_bucket,
    f.mean_c,
    f.std_c
FROM recent_predictions AS p
JOIN json_each(p.forecast_snapshot_ids_json) AS j
JOIN forecast_snapshots AS f
  ON f.id = CAST(j.value AS INTEGER)
ORDER BY p.id DESC, f.provider, f.model;
```

This is effectively a many-to-many bridge encoded as JSON. In a conventional
relational design, it might be a separate table with one row per
prediction/forecast pair.

Other useful JSON functions include `json_valid`, `json_extract`, and the `->`
and `->>` operators. JSON functions differ substantially between database
systems.

## 12. Compare models on the same market snapshot

Conditional aggregation can pivot model rows into columns:

```sql
WITH recent_predictions AS (
    SELECT
        market_snapshot_id,
        model_version_id,
        bracket_probability
    FROM prediction_snapshots
    WHERE id > (
        SELECT MAX(id) - 5000
        FROM prediction_snapshots
    )
),
model_comparison AS (
    SELECT
        p.market_snapshot_id,
        m.captured_at,
        m.city,
        m.target_date,
        m.bracket_label,
        MAX(CASE
            WHEN p.model_version_id = 'legacy-emos-2026-04-08'
            THEN p.bracket_probability
        END) AS emos_probability,
        MAX(CASE
            WHEN p.model_version_id = 'raw-ensemble-v1'
            THEN p.bracket_probability
        END) AS raw_probability
    FROM recent_predictions AS p
    JOIN market_snapshots AS m
      ON m.id = p.market_snapshot_id
    WHERE m.target_date BETWEEN date('now') AND date('now', '+2 days')
    GROUP BY
        p.market_snapshot_id,
        m.captured_at,
        m.city,
        m.target_date,
        m.bracket_label
)
SELECT
    captured_at,
    city,
    target_date,
    bracket_label,
    ROUND(emos_probability, 3) AS emos_probability,
    ROUND(raw_probability, 3) AS raw_probability,
    ROUND(emos_probability - raw_probability, 3) AS model_difference
FROM model_comparison
WHERE emos_probability IS NOT NULL
  AND raw_probability IS NOT NULL
ORDER BY ABS(emos_probability - raw_probability) DESC
LIMIT 100;
```

This pattern is useful when rows contain categories that Excel users might put
in PivotTable columns.

## 13. Query quality and performance

Correctness comes before speed. Check both deliberately.

### A pre-flight checklist

Before trusting a result, answer:

1. What is one input row in every table?
2. What is one output row?
3. Can each join key match zero, one, or many rows on either side?
4. Should missing rows be excluded (`JOIN`) or retained (`LEFT JOIN`)?
5. Are `NULL`s unknown, not applicable, or really zero?
6. Did a date/timestamp filter use the intended timezone?
7. Do row counts and distinct-key counts agree with the intended grain?
8. Is the result ordered explicitly?

### Inspect a query plan

```sql
EXPLAIN QUERY PLAN
SELECT
    city,
    COUNT(*) AS snapshots
FROM market_snapshots
WHERE target_date = date('now')
GROUP BY city;
```

Wethr has an index beginning with `market_snapshots.target_date`, so this filter
can avoid scanning the full table. Index column order matters: an index on
`(target_date, city, condition_id, captured_at)` is naturally useful when the
query constrains `target_date`; it is less useful for a filter on `captured_at`
alone.

### Habits for this 3+ GB database

- Filter snapshot tables by indexed columns such as `target_date` before joins,
  windows, or grouping.
- During exploration, bound append-only snapshot histories to a recent ID slice
  when that scope answers the question; state clearly that older entities may be
  omitted.
- Select only needed columns; large JSON and raw payload columns are expensive.
- Use `LIMIT` while developing, but remember that `LIMIT` without a selective
  filter does not always prevent upstream sorting or aggregation work.
- Avoid wrapping indexed filter columns in a function when a direct range can
  express the same condition.
- Use exact IDs for ledger joins.
- Run `EXPLAIN QUERY PLAN` when a query feels unexpectedly slow.
- In Excel, aggregate in SQL before importing whenever you do not need the raw
  rows.

For example, this direct range is easier to index than `date(captured_at) = ...`:

```sql
WHERE captured_at >= '2026-09-15T00:00:00Z'
  AND captured_at <  '2026-09-16T00:00:00Z'
```

## 14. Common bugs to learn to spot

### Accidental fan-out

```sql
-- Dangerous: both tables can have many rows for a city/date.
SELECT *
FROM forecast_snapshots AS f
JOIN market_snapshots AS m
  ON m.city = f.city
 AND m.target_date = f.target_date;
```

Reduce each side to the intended grain or follow explicit IDs first.

### A `LEFT JOIN` silently changed to an inner join

```sql
-- Unmatched observations cannot pass this WHERE condition.
FROM market_resolutions AS r
LEFT JOIN daily_observations AS d
  ON d.id = r.reconciled_daily_observation_id
WHERE d.quality_status = 'ok'
```

Move the right-side condition into `ON` if unmatched resolutions must remain.

### Counting the wrong thing

```sql
COUNT(*)        -- all rows
COUNT(pnl)      -- rows where pnl is not NULL
COUNT(DISTINCT city) -- distinct non-NULL cities
```

### Turning unknown into zero too early

`AVG(COALESCE(pnl, 0))` includes open trades as zero. `AVG(pnl)` excludes them.
Neither is universally correct; they answer different questions.

### Assuming an alias is available everywhere

SQLite allows many aliases in `ORDER BY`, but aliases are generally not
available to `WHERE` because `WHERE` is logically evaluated before `SELECT`.
Use a CTE when you want to filter a derived column cleanly.

### Using floating-point equality

Prefer a tolerance:

```sql
WHERE ABS(model_prob - market_prob) < 0.000001
```

rather than expecting independently calculated `REAL` values to be exactly
equal.

## 15. Working with SQL through Excel

Whether your connection uses Power Query, ODBC, or a database connector, a good
division of labour is:

- SQL: filter, join, define business logic, aggregate, and validate grain.
- Excel/Power Query: parameters, presentation, light reshaping, and refresh.
- PivotTables/charts: interactive exploration and communication.

Push work into SQL when it reduces the number of imported rows or establishes a
single reusable definition. Keep the final formatting in Excel.

For a refreshable query, return a stable contract:

- Explicit columns in a predictable order.
- Stable, unique column names.
- A documented output grain.
- Dates/timestamps in a format Excel parses consistently.
- No dependency on implicit row ordering.
- Parameters for dates/cities rather than hand-edited string concatenation when
  your connector supports them.

Do not paste SQLite dot commands into Excel. Also expect to translate SQLite
functions such as `date`, `strftime`, `julianday`, and `json_each` if your day-job
database uses a different SQL dialect.

## 16. A four-week learning path

Use short sessions and type the queries rather than only reading them.

### Week 1: clean retrieval

- Sections 2–6.
- Rewrite familiar Excel filters and PivotTables in SQL.
- Predict the result's columns and grain before running each query.
- Practise explaining the difference between `NULL`, zero, and an empty string.

### Week 2: relationships and proof

- Sections 7–8.
- Draw the two tables before each join and label each side one or many.
- Check `COUNT(*)` and `COUNT(DISTINCT key)` after every new join.
- Rebuild one day-job `XLOOKUP` workflow as a join plus a validation count.

### Week 3: analytical SQL

- Sections 9–12.
- Build running totals, rankings, and latest-row queries.
- Use CTEs to separate filtering, enrichment, and aggregation.
- Compare the active and shadow Wethr models on the same market snapshots.

### Week 4: independent analysis

- Sections 13–17.
- Complete one mini-project below.
- Save the final query with comments stating its grain and assumptions.
- Explain the result in plain language, including one caveat.

## 17. Wethr mini-projects

These deliberately do not have complete solutions. The point is to combine the
patterns above.

### Project A: paper-trading scorecard

Build one row per `strategy_version` and city with:

- placed and settled trades;
- win rate among settled trades;
- total dollars deployed;
- total and average P&L;
- return on deployed capital;
- first and latest trade dates.

Validation: settled + open should equal placed. Decide how flat trades affect
win rate and document it.

### Project B: forecast accuracy

Join `historical_forecasts` to `historical_observations`. Produce one row per
model and lead day with sample count, mean error (bias), mean absolute error, and
root mean squared error.

SQLite has `SQRT` in this build, so RMSE can be calculated as:

```sql
SQRT(AVG((forecast - actual) * (forecast - actual)))
```

Validation: compare signed mean error with absolute error and explain why the
first can look good when positive and negative misses cancel.

### Project C: market liquidity monitor

Select the latest snapshot per `condition_id` for today and the next two target
days. Summarize by city:

- number of market brackets;
- brackets missing a bid or ask;
- average spread where available;
- total volume and liquidity;
- oldest/latest capture timestamp.

Validation: confirm there is only one selected row per `condition_id` before
grouping by city.

### Project D: model disagreement

Extend the model-comparison query in section 12. Group absolute probability
differences into buckets, then compare cities and lead times. Join through the
forecast ID JSON only if you need forecast lead metadata.

Validation: ensure both models were generated against the same
`market_snapshot_id`.

### Project E: reconciliation coverage

For each target date, report resolution count and the number/rate that are
matched, missing station truth, or in another status. Then attach the reconciled
daily maximum for matched rows.

Validation: joining to `daily_observations` must not reduce the resolution count.

## 18. Query template for future work

Start complex analysis with this scaffold:

```sql
-- Question:
-- Output grain: one row per ...
-- Source grain(s):
-- Join expectation: one-to-one / many-to-one / one-to-many
-- NULL policy:
-- Timezone/date policy:

WITH base AS (
    SELECT
        -- only required columns
    FROM some_table
    WHERE
        -- selective filters early
),
enriched AS (
    SELECT
        -- joins and derived columns
    FROM base AS b
    -- JOIN ...
),
summarized AS (
    SELECT
        -- final grain and measures
    FROM enriched
    GROUP BY
        -- every dimension in the final grain
)
SELECT
    -- explicit final output columns
FROM summarized
ORDER BY
    -- deterministic presentation order
;
```

For serious work, write a second validation query beside the main query. A
result is much easier to trust when its row count, key uniqueness, missingness,
and totals are checked explicitly.

## 19. SQL vocabulary worth knowing next

Once the material above feels natural, round out your knowledge with:

- primary, foreign, natural, and surrogate keys;
- normalization and denormalization;
- constraints and referential integrity;
- transactions and ACID properties;
- indexes and query plans;
- views and materialized views;
- set operations: `UNION`, `INTERSECT`, and `EXCEPT`;
- correlated subqueries and `EXISTS`;
- database permissions and parameterized queries;
- slowly changing dimensions and event/snapshot data models.

Wethr already gives you concrete examples of most of these ideas. The best next
step is to answer a real question, state the expected grain first, and make the
query prove its own correctness.
