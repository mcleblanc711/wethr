"""Legacy historical reporting compatibility.

The historical tables and command surface are retained for audit reports only.
Their forecast issue times and station truth provenance are not sufficient for
automatic calibration, so all rows are quarantined. New collection and training
live in :mod:`src.calibration_ops` and the calibration ledger.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np

from . import config
from .ensemble import c_to_f
from .calibration import (
    TrainingData,
    EMOSParams,
    train_emos,
    cross_validate_emos,
    save_emos_params,
    load_emos_params,
)
from .paper_trader import get_db, init_db

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database schema for historical data
# ---------------------------------------------------------------------------

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    lead_days INTEGER NOT NULL,
    model TEXT NOT NULL,
    ens_mean_c REAL NOT NULL,
    ens_std_c REAL NOT NULL,
    ens_min_c REAL NOT NULL,
    ens_max_c REAL NOT NULL,
    n_members INTEGER NOT NULL,
    quality_status TEXT NOT NULL DEFAULT 'legacy_unverified',
    training_eligible INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(city, target_date, lead_days, model)
);

CREATE TABLE IF NOT EXISTS historical_observations (
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    observed_max_c REAL NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (city, target_date)
);

CREATE INDEX IF NOT EXISTS idx_hist_fc_city_date
    ON historical_forecasts(city, target_date);
"""


def init_history_db(db_path: Path | None = None) -> None:
    """Initialize historical data tables."""
    with get_db(db_path) as conn:
        conn.executescript(_HISTORY_SCHEMA)


# ---------------------------------------------------------------------------
# Fetch historical observations
# ---------------------------------------------------------------------------

async def fetch_historical_observations(
    client: httpx.AsyncClient,
    city_slug: str,
    start_date: date,
    end_date: date,
    db_path: Path | None = None,
) -> int:
    """
    Fetch observed daily max temperatures for a date range.
    
    Uses Open-Meteo Historical Weather API (ERA5 reanalysis).
    Stores results in SQLite, returns count of new records.
    """
    city = config.CITIES.get(city_slug)
    if not city:
        raise ValueError(f"Unknown city: {city_slug}")

    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={city.lat}&longitude={city.lon}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=celsius"
        f"&timezone={city.timezone}"
        f"&start_date={start_date.isoformat()}"
        f"&end_date={end_date.isoformat()}"
    )

    try:
        resp = await client.get(url, timeout=60)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.error(f"Historical obs fetch failed for {city.name}: {e}")
        return 0

    data = resp.json()
    daily = data.get("daily", {})
    times = daily.get("time", [])
    temps = daily.get("temperature_2m_max", [])

    if not times:
        log.warning(f"No historical obs for {city.name}")
        return 0

    count = 0
    with get_db(db_path) as conn:
        conn.executescript(_HISTORY_SCHEMA)
        for i, date_str in enumerate(times):
            if i >= len(temps) or temps[i] is None:
                continue
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO historical_observations
                        (city, target_date, observed_max_c, source)
                    VALUES (?, ?, ?, 'open-meteo-era5')
                    """,
                    (city_slug, date_str, float(temps[i])),
                )
                count += 1
            except Exception:
                pass

    log.info(f"Stored {count} observations for {city.name} ({start_date} → {end_date})")
    return count


# ---------------------------------------------------------------------------
# Fetch historical ensemble forecasts
# ---------------------------------------------------------------------------

async def fetch_historical_ensemble(
    client: httpx.AsyncClient,
    city_slug: str,
    start_date: date,
    end_date: date,
    model: str = "ecmwf_ifs025_ensemble",
    db_path: Path | None = None,
) -> int:
    """Deprecated unsafe collector retained only for API compatibility.

    Historical Forecast responses do not prove a fixed model issue time and a
    deterministic response is not an ensemble. Use
    ``calibration_ops.collect_previous_run_summaries`` for verifiable,
    explicitly untrainable fixed-lead summaries, or prospective Ensemble API
    snapshots for member-level calibration data.
    """
    del client, start_date, end_date
    if city_slug not in config.CITIES:
        raise ValueError(f"Unknown city: {city_slug}")
    with get_db(db_path) as conn:
        conn.executescript(_HISTORY_SCHEMA)
    log.warning(
        "Skipping legacy Historical Forecast collector for %s/%s; "
        "unverified issue times and deterministic values are not trainable",
        city_slug,
        model,
    )
    return 0


async def fetch_historical_ensemble_members(
    client: httpx.AsyncClient,
    city_slug: str,
    start_date: date,
    end_date: date,
    db_path: Path | None = None,
) -> int:
    """Deprecated look-back member fetch; prospective snapshots replace it."""
    del client, start_date, end_date, db_path
    if city_slug not in config.CITIES:
        raise ValueError(f"Unknown city: {city_slug}")
    log.warning(
        "Skipping look-back member fetch for %s; members first seen after their "
        "target cutoff cannot be used for calibration",
        city_slug,
    )
    return 0


# ---------------------------------------------------------------------------
# Build training dataset from stored history
# ---------------------------------------------------------------------------

def build_training_data(
    city_slug: str,
    unit: str = "C",
    db_path: Path | None = None,
) -> TrainingData:
    """
    Build EMOS training dataset by joining historical forecasts with observations.
    
    For each (city, date) where we have both a forecast and observation,
    creates a training sample.
    
    Args:
        city_slug: City to build data for
        unit: "C" for Celsius, "F" for Fahrenheit
    """
    with get_db(db_path) as conn:
        conn.executescript(_HISTORY_SCHEMA)

        # Join forecasts with observations
        # Average across models for each date (pooled ensemble stats)
        rows = conn.execute(
            """
            SELECT 
                f.target_date,
                AVG(f.ens_mean_c) as ens_mean,
                -- Pool std: use root-mean-square of individual stds
                -- This is approximate but reasonable for training
                AVG(f.ens_std_c) as ens_std,
                SUM(f.n_members) as total_members,
                o.observed_max_c
            FROM historical_forecasts f
            JOIN historical_observations o
                ON f.city = o.city AND f.target_date = o.target_date
            WHERE f.city = ?
              AND COALESCE(f.quality_status, 'legacy_unverified') = 'ok'
              AND COALESCE(f.training_eligible, 0) = 1
            GROUP BY f.target_date
            ORDER BY f.target_date
            """,
            (city_slug,),
        ).fetchall()

    if not rows:
        log.warning(f"No training data for {city_slug}")
        return TrainingData(
            ens_means=np.array([]),
            ens_stds=np.array([]),
            observations=np.array([]),
        )

    ens_means = []
    ens_stds = []
    observations = []

    for row in rows:
        mean_c = row["ens_mean"]
        std_c = max(row["ens_std"], 0.5)  # Floor std at 0.5°C
        obs_c = row["observed_max_c"]

        if unit == "F":
            mean_val = c_to_f(mean_c)
            # Convert std: Δ°F = Δ°C * 9/5
            std_val = std_c * 9.0 / 5.0
            obs_val = c_to_f(obs_c)
        else:
            mean_val = mean_c
            std_val = std_c
            obs_val = obs_c

        ens_means.append(mean_val)
        ens_stds.append(std_val)
        observations.append(obs_val)

    data = TrainingData(
        ens_means=np.array(ens_means),
        ens_stds=np.array(ens_stds),
        observations=np.array(observations),
    )

    log.info(
        f"Training data for {city_slug}: {data.n} samples, "
        f"mean obs={np.mean(data.observations):.1f}°{unit}"
    )
    return data


# ---------------------------------------------------------------------------
# Full training pipeline
# ---------------------------------------------------------------------------

async def collect_and_train(
    client: httpx.AsyncClient,
    city_slug: str,
    lookback_days: int = 90,
    db_path: Path | None = None,
) -> EMOSParams | None:
    """
    Full pipeline: collect historical data → build training set → train EMOS.
    """
    city = config.CITIES.get(city_slug)
    if not city:
        raise ValueError(f"Unknown city: {city_slug}")

    end_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    start_date = end_date - timedelta(days=lookback_days)

    log.info(f"Collecting historical data for {city.name} ({start_date} → {end_date})")

    # Fetch observations
    await fetch_historical_observations(client, city_slug, start_date, end_date, db_path)

    # Fetch deterministic historical forecasts (longer range)
    await fetch_historical_ensemble(client, city_slug, start_date, end_date, db_path=db_path)

    # Fetch recent ensemble-member forecasts (higher quality, shorter range)
    await fetch_historical_ensemble_members(client, city_slug, start_date, end_date, db_path)

    # Build training data in the unit the market uses
    data = build_training_data(city_slug, unit=city.temp_unit, db_path=db_path)

    if data.n < 10:
        log.warning(f"Insufficient training data for {city.name} ({data.n} samples)")
        return None

    # Train with cross-validation
    params, cv_crps = cross_validate_emos(data, city=city_slug)

    # Save
    save_emos_params(params, db_path)

    return params


async def train_all_cities(
    lookback_days: int = 90,
    db_path: Path | None = None,
) -> dict[str, EMOSParams]:
    """Train EMOS for all configured cities."""
    results = {}

    async with httpx.AsyncClient(
        headers={"User-Agent": config.USER_AGENT},
        timeout=60,
    ) as client:
        for slug in config.CITIES:
            try:
                params = await collect_and_train(
                    client, slug, lookback_days, db_path
                )
                if params:
                    results[slug] = params
            except Exception as e:
                log.error(f"Training failed for {slug}: {e}", exc_info=True)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Wethr historical data & EMOS training")
    sub = parser.add_subparsers(dest="command")

    p_collect = sub.add_parser("collect", help="Collect historical forecast/observation data")
    p_collect.add_argument("--city", required=True, help="City slug")
    p_collect.add_argument("--days", type=int, default=90, help="Lookback days")

    p_train = sub.add_parser("train", help="Train EMOS calibration")
    p_train.add_argument("--city", help="City slug (or --all)")
    p_train.add_argument("--all", action="store_true", help="Train all cities")
    p_train.add_argument("--days", type=int, default=90, help="Lookback days")

    p_report = sub.add_parser("report", help="Show EMOS parameters for all cities")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    init_db()
    init_history_db()

    if args.command == "collect":
        async def _run():
            async with httpx.AsyncClient(
                headers={"User-Agent": config.USER_AGENT}, timeout=60,
            ) as client:
                end = (datetime.now(timezone.utc) - timedelta(days=1)).date()
                start = end - timedelta(days=args.days)
                await fetch_historical_observations(client, args.city, start, end)
                await fetch_historical_ensemble(client, args.city, start, end)
                await fetch_historical_ensemble_members(client, args.city, start, end)
        asyncio.run(_run())

    elif args.command == "train":
        if args.all:
            results = asyncio.run(train_all_cities(args.days))
            print(f"\nTrained EMOS for {len(results)} cities:")
            for slug, params in results.items():
                print(
                    f"  {slug}: μ = {params.a:.3f} + {params.b:.3f}*mean, "
                    f"σ = {params.c:.3f} + {params.d:.3f}*std, "
                    f"CRPS = {params.crps_train:.4f} (n={params.n_training})"
                )
        elif args.city:
            async def _run():
                async with httpx.AsyncClient(
                    headers={"User-Agent": config.USER_AGENT}, timeout=60,
                ) as client:
                    return await collect_and_train(client, args.city, args.days)
            params = asyncio.run(_run())
            if params:
                print(
                    f"\nEMOS for {args.city}: "
                    f"μ = {params.a:.3f} + {params.b:.3f}*mean, "
                    f"σ = {params.c:.3f} + {params.d:.3f}*std"
                )
        else:
            print("Specify --city or --all")

    elif args.command == "report":
        from .calibration import load_all_emos_params
        params_dict = load_all_emos_params()
        if not params_dict:
            print("No EMOS parameters trained yet.")
        else:
            print(f"\n{'City':<15} {'a':>8} {'b':>8} {'c':>8} {'d':>8} {'CRPS':>8} {'CV':>8} {'n':>6}")
            print("-" * 80)
            for slug, p in sorted(params_dict.items()):
                print(
                    f"{slug:<15} {p.a:>8.3f} {p.b:>8.3f} "
                    f"{p.c:>8.3f} {p.d:>8.3f} "
                    f"{p.crps_train:>8.4f} {p.crps_test:>8.4f} {p.n_training:>6d}"
                )


if __name__ == "__main__":
    main()
