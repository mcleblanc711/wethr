-- One row per paper trade (open and settled), with its epoch attached.
-- Marts downstream filter to settled trades; open trades never count as wins or losses.
select
    t.*,
    date(t.settled_at_utc)              as settled_date_utc,
    e.starting_bankroll                 as epoch_starting_bankroll
from {{ ref('stg_trades') }} t
left join {{ ref('stg_strategy_epochs') }} e
  on e.strategy_epoch = t.strategy_epoch
