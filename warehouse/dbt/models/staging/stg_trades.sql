-- Timestamps load as TIMESTAMP_LTZ/TZ from Parquet; pin them to UTC wall time so
-- DATE() never depends on the session time zone.
select
    id                                                    as trade_id,
    convert_timezone('UTC', created_at)::timestamp_ntz    as created_at_utc,
    convert_timezone('UTC', settled_at)::timestamp_ntz    as settled_at_utc,
    target_date,
    city,
    bracket_label,
    bracket_lower,
    bracket_upper,
    bracket_unit,
    side,
    entry_price,
    size_usd,
    model_prob,
    market_prob,
    edge,
    confidence,
    kelly_full,
    kelly_frac,
    settled = 1                                           as is_settled,
    outcome,
    pnl,
    condition_id,
    market_volume,
    model_version_id,
    prediction_snapshot_id,
    strategy_version                                      as strategy_epoch,
    -- Same rule as collector/src/paper_trader.py get_stats(): win = settled and pnl > 0.
    case
        when settled = 0 then 'open'
        when pnl > 0     then 'won'
        else                  'lost'
    end                                                   as status
from {{ source('raw', 'trades') }}
