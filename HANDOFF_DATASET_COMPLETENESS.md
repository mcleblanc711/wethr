# Handoff: fix dataset-completeness cohort mismatch and clamping

Branch: `agent/dataset-completeness-cohort` (based on `main` @ `bde9fc6`).
Delete this file as part of the PR once the fix lands.

## Context

This is one of two still-open items from the calibration pipeline review —
see `CALIBRATION_REVIEW_FOLLOWUPS.md` at repo root for the other (daily
observation coverage, on a separate branch: `agent/observation-coverage-checks`
— do not touch that in this branch). All severity-1 blockers from the
original review are already resolved; this is a severity-2 data-quality item.

## The problem

`dataset_quality()` in `collector/src/calibration_ops.py:1013-1080` computes
a `data_completeness` fraction that feeds the promotion gate
(`collector/src/calibration_ops.py:1426`: `"completeness":
candidate_metrics.get("data_completeness", 0) >= .95`). The fraction is
built from two queries that don't measure the same cohort:

```python
# collector/src/calibration_ops.py:1049-1064
window = conn.execute(
    """SELECT MIN(target_date) AS first, MAX(target_date) AS last
        FROM forecast_snapshots WHERE id IN ({placeholders})""",
    ids,
).fetchone()
resolutions = conn.execute(
    """SELECT COUNT(DISTINCT city || '|' || target_date) AS total, ...
       FROM market_resolutions WHERE target_date BETWEEN ? AND ?""",
    (window["first"], window["last"]),
).fetchone()
covered = int(conn.execute(
    """SELECT COUNT(DISTINCT city || '|' || target_date) AS n
        FROM forecast_snapshots WHERE id IN ({placeholders})""",
    ids,
).fetchone()["n"])
```

- **Numerator** (`covered`): distinct city/day pairs among the candidate's
  own `snapshot_ids` — already scoped to a specific lead bucket and
  `lead_basis` by the caller (see below).
- **Denominator** (`resolved_city_days`): distinct city/day pairs in
  `market_resolutions` for *any* city, within the snapshot window's
  `MIN(target_date)..MAX(target_date)` — **no city filter, no lead-bucket
  filter, no lead_basis filter at all.** If the candidate only trains on 3
  cities but 10 cities resolve in that date range, the denominator counts
  all 10.

Then:

```python
# collector/src/calibration_ops.py:1075-1077
"data_completeness": (
    min(covered / resolved_city_days, 1.0) if resolved_city_days else 0.0
),
```

The `min(..., 1.0)` silently clamps any case where `covered >
resolved_city_days` (which can happen — e.g. discrepancy resolutions get
deleted/superseded, or multiple lead buckets snapshot the same city/day)
instead of surfacing it as a data-integrity failure worth investigating.

## Where the numerator's cohort actually comes from

`dataset_quality()` is called from two places, each defining "the
candidate's cohort" differently — both need to flow through consistently
once you fix this:

1. **`train_candidate()`**, `collector/src/calibration_ops.py:~1094-1118`:
   `snapshot_ids` comes from `training_forecast_rows(conn,
   lead_bucket_name=lead, cutoff=cutoff, lead_basis=lead_basis)`, already
   filtered to one `lead` bucket, one `lead_basis`, and a 365-day window,
   then further reduced to `by_city` / `eligible_city` (>=90 days spanning
   >=89 days) and `included_group` (>=15 rows) for actual training — but
   `dataset_quality` is called with the *unreduced* `snapshot_ids` from all
   `rows`, not the eligible/included subset. Decide whether completeness
   should be measured against all candidate rows or just the eligible ones.

2. **`promote_model()`**, `collector/src/calibration_ops.py:~1365-1376`:
   `snapshot_ids` comes from `manifest.get("forecast_snapshot_ids", [])` —
   the manifest recorded at training time (`collector/src/calibration_ops.py:1140-1156`
   has the `manifest` dict structure). Re-measured against current ledger
   state on purpose (comment at :1363-1364), so a discrepancy that appeared
   *after* training correctly fails this gate — keep that property.

## Required outcome (from `CALIBRATION_REVIEW_FOLLOWUPS.md`)

- Define "expected" and "covered" city/days against the *same* cohort: same
  cities, same lead bucket, same `lead_basis` as the snapshot set being
  measured — not an unscoped date-range query.
- Treat `covered > expected` as an integrity failure (log/flag it, don't
  clamp to 1.0 and move on) — surface it in the returned dict so callers can
  decide whether to fail closed.
- Report pending / missing-metadata / missing-truth / discrepancy counts
  *separately* rather than folding them all into one ratio, so the promotion
  report actually shows *why* completeness is low.

## Suggested approach

1. Change the "expected" query to restrict by the actual city set and
   date set implied by `snapshot_ids` (e.g. join through
   `forecast_snapshots` for those ids to get the exact `(city, target_date)`
   pairs, then check `market_resolutions` for exactly those pairs — not a
   blanket date-range scan).
2. Split out the resolution states for those exact pairs: matched/resolved,
   pending (no resolution row yet), discrepancy, missing station/truth
   metadata — these look like they're already distinguishable via
   `market_resolutions.reconciliation_status` (used at :1056) — return counts
   for each rather than only `bad`/discrepancies.
3. Replace the `min(covered / resolved_city_days, 1.0)` clamp with an
   explicit check: if `covered > expected_total`, set an
   `"integrity_error"`-style flag (or raise, if that's a better fit for how
   `dataset_quality` failures currently propagate — check callers) instead
   of silently normalizing.
4. Decide (and document in a code comment, since this was flagged as an
   open judgment call in the original review) whether `train_candidate`
   should measure completeness against all `rows` or just `eligible_city`/
   `included_group` rows, and make sure `promote_model`'s manifest-based
   path stays consistent with whatever `train_candidate` records into
   `forecast_snapshot_ids`.
5. Add tests — there are currently **no** direct tests of `dataset_quality`
   itself (checked `collector/tests/test_calibration_ledger.py`; the
   existing tests around `data_completeness` at lines 241 and 256 only feed
   a pre-built dict into the promotion-gate check, not real ledger data).
   Add cases for: matching cohort with 100% coverage, a city outside the
   cohort that shouldn't count in either numerator or denominator, a
   discrepancy resolution, and a constructed `covered > expected` scenario
   that should trip the new integrity check rather than clamp.

## Acceptance criteria

- `dataset_quality()` numerator and denominator are computed from the same
  city/lead/lead_basis cohort.
- `covered > expected` no longer silently clamps to `1.0`.
- Pending / missing-metadata / missing-truth / discrepancy counts are
  visible in the returned dict, not just a single ratio.
- `train_candidate()` and `promote_model()` both still pass their existing
  tests, plus new coverage for the cases above.
- `./scripts/check` passes.
- Delete this file in the same PR.
