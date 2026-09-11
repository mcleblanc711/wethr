# Note: pull `main` before starting work here

This branch (`agent/observation-coverage-checks`) was cut from `main` at
`bde9fc6` on 2026-09-11, at the same time as `agent/dataset-completeness-cohort`
(the other still-open item from `CALIBRATION_REVIEW_FOLLOWUPS.md`).

Before doing any work here: `git fetch origin && git rebase origin/main` (or
merge, whichever this repo's convention favors at the time). If
`agent/dataset-completeness-cohort` has already landed on `main`, rebasing
picks up its changes to `collector/src/calibration_ops.py` — this branch's
target (`aggregate_daily_observation` in `collector/src/ledger.py`) is a
different file, so a conflict is unlikely, but the two fixes are close
enough in the calibration pipeline that it's worth starting from current
`main` rather than this stale branch point regardless.

Delete this file once you've rebased and are ready to start; it's not part
of the actual fix.
