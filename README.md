# Wethr

A weather prediction market trading system for [Polymarket](https://polymarket.com), with a companion divergence-audit pipeline that compares model forecasts against ground-truth station observations.

**Paper trading only by default.** Live trading requires explicit opt-in.

## Repository layout

```
wethr/
├── collector/      # Python trading agent — ensemble forecasts, EMOS calibration,
│                   #   Kelly sizing, paper trading, Gamma settlement, station reconciliation
├── n8n-wethr/      # n8n workflows — divergence audit between Polymarket and
│                   #   Wethr station observations, exported as JSON for VC
└── data/           # Shared local databases (gitignored)
```

The two halves talk via the shared SQLite database in `data/wethr.db`:
`collector/` writes trades and observations, and `n8n-wethr/` reads it
(mounted read-only into the n8n container) to produce the divergence audit.

## Sub-projects

- **[collector/](collector/README.md)** — How the trading agent works: ensemble
  fetching, probability estimation, EMOS, BMA, position sizing, settlement.
- **n8n-wethr/** — Docker-compose stack running n8n with the divergence audit
  workflows. Workflow JSON is checked into `n8n-wethr/workflows/`.

## Quick start

```bash
# Reproduce the complete local quality gate (requires uv 0.11.32,
# CPython 3.12.13, Docker Compose 2.40.3, Git, and systemd 255)
./scripts/check

# Trading agent
cd collector
uv sync --locked
uv run python run.py diagnose
uv run python run.py doctor

# Divergence audit (separate terminal)
cd ../n8n-wethr
docker compose up -d
# open http://localhost:5678
```

`./scripts/check` is the command used by CI. It synchronizes the locked runtime
and development dependencies, disables network socket access during pytest, and
directs all Python gate data to a temporary directory. It does not start the
collector, containers, services, or timers.

`uv run python run.py export-settled` writes
`n8n-wethr/wethr-output/settled_trades.json`, the file consumed by the audit
workflow. `uv run python run.py doctor` reports the DB path, recent table
activity, export status, latest expected/successful n8n audit run, explicit
capture-gap eligibility, and any legacy `collector/data` database still present.

## License

MIT — see [LICENSE](LICENSE).
