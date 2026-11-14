select
    id                                                    as prediction_id,
    convert_timezone('UTC', generated_at)::timestamp_ntz  as generated_at_utc,
    model_version_id,
    market_snapshot_id,
    bracket_probability,                                  -- P(bracket resolves YES)
    raw_probability,
    executable_side,
    executable_ask,
    edge,
    data_quality
from {{ source('raw', 'prediction_snapshots') }}
