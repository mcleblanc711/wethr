# Calibration Pipeline Review Follow-ups

Review target: `55747b9` (`Fix review defects in the calibration-grade data pipeline`)

Parent: `ec0a149` (`Add calibration-grade data pipeline`)

Status: **Do not treat the pipeline as calibration-grade or promote a candidate until the severity-1 items below are resolved.** Paper trading may continue as the labeled legacy control; live trading remains disabled.

## Severity 1 — merge/promotion blockers

### 1. Preserve forecast run identity

`record_forecast_snapshot` hashes member content without `run_time`, `issued_at`, or `lead_basis`. Identical values from different verified runs collapse into one row and retain the earlier lead and bucket. The current deduplication test explicitly expects this collapse.

Required outcome:

- Retain a content hash for deduplication/auditing.
- Give every distinct verified run or capture cutoff its own snapshot/sighting identity.
- Prevent a capture-cutoff sighting and a verified-run sighting from collapsing together.
- Add regressions for identical values across different run times, lead buckets, and lead bases.

### 2. Reconcile only against the declared station

`reconcile_resolution` selects daily truth by city/date but does not require `daily_observations.station_id` to match `market_resolutions.declared_station`. A complete day from an unrelated station currently reconciles as `matched`.

Required outcome:

- Normalize the declared station identifier.
- Require the matching station and official adapter when selecting daily truth.
- Preserve the selected daily-observation ID or equivalent provenance on the reconciliation.
- Test that a wrong station cannot reconcile or train.

### 3. Align training, shadow evaluation, and serving

Forecast snapshots are stored per provider model, `_independent_rows` reduces them to one arbitrary row per city/day, and the resulting EMOS parameters are served against the pooled multi-model ensemble. Lead-bucket candidates are also materialized at every lead, and the schema permits only one global active model.

Required outcome:

- Choose and document either pooled-ensemble calibration or per-provider-model calibration followed by a defined mixture.
- Train and score the same feature distribution that is served.
- Apply a candidate only to forecasts in its declared lead bucket and lead basis.
- Support active routing by lead bucket, or promote an immutable bundle containing all required lead-bucket models.
- Remove the extra promotable `lead_bucket='all'` candidate unless it has an explicitly validated purpose.

### 4. Retry unresolved outcomes at city/date grain indefinitely

The unresolved query joins resolutions only by target date. One resolved city suppresses retries for every unresolved city on that date. The new 30-day cutoff also permanently misses markets that resolve later and contradicts the original retry-until-resolved requirement.

Required outcome:

- Determine unresolved state at city/date or event/condition grain.
- Continue retrying until resolution; use backoff or lower-frequency retries rather than abandonment.
- Continue alerting for every unresolved item older than three days.
- Test a date with multiple cities where only one has resolved, plus a resolution arriving after day 30.

### 5. Compare resolution times as datetimes

`min(str(existing["resolved_at"]), resolved_iso)` is unsafe because `iso_utc` emits fractional seconds only when present. Lexicographic order can select `00:00:00.500000Z` as earlier than `00:00:00Z`.

Required outcome:

- Parse both values, take the datetime minimum, then serialize with `iso_utc`.
- Add fractional-second, offset, and zero-microsecond tests.

### 6. Recompute promotion evidence immediately before promotion

`promote_model` trusts stored `metrics_json`. Shadow duration itself is monotonic, but reconciliation, completeness, archive status, operational acceptance, Gamma metrics, and the active-control comparison can become stale.

Required outcome:

- Re-evaluate current evidence immediately before the atomic transition.
- Verify the freshly computed gates inside the promotion transaction or otherwise prevent a race.
- Persist the exact gate inputs/report used for the transition.
- Add a test where a candidate passes evaluation, the ledger later gains a discrepancy, and promotion fails.

### 7. Do not finalize archives with missing resolutions

`archive_month` checks only existing resolution rows whose status is `pending`. A month with unresolved trades/signals and no resolution row has a pending count of zero and can be archived. This is especially dangerous when combined with the retry cutoff.

Required outcome:

- Refuse finalization when any expected city/date/event lacks an authoritative Gamma resolution.
- Define which non-`matched` reconciliation states are finalizable and record that policy in the manifest.
- Test an unresolved market with no `market_resolutions` row.

## Severity 2 — high-priority data-quality work

### 8. Redefine dataset completeness on a coherent cohort

The current numerator counts manifest forecast city/days while the denominator counts every resolved city/day between the manifest's minimum and maximum dates. This mixes cohorts and lead buckets. Clamping ratios above 1.0 can hide deleted resolutions or referential drift.

Required outcome:

- Define expected and covered city/days for the same candidate cohort.
- Count only currently eligible, reconciled records in the numerator.
- Treat numerator greater than denominator as an integrity failure rather than clamping.
- Report pending, missing-metadata, missing-truth, and discrepancy counts separately.

### 9. Strengthen daily-observation coverage checks

`12` readings spanning `18` hours is an improvement over accepting one reading but can still miss the true maximum, accept large gaps, or count revised/duplicate timestamps more than once.

Required outcome:

- Count unique observation times after resolving provider revisions.
- Use provider/station cadence expectations, local-day boundary coverage, and a maximum-gap rule.
- Handle 23- and 25-hour DST days explicitly.
- Keep thresholds configurable and record the applied quality policy/version.

### 10. Preserve and verify the archive revision chain

Revisioning is acceptable, but the top-level manifest is overwritten and superseded revision directories do not retain their own referenced manifests. Old revisions are therefore orphaned from routine verification.

Required outcome:

- Store an immutable manifest inside every revision directory.
- Make the top-level file a current pointer or hash-linked revision index.
- Verify every retained revision and the supersession chain.

## Severity 3 — hardening and missing tests

- Add an integration test proving the METAR collector prefers epoch `obsTime`, treats documented naive `reportTime` as UTC only in that adapter, and never substitutes collection time.
- Test actual `dataset_quality`, per-scope rolling validation, and holdout scoring rather than only passing dictionaries to `promotion_gates`.
- Exercise concurrent migrations and SQLite busy-timeout behavior.
- Add a `CHECK` constraint for `lead_basis`; prefer a default of `unknown` for both fresh and migrated schemas.
- Ensure BMA cannot become activation-ready unless every required model/lead cohort meets its sample floor and per-model calibration exists. The current global kill switch keeps this dormant for now.

## Reviewed judgment calls

- **Capture-cutoff forecasts:** acceptable as a conservative information cutoff only as a separate model basis. They must not be mixed with run-time leads, and run/sighting identity must first be fixed.
- **Missing declared precision:** do not infer it from city defaults or bracket shape. Add a reviewed metadata override with source provenance instead of guessing.
- **Archive revisioning:** acceptable if every revision remains immutable, manifested, and verifiable.
- **SQLite `busy_timeout=30s`:** reasonable and configurable.
- **One transaction per quote scan:** reasonable because network calls finish before the write transaction. Revisit only if measured lock contention becomes material.
- **`BEGIN IMMEDIATE` in promote/rollback:** safe with the current connection lifecycle.
- **Fresh versus migrated `lead_basis` defaults:** application inserts are explicit and migrated rows are quarantined as `unknown`, but matching safe defaults plus a constraint would be clearer.

## Validation completed during review

- `63` legacy tests passed.
- `32` calibration-ledger tests passed.
- Python compilation passed.
- `git diff --check ec0a149..55747b9` passed.
- Targeted reproductions confirmed:
  - different run times collapse into one forecast snapshot;
  - a wrong station can reconcile as matched;
  - one city's resolution suppresses another unresolved city on the same date;
  - lexicographic timestamp minimization is incorrect with fractional seconds.

## Suggested next-session order

1. Fix snapshot/run identity and decide pooled versus per-model calibration.
2. Fix station-bound reconciliation and unresolved city/date tracking.
3. Fix lead-bucket serving/routing and promotion freshness.
4. Fix resolution-time comparison and archive finalization.
5. Rework completeness, observation coverage, and archive revision manifests.
6. Add the missing regressions, run prospective collection, and keep promotion blocked until the operational acceptance window passes.
