# Wethr Operations

## Local Status

From `collector/`:

```bash
python3 run.py doctor
python3 run.py diagnose
python3 run.py report
python3 run.py pending
```

`doctor` is local-only. It reports the active SQLite DB, export file, recent table
activity, and whether a legacy `collector/data/wethr.db` still exists.

## Collector

Paper trading is the default. To run the loop in the foreground:

```bash
cd ~/projects/wethr/collector
python3 run.py loop
```

To run it as a user service:

```bash
mkdir -p ~/.config/systemd/user
cp ~/projects/wethr/deploy/systemd/wethr-collector.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wethr-collector.service
loginctl enable-linger "$USER"
```

Check it with:

```bash
systemctl --user status wethr-collector.service
journalctl --user -u wethr-collector.service -f
```

## Push alerts

New-position alerts push to the ntfy topic in `WETHR_NTFY_TOPIC_URL` (see
`collector/src/ntfy.py`). Set it through a user systemd drop-in or secret
environment file for `wethr-collector.service` and subscribe to the same
topic in the ntfy app; unset, alerts are skipped silently.

## Telegram command bot

ntfy is push-only. To add read-only commands from a
configured Telegram chat (`/positions`, `/pnl`, `/status`, `/help`), install the
separate long-polling service after supplying `WETHR_TELEGRAM_BOT_TOKEN` and
`WETHR_TELEGRAM_CHAT_ID` through a user systemd drop-in or secret environment
file:

```bash
cp ~/projects/wethr/deploy/systemd/wethr-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wethr-telegram.service
```

The service is deliberately read-only and ignores messages from every other
chat. It must be the only webhook/polling consumer for that bot. Enabling it is
an operational action; this repository change does not configure credentials or
start the service.

## n8n Audit

The audit reads `n8n-wethr/wethr-output/settled_trades.json` as
`/data/wethr/settled_trades.json` inside the container. Regenerate it manually:

```bash
cd ~/projects/wethr/collector
python3 run.py export-settled
```

Or install the hourly export timer:

```bash
mkdir -p ~/.config/systemd/user
cp ~/projects/wethr/deploy/systemd/wethr-export.service ~/.config/systemd/user/
cp ~/projects/wethr/deploy/systemd/wethr-export.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wethr-export.timer
```

The checked-in audit workflow polls hourly but its `Audit Due?` gate permits
only one production run per Edmonton calendar day after 09:00. This catches up
after a host/container restart that missed 09:00 without sending hourly
ntfy summaries. `python3 run.py doctor` compares the audit ledger with the
most recent expected 09:00 run; a running container alone is not audit health.

Workflow JSON is source control, not automatic deployment. After changing
`n8n-wethr/workflows/audit.json`, import it into n8n, publish/activate it, and
verify that `Hourly Catch-up Trigger -> Audit Due? -> Start Run` is active.

If the repo was moved, recreate n8n from this checkout so Docker bind mounts point
at the right `wethr-output` directory:

```bash
cd ~/projects/wethr/n8n-wethr
docker compose up -d --force-recreate
```

## Data Notes

The canonical collector DB is `data/wethr.db` at the repo root. Older runs may
have written to `collector/data/wethr.db`; `doctor` reports that separately so it
can be inspected or migrated deliberately.

## Calibration Collection and Archives

From `collector/`, inspect calibration health and reconcile outcomes with:

```bash
python3 run.py collect-status
python3 run.py reconcile
python3 run.py migrate-legacy --dry-run
```

Before an explicit `migrate-legacy --apply`, both SQLite files are copied to
`data/backups/`. The merge is idempotent and legacy history remains quarantined.

Install the daily collection/reconciliation and monthly archive/training timers:

```bash
cp ~/projects/wethr/deploy/systemd/wethr-calibration-* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wethr-calibration-daily.timer
systemctl --user enable --now wethr-calibration-monthly.timer
```

The daily job captures prospective forecasts, then processes at most
`WETHR_DAILY_BACKFILL_LIMIT` due city/date truth grains (60 by default).
It only selects dates whose calendar day has ended in that city's timezone,
prioritizes recent past dates because METAR history expires quickly, persists
provider failures on the retry row, and opens a per-run circuit after a 429 so
one provider cannot abort or flood the remaining batch. Older unresolved items
remain durable for later retries. The daily truth path does not request
untrainable Open-Meteo Previous Runs summaries; use a narrow explicit
`backfill --from ... --to ...` when those audit summaries are needed.

Known prospective-capture outages are declared in
`collector/calibration_exclusions.json`. Training queries reject captures in
those intervals, archive and candidate manifests retain the exclusions, and the
promotion acceptance gate requires a new uninterrupted seven-day window.

The monthly job writes immutable Parquet partitions plus row-count/SHA-256
manifests under `data/archive/YYYY-MM/`, then creates shadow candidates. A
candidate must still be explicitly evaluated and promoted; live trading remains
disabled.
