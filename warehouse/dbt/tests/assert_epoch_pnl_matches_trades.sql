-- Fails if the daily mart's final cumulative P/L per epoch differs from the
-- straight sum of settled trade P/L (catches window-function or grain bugs).
with mart as (
    select strategy_epoch, max_by(cumulative_pnl_usd, settled_date_utc) as final_pnl
    from {{ ref('daily_pnl_by_epoch') }}
    group by 1
),
direct as (
    select strategy_version as strategy_epoch, sum(pnl) as total_pnl
    from {{ source('raw', 'trades') }}
    where settled = 1
    group by 1
)
select d.strategy_epoch, d.total_pnl, m.final_pnl
from direct d
left join mart m using (strategy_epoch)
where m.final_pnl is null or abs(d.total_pnl - m.final_pnl) > 0.01
