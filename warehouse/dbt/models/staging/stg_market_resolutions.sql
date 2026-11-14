select
    event_id,
    condition_id                                          as winning_condition_id,
    city,
    target_date,
    winning_label,
    winning_lower,
    winning_upper,
    winning_unit,
    convert_timezone('UTC', resolved_at)::timestamp_ntz   as resolved_at_utc,
    reconciliation_status
from {{ source('raw', 'market_resolutions') }}
