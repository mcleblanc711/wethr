# 🌡️ Wethr

A weather prediction market trading agent for [Polymarket](https://polymarket.com). Estimates temperature probabilities from multi-model ensemble forecasts and trades mispriced brackets.

**Paper trading only by default.** Live trading requires explicit opt-in after validated performance.

## How it works

Polymarket lists daily "Highest temperature in [City]?" markets with 7-11 brackets (e.g., "72°F - 74°F"). Each bracket trades as a binary contract (YES/NO) on a CLOB order book.

Wethr fetches 109-member ensemble weather forecasts from four models (ECMWF, GEFS, ICON, GEM) via Open-Meteo's free API, estimates the probability of each bracket, and trades when the ensemble disagrees with the market by ≥8%.

```
Ensemble (109 members)  →  Probability per bracket  →  Edge = P(model) - P(market)
                         ↓                            ↓
                    EMOS calibration              Kelly sizing (5% fractional)
                    BMA model weighting           Hard caps ($100/trade, 5% bankroll)
```

## Architecture

```
src/
├── config.py          # Settings, 11 cities, ensemble model config
├── markets.py         # Gamma API discovery, bracket parsing
├── ensemble.py        # Multi-model ensemble fetching (per-model, with retries)
├── probability.py     # Raw ensemble counting → bracket probabilities
├── calibration.py     # EMOS/NGR — correct ensemble under-dispersion
├── bma.py             # Bayesian Model Averaging — skill-weighted models
├── latency.py         # Detect model update shifts for timing edge
├── sizing.py          # Fractional Kelly criterion with hard caps
├── paper_trader.py    # SQLite persistence, Brier score, calibration tracking
├── settlement.py      # NWS/Open-Meteo observations → settle trades
├── trading.py         # Polymarket CLOB API (dry-run default)
├── history.py         # Historical data collection for EMOS training
├── diagnose.py        # API diagnostics (validate before trusting)
└── main.py            # CLI orchestrator
```

## Quick start

```bash
cd collector   # from the repo root

# Create venv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Or use the setup script (does all of the above + runs tests):
# ./setup.sh

# 1. Run diagnostics first — validates each API
python run.py diagnose

# 2. Inspect the calibration ledger (new candidates need prospective history)
python run.py collect-status

# 3. One-shot scan to see current edges
python run.py scan

# 4. Start paper trading loop (scans every 5min, auto-settles daily)
python run.py loop

# 5. Check results
python run.py report
```

Note: `run.py` auto-detects the `.venv` directory and re-execs with the venv Python, so `python3 run.py scan` works even if you forgot to activate.

## CLI reference

| Command | Description |
|---------|-------------|
| `python run.py scan [--cities nyc london]` | Discover markets, find edges |
| `python run.py trade [--cities nyc]` | Scan + place paper trades |
| `python run.py loop [--interval 300]` | Continuous scan/trade/settle loop |
| `python run.py settle [YYYY-MM-DD]` | Settle trades (defaults to yesterday) |
| `python run.py report` | Performance report with Brier score |
| `python run.py pending` | Show open trades |
| `python run.py train --city nyc --days 90` | Legacy audit command; unverified rows are excluded |
| `python run.py train --all` | Legacy audit command; unverified rows are excluded |
| `python run.py emos` | Show trained EMOS parameters |
| `python run.py diagnose` | Validate all API endpoints |
| `python run.py doctor` | Show local DB/export wiring status |
| `python run.py export-settled` | Write settled-trade JSON for n8n |

## Probability estimation (4 phases)

**Phase 1 — Raw ensemble counting** (baseline)
```
P(bracket) = members_in_bracket / total_members
```
With 109 pooled members, resolution is ~0.9% per count.

**Phase 2 — EMOS calibration** (`calibration.py`)
Fits N(μ, σ²) where μ = a + b·mean, σ = c + d·std. Trained by minimizing CRPS on historical data. Corrects ensemble under-dispersion (the #1 source of error in raw counting — tail brackets get underpriced).

**Phase 3 — Forecast latency** (`latency.py`)
Detects when a new model run shifts the ensemble distribution (≥0.8°C in mean). Trades the shift before the market reprices. Window is typically 15-60 minutes.

**Phase 4 — BMA weighting** (`bma.py`)
Weights models by historical CRPS: w_i ∝ exp(-CRPS_i / T). ECMWF typically gets 2-3x the weight of GEM for US cities. Produces a weighted mixture of Gaussians that can represent model disagreement.

## Position sizing

Fractional Kelly at 5% with three caps:

```
kelly = (model_prob - market_price) / (1 - market_price)
size  = kelly × 0.05 × bankroll
size  = min(size, 0.05 × bankroll, $100)
```

Why 5%? Full Kelly assumes perfect probability estimates. At 5%, a 2x overestimate of edge costs much less capital than full Kelly while still ranking larger edges above smaller ones.

## Settlement

Trades settle against authoritative Gamma bracket outcomes. NWS and AviationWeather/METAR station observations provide reconciled exact temperatures; Open-Meteo ERA5 is audit-only. The `loop` command retries every unresolved target date daily, regardless of age.

## Configuration

All settings in `src/config.py`, overridable via `WETHR_` environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `WETHR_DB_PATH` | `../data/wethr.db` | Shared SQLite database path |
| `WETHR_DATA_DIR` | `../data` | Shared data directory |
| `WETHR_MIN_EDGE` | 0.08 | Minimum edge (8%) to trade |
| `WETHR_KELLY_FRAC` | 0.05 | Fractional Kelly multiplier |
| `WETHR_MAX_TRADE` | 100.0 | Max USD per trade |
| `WETHR_MAX_BANK_PCT` | 0.05 | Max % of bankroll per trade |
| `WETHR_BANKROLL` | 10000.0 | Starting paper bankroll |
| `WETHR_DAILY_LOSS` | 300.0 | Daily loss circuit breaker |
| `WETHR_SCAN_INTERVAL` | 300 | Seconds between scans |
| `WETHR_LIVE` | 0 | Set to 1 for live trading |
| `WETHR_TELEGRAM_BOT_TOKEN` | unset | Telegram bot token for new-position alerts |
| `WETHR_TELEGRAM_CHAT_ID` | unset | Telegram chat ID for new-position alerts |
| `WETHR_TELEGRAM_MESSAGE_THREAD_ID` | unset | Optional topic/thread ID for forum chats |

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are also accepted as aliases.

## Live trading

**Disabled by default.** To enable:

```bash
export WETHR_LIVE=1
export POLYMARKET_API_KEY="..."
export POLYMARKET_API_SECRET="..."
export POLYMARKET_PASSPHRASE="..."
```

Pre-live checklist:
- Brier score < 0.20 over 50+ settled signals
- Calibration plot shows no systematic bias
- Paper P&L positive over 2+ weeks
- You've read the code and understand the risk

## Supported cities

| City | Station | Unit | Typical liquidity |
|------|---------|------|-------------------|
| New York | KLGA | °F | $200K-$455K |
| London | EGLC | °C | $100K-$200K |
| Seoul | RKSI | °C | $66K-$150K |
| Chicago | KORD | °F | $50K-$100K |
| Miami | KMIA | °F | $30K-$80K |
| Toronto | CYYZ | °F | $20K-$60K |
| Los Angeles | KLAX | °F | $20K-$50K |
| Denver | KDEN | °F | $15K-$40K |
| Atlanta | KATL | °F | $10K-$30K |
| Dallas | KDFW | °F | $10K-$30K |
| Seattle | KSEA | °F | $10K-$25K |

## Data sources (all free)

| Source | Data | Auth |
|--------|------|------|
| Open-Meteo Ensemble API | 109-member multi-model ensemble forecasts | None |
| Open-Meteo Previous Runs | Verifiable fixed-lead deterministic audit summaries | None |
| NWS + AviationWeather | Open official station observations | None |
| Open-Meteo ERA5 | Secondary gridded audit only | None |
| NWS API | US station observations (settlement) | None |
| Polymarket Gamma API | Market prices + brackets | None |
| Polymarket CLOB API | Order placement | API key |

## Tests

```bash
python tests/test_core.py  # 56 tests, no network required
```

Covers: bracket parsing, ensemble counting, CRPS math, EMOS training, BMA weighting, Kelly criterion, settlement logic, signal/trade dedup, trading client dry-run, latency detection, agent message routing.

## License

MIT

## Calibration-grade ledger

The operational source of truth is now the additive immutable ledger in the
same SQLite database. Legacy `signals`, `trades`, and `historical_*` rows remain
available to old reports, are labeled `legacy-v0` / `legacy_unverified`, and are
excluded from automatic EMOS/BMA training.

```bash
python run.py collect-status
python run.py backfill --from 2026-07-01 --to 2026-07-20
python run.py reconcile --date 2026-07-19
python run.py train-candidate --lead-bucket 24_48h
python run.py evaluate MODEL_VERSION
python run.py promote MODEL_VERSION
python run.py rollback
python run.py archive --month 2026-06
python run.py migrate-legacy --dry-run
```

`promote` changes only the active **paper** model and refuses activation until
all chronological, Gamma-outcome, continuous-truth, completeness, shadow-time,
and segment gates pass. It never enables live trading. Forecast snapshots store
complete member values and verified prospective capture cutoffs; deterministic
Previous Runs summaries are retained for audit but marked untrainable. Gamma is
authoritative for bracket scoring, while NWS/METAR station readings are used for
reconciled continuous targets. Open-Meteo ERA5 remains audit-only.
