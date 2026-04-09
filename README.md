# Wethr

A weather prediction market trading system for [Polymarket](https://polymarket.com), with a companion divergence-audit pipeline that compares model forecasts against ground-truth station observations.

**Paper trading only by default.** Live trading requires explicit opt-in.

## Repository layout

```
wethr/
├── collector/      # Python trading agent — ensemble forecasts, EMOS calibration,
│                   #   Kelly sizing, paper trading, settlement against NWS/ERA5
├── n8n-wethr/      # n8n workflows — divergence audit between Polymarket and
│                   #   Wethr station observations, exported as JSON for VC
└── data/           # Shared local databases (gitignored)
```

The two halves talk via the shared SQLite database in `data/`: `collector/` writes
trades and observations, and `n8n-wethr/` reads them (mounted read-only into the
n8n container) to produce the divergence audit.

## Sub-projects

- **[collector/](collector/README.md)** — How the trading agent works: ensemble
  fetching, probability estimation, EMOS, BMA, position sizing, settlement.
- **n8n-wethr/** — Docker-compose stack running n8n with the divergence audit
  workflows. Workflow JSON is checked into `n8n-wethr/workflows/`.

## Quick start

```bash
# Trading agent
cd collector
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py diagnose

# Divergence audit (separate terminal)
cd n8n-wethr
docker compose up -d
# open http://localhost:5678
```

## License

MIT — see [LICENSE](LICENSE).
