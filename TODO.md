# Wethr TODO: recalibration epoch

Status: **in progress** (written 2026-09-14). §1 code landed on
`agent/train-candidates`; §2–3 follow in separate PRs. The paper-trading
collector keeps running as-is until this is picked up. Live trading is **not**
a goal. Everything here is paper-only, so don't design around live promotion.

## Where things stand (verified 2026-09-14)

Re-verified 2026-09-14 before starting: all facts below still held, except
average win probability (~55%, not ~60%), `missing_resolution_metadata` (251),
and the calibration ntfy drop-ins (already present).

- **Serving model:** `legacy-emos-2026-04-08` (active since 2026-07-21).
  `raw-ensemble-v1` is the only other registered model (shadow). No candidates
  have ever been trained and `model_transitions` is empty.
- **The monthly job has never trained anything.** `scripts/monthly_calibration.py`
  runs `archive_month()` first. On 2026-09-01 that raised `2026-08 is not
  finalizable under matched-only policy: 157 expected city/dates incomplete,
  141 non-matched resolutions`, so `train_candidate()` never ran.
  - 2026-08 has host-offline gaps listed in `collector/calibration_exclusions.json`.
  - `collect-status` reports `missing_resolution_metadata: 248`.
- **Collection is healthy:** forecasts are fresh, the 7-day window has no gaps,
  0 reconciliation discrepancies, about 1.9M forecast snapshots.
- **Ledger (all `legacy-emos-2026-04-08` / `strategy_version='legacy-v0'`):**
  760 trades, 740 settled. **−$10,889 on $56.5k staked (−19% ROI)**, 153W / 587L.
  - The model is overconfident: its average win probability on taken trades is
    ~55%, but it wins ~21%.
  - YES long shots are the worst segment: avg entry $0.12, 9% hit rate, −$7.4k.
  - `bankroll_adjustment` = 2695.72, set by `scripts/reset_bankroll.py` on
    2026-04-08.
- **Open-position cap:** `MAX_PENDING_TRADES` (`WETHR_MAX_PENDING`, default 20,
  `src/config.py:198`, enforced in `src/sizing.py:127`). The cap binds: 20 new
  positions on each of 09-05, 09-09 and 09-14.

## 1. Train candidates now and keep them in shadow

Goal: fresh per-lead-bucket models exist and accumulate out-of-sample shadow
history, without waiting for a clean monthly archive.

- [ ] **Ops (needs approval, writes `model_versions`):**
  `python3 run.py train-candidate --lead-bucket <b>` for each of `LEAD_BUCKETS`
  (`src/calibration_ops.py:54`: `in_day, 0_24h, 24_48h, 48_72h, 72h_plus`).
  `train_candidate()` (`calibration_ops.py:1083`) does not need an archive. It
  uses a trailing 365-day window and honours the exclusions file.
- [ ] Then run `python3 run.py evaluate <id>` on each candidate against
  `legacy-emos-2026-04-08` and `raw-ensemble-v1`. Record Brier, CRPS, and
  reliability by probability band. The overconfidence found above is the thing
  to check.
- [x] **Fix `monthly_calibration.py`:** archive, per-bucket training, and an
  immediate evaluate (starting the shadow clock) each run independently; one
  alert lists every failure and the unit exits non-zero. The daily job's
  failure alert is now in the mutable `calibration` category too.
- [x] **Ops:** `WETHR_NTFY_TOPIC_URL` is already set in the
  `wethr-calibration-{daily,monthly}.service` drop-ins (checked 2026-09-14).
- **Paper promotion:** `promote_model()` (`calibration_ops.py:1433`) applies
  strict gates: 28 shadow days, 60 city-days, 95% completeness, and beating
  both raw and control. Those gates were written with live trading in mind.
  For a paper-only epoch, decide whether to:
  - (a) wait out the gates,
  - (b) add an explicit `--paper-override` that records the failed gates in
    `model_transitions.gate_report_json`, or
  - (c) run the new epoch on a candidate without promoting it.

  Keep the audit trail whichever you choose.
- [x] **Chosen: (b).** `promote MODEL --paper-override REASON` promotes despite
  failed gates, stores `paper_override: {applied, reason, failed_gates}` in
  `gate_report_json`, and is refused when `WETHR_LIVE=1`.
- **Known data-quality caveats that affect gate numbers:** see
  `CALIBRATION_REVIEW_FOLLOWUPS.md` (cohort-mixing in `dataset_quality()`, and
  observation coverage with no dedupe or gap rule).

## 2. Epoch-split P/L ("pre-calibration" vs current)

Goal: start a new strategy epoch without deleting anything. Lifetime P/L stays
visible as "pre-calibration", and the new epoch gets its own P/L and bankroll.

- The columns already exist on `trades`: `model_version_id`, and
  `strategy_version TEXT NOT NULL DEFAULT 'legacy-v0'`.
  - `record_paper_trade()` (`src/paper_trader.py:205`) **hardcodes**
    `'legacy-v0'` in the INSERT and falls back to `legacy-emos-2026-04-08`.
- Add `WETHR_STRATEGY_VERSION` (or a `settings` row, `current_strategy_epoch`)
  and write it on every new trade.
  - A settings row can be switched without a restart. Recommended; the Telegram
    bot already writes settings for mutes.
  - Keep a record of when each epoch started, its label, and the starting
    bankroll.
- `get_stats()` (`paper_trader.py:527`) takes an optional `strategy_version`
  filter. Bankroll per epoch = epoch starting bankroll + epoch P/L. Replace or
  generalize the global `bankroll_adjustment` approach. Sizing must use the
  **current epoch's** bankroll.
- **Surfaces:**
  - Telegram `/pnl` shows the current epoch first, then "Pre-calibration
    (legacy-v0): −$10,889 …", then all-time.
  - Optional `/pnl all` for a per-epoch table.
  - ntfy settlement push (`src/ntfy.py` `build_trade_settled_message`, caller
    `notify_settlements` in `src/main.py`): the lifetime line becomes the epoch
    line.
  - `print_report`, `export-settled` (n8n) and the Signal Ledger export may
    want a `strategy_version` field. Check `signal-ledger/` before changing
    export shapes.
- Tests: epoch switch mid-ledger, per-epoch stats, and sizing using epoch
  bankroll.

## 3. Best-first slot filling

Goal: when slots are limited, open positions on the strongest signals, not
whichever market happened to be scanned first.

- **Current behaviour:** `scan_and_trade()` (`src/main.py:123`) loops
  `for market in markets` → `for bp in mp.brackets` (~lines 176–240). It sizes
  each edge with `size_position(bp, bankroll, daily_pnl, pending)` and records
  it immediately. Once `pending >= MAX_PENDING_TRADES`, sizing rejects
  everything else, so slots go first-come in market-discovery order.
- **Change:** do it in two passes.
  1. Evaluate every market and bracket (record signals as today), then collect
     candidates that pass the edge threshold and `ps.is_valid`, ignoring the
     pending count.
  2. Rank them, then record trades best-first until the cap or the daily loss
     limit is reached.
- **Ranking key to decide:** candidates are `|edge|`, expected value per
  dollar (`model_prob*win_pnl + (1-model_prob)*loss_pnl) / size`), or
  Kelly fraction. Prefer EV per dollar or Kelly; raw edge favours cheap long
  shots, which is where the losses are.
- **Also consider:**
  - at most N positions per city/date (correlated brackets on the same market);
  - a minimum entry price or a YES long-shot filter as a strategy parameter
    for the new epoch;
  - the ordering must stay deterministic for tests.
- `pending_count` in the new-position ntfy push must still be accurate.
- The live-trade branch (`trading_client.is_live`) stays in the same loop, but
  live is off. Keep the behaviour identical apart from ordering.
- Tests: with the cap at 2 and 5 candidates, the top 2 by the ranking key
  are recorded, regardless of market order.

## 4. Raise the open-position cap

- `WETHR_MAX_PENDING` is already configurable. No code change is needed: set
  it in the collector unit or drop-in and restart.
- **Do it after 2 and 3 land**, and ideally once an evaluated model shows
  better calibration. With the current −19% ROI, more slots just means losing
  faster.
- **Check what else binds once the cap rises:**
  - `DAILY_LOSS_LIMIT` ($300) is evaluated on realized daily P/L, but losses
    settle the next day, so it barely limits exposure;
  - per-trade caps: `MAX_TRADE_SIZE_USD` $100 and `MAX_BANKROLL_PCT` 5%;
  - consider a total open-stake cap (e.g. ≤ X% of epoch bankroll) instead of,
    or as well as, a count cap.

## Suggested order

1 (train + fix monthly job + ntfy env) → 2 (epoch) → 3 (best-first) → start
the new epoch → 4 (raise cap, once the numbers justify it).

---

## Prompt for a new session

Paste this into a fresh Claude Code session started in `~/projects/wethr`:

```
Read TODO.md at the repo root, then OPERATIONS.md, collector/README.md, and
CALIBRATION_REVIEW_FOLLOWUPS.md. This is a paper-trading-only Polymarket
weather collector; live trading is not a goal, so do not optimize for live
promotion gates.

Work through TODO.md sections 1–3 (train shadow candidates and fix the
monthly job, epoch-split P/L, best-first slot filling). Section 4 (raising
WETHR_MAX_PENDING) is config-only and should wait until I ask.

Start in plan mode. Before planning:
- Re-verify every "verified 2026-09-14" fact in TODO.md against the live
  ledger (read-only: `sqlite3 -readonly data/wethr.db`; the DB is ~4 GB and
  shared with a running collector, so never copy or VACUUM it) and against
  `python3 run.py collect-status` from collector/.
- Check `git log` since 2026-09-14 for anything that already landed.

Constraints:
- The systemd collector (`wethr-collector.service`) runs whatever branch is
  checked out in ~/projects/wethr. Work on a new branch off main, and ask
  before restarting any service.
- Never delete or rewrite historical trades; epochs are additive.
- Quality gate: `./scripts/check` (slow, ~7 min; run it once at the end,
  targeted pytest files while iterating).
- Training/evaluating candidates writes model_versions rows — ask before
  running train-candidate or promote against the live DB.

Deliver one PR per section (or one PR for 2+3 if they're tightly coupled),
each with tests, and update TODO.md to mark what's done.
```
