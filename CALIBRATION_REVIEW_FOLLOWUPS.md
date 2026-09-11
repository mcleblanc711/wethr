# Calibration Pipeline Review Follow-ups

Original review target: `55747b9` (`Fix review defects in the calibration-grade
data pipeline`), parent `ec0a149` (`Add calibration-grade data pipeline`).

Status: **All severity-1 (merge/promotion blocker) items from the original
review were resolved in `27d8547` (`Resolve calibration review blockers`,
2026-07-24)** — run/sighting identity, station-bound reconciliation,
lead-bucket serving, promotion-freshness re-evaluation, resolution-time
comparison, and archive finalization are all in place in `ledger.py` and
`calibration_ops.py`. Live trading remains disabled regardless.

This file now tracks only what's still verifiably open, re-audited against
`main` on 2026-09-11.

## Still open

### Dataset completeness mixes cohorts and clamps silently

`dataset_quality()` in `collector/src/calibration_ops.py` computes
`data_completeness` as `min(covered / resolved_city_days, 1.0)`, where the
numerator is city/days covered by the candidate's own forecast snapshots and
the denominator is *every* resolved city/day in the snapshot window — still
two different cohorts, and a numerator that legitimately exceeds the
denominator (e.g. from referential drift) is silently clamped to 1.0 rather
than surfaced as an integrity failure.

Required outcome (unchanged from original review):
- Define expected and covered city/days for the same candidate cohort.
- Treat numerator > denominator as an integrity failure, not a clamp.
- Report pending/missing-metadata/missing-truth/discrepancy counts separately.

### Daily-observation coverage checks don't dedupe or bound gaps

`aggregate_daily_observation()` in `collector/src/ledger.py` still gates
quality on raw `len(selected) >= 12` and `span_hours >= 18.0` (min to max of
observed timestamps). It does not deduplicate revised/duplicate observation
times before counting, does not apply a maximum-gap rule (a 17-hour span with
one reading at each end passes; a scan with an 8-hour hole in the middle also
passes if the endpoints are far enough apart), and does not special-case
23/25-hour DST days — `min_span_hours=18.0` is a flat constant.

Required outcome (unchanged from original review):
- Count unique observation times after resolving provider revisions.
- Add a maximum-gap rule, not just an endpoint span.
- Handle 23- and 25-hour DST days explicitly.

## Resolved since original review

- **Archive revision chain** — `archive_month()` now writes an immutable
  manifest inside every `rN/` revision directory, and `_verify_archive_manifests()`
  walks and verifies all retained revisions, not just the latest.
- **METAR `obsTime`/`reportTime` handling** — covered by
  `collector/tests/test_calibration_ledger.py`.

## Not yet verified

- **BMA per-model/per-lead activation gating** (`sample_floor`, kill switch) —
  no `BMA`/`kill_switch`/`sample_floor` symbols found in
  `collector/src/calibration_ops.py`; either this was never built or lives
  under different naming. Needs a dedicated look before assuming either way.
- **`lead_basis` `CHECK` constraint** — no `CHECK` clause on `lead_basis`
  found in the schema; migrated rows may still rely on convention rather than
  a DB-enforced default/constraint.
- **Concurrent migrations / SQLite `busy_timeout` behavior under contention**
  — no test file references `busy_timeout` or concurrent access; the setting
  itself may still be fine, but it's untested.

## Bigger picture

None of the above blocks the pipeline's own internal correctness gates from
running — they mostly affect how *trustworthy the completeness/coverage
numbers are* when deciding whether a candidate is promotable. Separately,
per `OPERATIONS.md`, no candidate has actually cleared promotion yet: paper
trading still runs on `legacy-emos-2026-04-08`, and promotion requires a new
uninterrupted seven-day capture window per `calibration_exclusions.json`
policy. That's the larger standing milestone once the items above are
addressed.
