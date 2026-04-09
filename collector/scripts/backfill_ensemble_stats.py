#!/usr/bin/env python3
"""
Backfill ensemble_mean and ensemble_std for signals that are missing them.

Uses the ensemble API's past_days parameter to fetch recent ensemble data,
then matches it to signals by city/date to populate the stats.

The ensemble API only goes back ~14 days, so this won't cover all historical
signals — but it's the only source of real member-level ensemble statistics
for past dates.

Usage:
    python scripts/backfill_ensemble_stats.py [--past-days 14]
"""
import asyncio
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.ensemble import (
    _build_batch_url,
    _parse_ensemble_response,
    _fetch_with_backoff,
    c_to_f,
    _REQUEST_DELAY,
)
from src.paper_trader import get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")


async def fetch_ensemble_stats_batch(
    client: httpx.AsyncClient,
    city_slugs: list[str],
    past_days: int = 14,
) -> dict[str, dict[date, tuple[float, float]]]:
    """
    Fetch ensemble data for multiple cities using past_days and compute
    mean/std per city/date.

    Returns: city_slug -> date -> (mean_c, std_c)
    """
    cities = [config.CITIES[s] for s in city_slugs if s in config.CITIES]
    if not cities:
        return {}

    results: dict[str, dict[date, tuple[float, float]]] = {}

    for model in config.ENSEMBLE_MODELS:
        lats = ",".join(str(c.lat) for c in cities)
        lons = ",".join(str(c.lon) for c in cities)
        url = (
            f"{config.ENSEMBLE_API_BASE}"
            f"?latitude={lats}&longitude={lons}"
            f"&hourly=temperature_2m"
            f"&models={model}"
            f"&temperature_unit=celsius"
            f"&past_days={past_days}"
            f"&forecast_days=1"
        )

        data = await _fetch_with_backoff(client, url, f"{model} (backfill)")
        if data is None:
            continue

        # Normalize response
        if isinstance(data, dict):
            responses = [data]
        elif isinstance(data, list):
            responses = data
        else:
            continue

        for i, resp_item in enumerate(responses):
            if i >= len(city_slugs):
                break
            slug = city_slugs[i]
            city_cfg = config.CITIES.get(slug)
            tz_name = city_cfg.timezone if city_cfg else "UTC"

            parsed = _parse_ensemble_response(resp_item, slug, model, tz_name=tz_name)
            for d, members in parsed.items():
                temps_c = np.array([m.daily_max for m in members])
                mean_c = float(np.mean(temps_c))
                std_c = float(np.std(temps_c, ddof=1)) if len(temps_c) > 1 else 1.0

                # Accumulate across models
                if slug not in results:
                    results[slug] = {}
                if d in results[slug]:
                    # Average with existing (simple pooling)
                    old_mean, old_std = results[slug][d]
                    results[slug][d] = (
                        (old_mean + mean_c) / 2,
                        (old_std + std_c) / 2,
                    )
                else:
                    results[slug][d] = (mean_c, std_c)

        await asyncio.sleep(_REQUEST_DELAY)

    return results


async def backfill(past_days: int = 14):
    """Backfill ensemble stats for signals missing them."""
    # Find signals that need backfilling
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT city, target_date
            FROM signals
            WHERE ensemble_mean IS NULL
            AND outcome IS NOT NULL
            ORDER BY target_date DESC
        """).fetchall()

    if not rows:
        log.info("No signals need backfilling")
        return

    # Group by city
    city_dates: dict[str, set[date]] = {}
    for r in rows:
        city = r["city"]
        d = date.fromisoformat(r["target_date"])
        city_dates.setdefault(city, set()).add(d)

    total_signals = sum(len(dates) for dates in city_dates.values())
    log.info(f"Need backfill: {total_signals} city/date combos across {len(city_dates)} cities")

    # Determine which dates are within the past_days window
    cutoff = (datetime.now(timezone.utc) - timedelta(days=past_days)).date()
    reachable = {
        city: {d for d in dates if d >= cutoff}
        for city, dates in city_dates.items()
    }
    reachable = {c: d for c, d in reachable.items() if d}
    n_reachable = sum(len(d) for d in reachable.values())
    log.info(f"Reachable via past_days={past_days}: {n_reachable} city/dates")

    if not reachable:
        log.warning("No dates within API range. All signals are too old for ensemble backfill.")
        return

    # Fetch ensemble data
    async with httpx.AsyncClient(
        headers={"User-Agent": config.USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    ) as client:
        city_slugs = list(reachable.keys())
        stats = await fetch_ensemble_stats_batch(client, city_slugs, past_days)

    # Update signals
    updated = 0
    with get_db() as conn:
        for city, date_stats in stats.items():
            for d, (mean_c, std_c) in date_stats.items():
                ds = d.isoformat()
                n = conn.execute("""
                    UPDATE signals
                    SET ensemble_mean = ?, ensemble_std = ?
                    WHERE city = ? AND target_date = ? AND ensemble_mean IS NULL
                """, (round(mean_c, 4), round(std_c, 4), city, ds)).rowcount
                updated += n
                if n > 0:
                    log.info(f"  {city} {ds}: mean={mean_c:.2f}°C std={std_c:.2f}°C ({n} signals)")

    log.info(f"Updated {updated} signals with ensemble stats")

    # Report remaining gaps
    with get_db() as conn:
        remaining = conn.execute("""
            SELECT COUNT(DISTINCT city || target_date) as n
            FROM signals WHERE ensemble_mean IS NULL AND outcome IS NOT NULL
        """).fetchone()["n"]
    if remaining:
        log.info(f"{remaining} city/date combos still missing (older than {past_days} days)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--past-days", type=int, default=14)
    args = parser.parse_args()
    asyncio.run(backfill(args.past_days))
