-- Grain: one row per strategy epoch per UTC settlement date.
-- bankroll_end_of_day = epoch starting bankroll + cumulative realised P/L, which
-- matches get_stats(strategy_version=...) in collector/src/paper_trader.py.
with daily as (
    select
        strategy_epoch,
        settled_date_utc,
        any_value(epoch_starting_bankroll)      as starting_bankroll,
        count(*)                                as trades_settled,
        count_if(status = 'won')                as wins,
        sum(size_usd)                           as stake_usd,
        sum(pnl)                                as pnl_usd
    from {{ ref('fct_trades') }}
    where is_settled
    group by 1, 2
)
select
    strategy_epoch,
    settled_date_utc,
    trades_settled,
    wins,
    wins / nullif(trades_settled, 0)                                           as win_rate,
    stake_usd,
    pnl_usd,
    sum(pnl_usd) over (partition by strategy_epoch order by settled_date_utc
                       rows between unbounded preceding and current row)       as cumulative_pnl_usd,
    starting_bankroll
      + sum(pnl_usd) over (partition by strategy_epoch order by settled_date_utc
                           rows between unbounded preceding and current row)   as bankroll_end_of_day
from daily
