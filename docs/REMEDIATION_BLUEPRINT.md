# Wethr Remediation Blueprint

Status: planning document; no remediation item is implemented merely because it
appears here.

Last evidence review: 2026-07-26
Repository baseline: `72dc12e`
Working branch at review: `agent/p0-01-recoverable-backups`

## Purpose

This document is the durable program map for turning Wethr into a defensible
research-grade paper-evidence system. It is not a live-trading roadmap.

The program should preserve useful operational data while repairing collection,
provenance, market-price, settlement, evaluation, and workflow defects in small,
reviewable packets.

## Non-negotiable guardrails

- Paper trading only. Live trading, BMA activation, and automatic model promotion
  remain disabled.
- One issue, one branch, and one pull request at a time.
- Before editing, propose the smallest useful version of the selected packet.
- Claude builds; GPT independently reviews the resulting diff and evidence.
- Resolve every Blocker and Major review finding before starting the next packet.
- Do not treat historical paper results as readiness evidence.
- Do not mutate production data, services, timers, containers, or GitHub settings
  merely to validate a proposed design.
- Prefer adaptive/free Open-Meteo usage initially.
- Long-term data policy: immutable archives retained indefinitely and 90 days of
  hot SQLite data.
- Coordination documents explain expectations. Tests, scripts, CI jobs, schema
  constraints, and protected-branch settings provide enforcement.

## Evidence snapshot

The following findings motivated the roadmap. They are a dated audit snapshot,
not permanent truths; remeasure them before relying on exact counts.

- The collector was running in paper mode.
- All 220 recorded collection runs were incomplete, averaging about 70.5%
  executable-book coverage.
- Logs showed 276 scan starts but only 220 collection-run rows. Fifty-six
  Open-Meteo `429` failures disappeared from the coverage denominator.
- Open-Meteo collection ran before CLOB capture, so a weather quota failure could
  prevent market-book collection.
- The primary database was about 233 MiB after roughly three collection days.
- At audit time it contained:
  - 129,525 forecast snapshots
  - 64,173 market snapshots
  - 146,194 predictions
  - zero station observations
  - zero daily observations
  - zero market resolutions
  - zero retry-state rows
- Daily and monthly calibration services used broken direct-script entrypoints.
- Fixing only the import path could unleash roughly 1,600 requests because 759
  city/date items across 53 dates were considered unresolved, including future
  dates.
- Existing paper trades used Gamma `outcomePrices`, not executable CLOB asks and
  depth.
- Decision-time signals were overwritten, so settlement scored the final
  probability instead of the original decision.
- Existing paper results were not readiness evidence:
  - 392 settled trades
  - $31,680.22 staked
  - -$2,869.80 P&L
  - -9.06% ROI
  - model Brier approximately uniform and materially worse than the market
    baseline

## Program dependency map

The numbered packets remain the unit of work. The arrows describe dependency and
do not authorize combining packets into one PR.

```text
Operational safety
  01 backups -> 02 stable entrypoint -> 03 reproducible gate -> 04 migrations

Collection integrity
  04 migrations -> 05 run/source ledger
  05 ledger -> 06 quota control -> 07 Gamma discovery -> 08 completeness
  08 completeness -> 09 bounded outcome queue -> 12 monitoring

Market and decision truth
  05 ledger -> 10 executable CLOB quotes -> 11 fail-closed prediction gates
  10 + 11 -> 12 monitoring

Research-grade evidence
  05 -> 13 registry -> 14 station truth -> 15 resolution semantics
  08 -> 16 forecast normalization -> 17 atomic archives
  13-17 -> 18 n8n semantics -> 19 durable n8n handoff
  14-17 -> 20 statistical evaluation -> 21 economic ledger
  20 + 21 -> 22 cohort gates -> 23 training/serving promotion bundles
```

## P0 — Prevent further evidence loss or silent corruption

### 01. Recoverable backups

Outcome: the active collector and audit SQLite databases can be backed up online,
verified, and restored into a new path.

Smallest useful version:

- Back up `data/wethr.db` and
  `n8n-wethr/wethr-output/wethr_audit.db` using SQLite's online-backup mechanism.
- Write into a new timestamped directory through temporary files and atomic
  publication.
- Produce a compact JSON manifest containing source identity, timestamp, byte
  size, SHA-256, `quick_check`, foreign-key results, and table counts.
- Use `0700` directories and `0600` files.
- Add a restore-test command that refuses to overwrite an existing target.
- Test with temporary WAL databases and deliberately corrupted or incomplete
  input.

Explicit Packet 01 non-goals:

- Docker-volume snapshots
- n8n credential-encryption-key backup
- n8n `nodes/` or `storage/`
- off-host replication
- retention tiers
- timers or services
- backup encryption or remote provisioning

Those belong in Packet 01b after a real secondary filesystem or remote target
exists.

### 02. Stable application entrypoint

Outcome: local commands, tests, and systemd units invoke the same supported module
entrypoint. Direct-script import behavior cannot differ from package execution.

### 03. Reproducible build and test path

Outcome: one documented command runs the same locked environment and checks
locally and in CI. See [WORKFLOW_HARDENING.md](WORKFLOW_HARDENING.md).

Status: the P0-03 source scaffold is implemented by `./scripts/check`, the
`collector/uv.lock`, and the `quality` workflow. Making `quality` required remains a
separate protected-branch activation step after the workflow reaches `main`.

### 04. Versioned migrations

Outcome: ordered migration files replace inline schema evolution. Already-merged
migrations are immutable, new identifiers are unique, and upgrade paths are
tested from representative prior schemas.

### 05. Stage-aware run ledger and source isolation

Outcome: every scan stage is represented, including pre-collection failures.
Weather, Gamma discovery, CLOB capture, prediction, and persistence failures are
separable and cannot silently disappear from denominators.

### 06. Open-Meteo quota controller

Outcome: adaptive request scheduling, bounded retries, cache/reuse, backoff, and
quota telemetry prevent weather calls from starving market capture.

### 07. Complete and fail-closed Gamma discovery

Outcome: pagination and discovery completeness are explicit. Unknown response
shapes, missing pages, and partial market sets fail with actionable evidence.

### 08. Forecast completeness contract

Outcome: expected providers, members, cities, target dates, and lead routes are
defined for one coherent cohort. Missing or unrecognized inputs cannot be counted
as complete.

### 09. Bounded city/date outcome queue

Outcome: only eligible past city/date items enter the outcome queue. Retries are
city/date scoped, rate limited, durable, and never unleashed merely by fixing an
entrypoint.

### 10. Executable CLOB quote truth

Outcome: paper decisions use executable asks, spread, depth, and quote timestamps,
not Gamma display prices.

### 11. Fail-closed prediction gates and legacy quarantine

Outcome: decisions persist their original inputs and probability. Incomplete
cohorts, stale quotes, legacy signals, and unknown model routes cannot become new
readiness evidence.

### 12. Operational monitoring

Outcome: alerts and health views expose stage failures, quota state, coverage,
queue age, stale data, archive verification, and service failures without
silently redefining denominators.

## P1 — Build research-grade truth and evaluation

### 13. Canonical contract and station registry

Version contract identity, city, station, timezone, units, resolution source, and
reviewed overrides.

### 14. Revision-aware station truth quality

Retain provider revisions and evaluate local-day boundary coverage, cadence,
duplicates, gaps, and 23/25-hour DST days.

### 15. Contract rounding and resolution semantics

Represent interval boundaries, declared precision, station choice, rounding
rules, and authoritative bracket outcomes without guessing midpoints.

### 16. Forecast normalization and 90-day hot retention

Normalize payloads and sightings while preserving provider/run identity. Retain
90 days in hot SQLite after verified archival.

### 17. Atomic hash-chained archives

Publish immutable revisions atomically. Every revision retains its own manifest,
and a hash-linked index records supersession without orphaning history.

### 18. Correct n8n audit semantics

Make the audit compare coherent decision-time, resolution, and station-truth
records with explicit missing/unknown states.

### 19. Durable n8n cursor and handoff

Replace fragile file/export assumptions with a durable, replayable cursor and
idempotent handoff.

### 20. Statistical evaluation rewrite

Use chronological splits, decision-time predictions, market baselines,
calibration/reliability analysis, uncertainty, and route/cohort segmentation.

### 21. Economic paper ledger

Model executable price, size, slippage assumptions, capital use, settlement, and
P&L without rewriting historical decisions.

### 22. Cohort-correct acceptance gates

Define minimum sample sizes, completeness, freshness, reconciliation quality,
market comparison, and shadow-time gates over the same eligible cohort.

### 23. Training/serving alignment and atomic promotion bundles

Train, evaluate, route, and serve the same feature distribution. Promotion is an
atomic paper-only bundle and never enables live trading.

## P2 — Reconciliation and operational maturity

- Legacy database reconciliation
- Versioned deployment and security configuration
- Documentation, restore drills, incident runbooks, and recovery evidence

## Model and effort routing

Model availability should be checked when a packet starts. As of the evidence
review:

| Work class | Builder | Independent reviewer |
|---|---|---|
| Routine, tightly scoped implementation | Claude Sonnet 5, `high` | GPT-5.6 Sol, `high` |
| Data-recovery or operational safety | Claude Sonnet 5, `high` | GPT-5.6 Sol, `xhigh` |
| Cross-cutting collection/data-truth work | Claude Opus 5, `xhigh` | GPT-5.6 Sol, `xhigh` |
| Schema, archive, statistical, or promotion work | Claude Fable 5, `xhigh` | GPT-5.6 Sol, `max` |

References:

- [Anthropic models overview](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Anthropic effort guidance](https://platform.claude.com/docs/en/build-with-claude/effort)
- [OpenAI GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model)

Use `max` only for genuinely difficult quality-first review. A higher effort label
does not replace explicit acceptance criteria, failing tests, or evidence.

## Standard packet contract

Every issue should define the following before implementation:

1. **Defect:** the observed failure being closed.
2. **Evidence:** logs, code paths, database observations, or a reproducible test.
3. **Smallest useful outcome:** one sentence describing what becomes reliably
   possible.
4. **In scope:** exact files, commands, and state transitions.
5. **Non-goals:** attractive adjacent work explicitly excluded.
6. **Safety boundary:** production data, network, services, secrets, and external
   settings that must not be changed during development.
7. **Failure contract:** how unknown, incomplete, corrupt, or stale input fails.
8. **Acceptance tests:** including at least one deliberately failing case.
9. **Full gate:** the exact canonical command and real output.
10. **Operational activation:** a separate, explicitly approved step after review.
11. **Builder routing:** Claude model and effort.
12. **Review routing:** GPT model and effort.
13. **Follow-ups:** deferred work that must not leak into the current packet.

## Review and merge contract

- Claude receives the approved packet, implements only that scope, and provides
  real validation output.
- GPT reviews the final diff independently, checks the failure demonstrations,
  and classifies findings as Blocker, Major, Minor, or Note.
- Claude resolves every Blocker and Major.
- GPT re-reviews the resulting head.
- Only then may the branch merge or any operational activation be considered.
- Start the next issue from the updated protected default branch.
