-- Grain: model version x 10-point probability bucket.
-- Each market bracket (condition_id) contributes ONE prediction per model version:
-- the latest one, so millions of repeated snapshots don't swamp the buckets.
-- Outcome = the bracket was the event's winning label.
with last_prediction as (
    select
        p.model_version_id,
        m.condition_id,
        m.event_id,
        m.bracket_label,
        p.bracket_probability
    from {{ ref('stg_prediction_snapshots') }} p
    join {{ ref('stg_market_snapshots') }} m
      on m.market_snapshot_id = p.market_snapshot_id
    qualify row_number() over (
        partition by p.model_version_id, m.condition_id
        order by p.generated_at_utc desc, p.prediction_id desc) = 1
),
resolved as (
    select
        lp.model_version_id,
        lp.bracket_probability,
        iff(lp.bracket_label = r.winning_label, 1, 0) as hit
    from last_prediction lp
    join {{ ref('stg_market_resolutions') }} r
      on r.event_id = lp.event_id
)
select
    model_version_id,
    least(floor(bracket_probability * 10), 9) / 10       as prob_bucket_low,
    least(floor(bracket_probability * 10), 9) / 10 + 0.1 as prob_bucket_high,
    count(*)                                             as n_brackets,
    avg(bracket_probability)                             as mean_predicted,
    avg(hit)                                             as observed_rate,
    avg(hit) - avg(bracket_probability)                  as calibration_gap,
    count(*) < 30                                        as small_sample
from resolved
group by 1, 2, 3
