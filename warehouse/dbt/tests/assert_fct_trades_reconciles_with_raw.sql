-- Fails (returns a row) if the mart gained or lost trades vs the raw table.
select raw_rows, mart_rows
from (select count(*) as raw_rows from {{ source('raw', 'trades') }})
cross join (select count(*) as mart_rows from {{ ref('fct_trades') }})
where raw_rows <> mart_rows
