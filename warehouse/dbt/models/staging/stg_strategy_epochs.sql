select
    label                                                 as strategy_epoch,
    convert_timezone('UTC', started_at)::timestamp_ntz    as started_at_utc,
    starting_bankroll,
    notes
from {{ source('raw', 'strategy_epochs') }}
