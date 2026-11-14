-- Grain: city x UTC capture date. Rolls ~2.4M market snapshots into a small
-- table that dashboards can scan cheaply.
select
    city,
    date(captured_at_utc)                 as captured_date_utc,
    count(*)                              as snapshots,
    count(distinct condition_id)          as distinct_brackets,
    avg(spread)                           as avg_spread,
    median(spread)                        as median_spread,
    avg(liquidity)                        as avg_liquidity,
    max(volume)                           as max_volume
from {{ ref('stg_market_snapshots') }}
group by 1, 2
