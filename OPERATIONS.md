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

The daily job retries every unresolved target date regardless of age. The
monthly job writes immutable Parquet partitions plus row-count/SHA-256 manifests
under `data/archive/YYYY-MM/`, then creates shadow candidates. A candidate must
still be explicitly evaluated and promoted; live trading remains disabled.
