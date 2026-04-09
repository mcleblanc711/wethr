#!/usr/bin/env python3
"""
Train EMOS using settled signals that have ensemble_mean, ensemble_std,
and resolved_value populated.

This supplements the history.py pipeline by using the signals table as a
training data source. Signals have real ensemble statistics from the live
multi-model ensemble (109+ members), not the fake std=2.0 approximation
from the deterministic historical forecast API.

Supports city grouping to pool samples across similar cities when
per-city data is too sparse for reliable EMOS coefficients.

Usage:
    python scripts/train_emos_from_signals.py [--min-samples 10] [--grouped]
"""
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.calibration import (
    TrainingData,
    EMOSParams,
    train_emos,
    cross_validate_emos,
    save_emos_params,
    mean_crps,
    crps_gaussian,
)
from src.ensemble import c_to_f
from src.paper_trader import get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("emos-signals")


# ---------------------------------------------------------------------------
# City grouping for pooled EMOS training
# ---------------------------------------------------------------------------

CITY_GROUPS = {
    "fahrenheit_us": [
        "nyc", "chicago", "miami", "atlanta", "dallas",
        "seattle", "denver", "los_angeles",
    ],
    "celsius_minimal_bias": [
        "london", "toronto", "sao_paulo", "seoul",
    ],
    "celsius_cool_bias": [
        "paris", "tel_aviv", "buenos_aires",
    ],
}

def get_city_group(city: str) -> str:
    for group, cities in CITY_GROUPS.items():
        if city in cities:
            return group
    return "unknown"


# ---------------------------------------------------------------------------
# Extract training data from signals
# ---------------------------------------------------------------------------

def extract_signal_training_data(
    cities: list[str] | None = None,
    unit: str = "C",
) -> dict[str, TrainingData]:
    """
    Extract EMOS training data from settled signals.

    For each city/date combo with both ensemble stats and resolved_value,
    creates one training sample using the unique (ensemble_mean, ensemble_std)
    for that city/date and the resolved_value as the observation.

    Since resolved_value is stored in the market's unit (°F or °C) and
    ensemble_mean/std are always in °C, we convert to a common unit.

    Returns training data keyed by city slug.
    """
    with get_db() as conn:
        query = """
            SELECT city, target_date,
                   ensemble_mean, ensemble_std, resolved_value
            FROM signals
            WHERE ensemble_mean IS NOT NULL
            AND ensemble_std IS NOT NULL
            AND resolved_value IS NOT NULL
            AND outcome IS NOT NULL
            GROUP BY city, target_date
            ORDER BY city, target_date
        """
        rows = conn.execute(query).fetchall()

    # Group by city
    city_data: dict[str, list[tuple[float, float, float]]] = defaultdict(list)

    for r in rows:
        city = r["city"]
        if cities and city not in cities:
            continue

        ens_mean_c = r["ensemble_mean"]
        ens_std_c = r["ensemble_std"]
        resolved = r["resolved_value"]

        city_cfg = config.CITIES.get(city)
        if not city_cfg:
            continue

        # Convert ensemble stats to the requested unit
        if unit == "F":
            ens_mean = c_to_f(ens_mean_c)
            ens_std = ens_std_c * 9.0 / 5.0
        else:
            ens_mean = ens_mean_c
            ens_std = ens_std_c

        # Convert resolved_value to the requested unit
        # resolved_value is stored in the market's unit
        if unit == "F" and city_cfg.temp_unit == "C":
            obs = c_to_f(resolved)
        elif unit == "C" and city_cfg.temp_unit == "F":
            obs = (resolved - 32.0) * 5.0 / 9.0
        else:
            obs = resolved

        city_data[city].append((ens_mean, ens_std, obs))

    result = {}
    for city, samples in city_data.items():
        if not samples:
            continue
        means, stds, obs = zip(*samples)
        result[city] = TrainingData(
            ens_means=np.array(means),
            ens_stds=np.array(stds),
            observations=np.array(obs),
        )

    return result


def extract_combined_training_data(
    cities: list[str] | None = None,
    unit: str = "C",
) -> dict[str, TrainingData]:
    """
    Combine signal-based and history-based training data.

    Signal data: real ensemble stats from live multi-model ensemble.
    History data: from historical_forecasts + historical_observations tables.

    Returns training data keyed by city slug.
    """
    # Signal-based data
    signal_data = extract_signal_training_data(cities, unit)

    # History-based data (from history.py pipeline)
    from src.history import build_training_data
    history_data = {}
    for slug in (cities or list(config.CITIES.keys())):
        city_cfg = config.CITIES.get(slug)
        if not city_cfg:
            continue
        td = build_training_data(slug, unit=unit)
        if td.n > 0:
            history_data[slug] = td

    # Merge: signal data takes priority, history fills gaps
    combined = {}
    all_cities = set(list(signal_data.keys()) + list(history_data.keys()))

    for city in all_cities:
        if cities and city not in cities:
            continue

        sig = signal_data.get(city)
        hist = history_data.get(city)

        if sig and hist:
            combined[city] = TrainingData(
                ens_means=np.concatenate([sig.ens_means, hist.ens_means]),
                ens_stds=np.concatenate([sig.ens_stds, hist.ens_stds]),
                observations=np.concatenate([sig.observations, hist.observations]),
            )
        elif sig:
            combined[city] = sig
        elif hist:
            combined[city] = hist

    return combined


# ---------------------------------------------------------------------------
# Grouped training
# ---------------------------------------------------------------------------

def train_grouped(
    data_by_city: dict[str, TrainingData],
    min_samples: int = 10,
) -> dict[str, EMOSParams]:
    """
    Train EMOS with city grouping.

    1. Try per-city training if enough samples (>= min_samples * 3)
    2. Fall back to group-level training
    3. Fall back to global training
    """
    results = {}

    # Pool data by group
    group_data: dict[str, list[TrainingData]] = defaultdict(list)
    for city, td in data_by_city.items():
        group = get_city_group(city)
        group_data[group].append(td)

    # Train per group
    group_params: dict[str, EMOSParams] = {}
    for group_name, td_list in group_data.items():
        pooled = TrainingData(
            ens_means=np.concatenate([td.ens_means for td in td_list]),
            ens_stds=np.concatenate([td.ens_stds for td in td_list]),
            observations=np.concatenate([td.observations for td in td_list]),
        )

        if pooled.n >= min_samples:
            log.info(f"Training group '{group_name}': {pooled.n} samples")
            params, cv_crps = cross_validate_emos(pooled, city=group_name)
            group_params[group_name] = params
            log.info(
                f"  Group {group_name}: μ = {params.a:.3f} + {params.b:.3f}*mean, "
                f"σ = {params.c:.3f} + {params.d:.3f}*std, "
                f"CRPS = {params.crps_train:.4f} (n={params.n_training})"
            )
        else:
            log.warning(f"Group '{group_name}' has only {pooled.n} samples — skipping")

    # Global fallback
    all_td = list(data_by_city.values())
    if all_td:
        global_data = TrainingData(
            ens_means=np.concatenate([td.ens_means for td in all_td]),
            ens_stds=np.concatenate([td.ens_stds for td in all_td]),
            observations=np.concatenate([td.observations for td in all_td]),
        )
        if global_data.n >= min_samples:
            log.info(f"Training global model: {global_data.n} samples")
            global_params = train_emos(global_data, city="global")
            log.info(
                f"  Global: μ = {global_params.a:.3f} + {global_params.b:.3f}*mean, "
                f"σ = {global_params.c:.3f} + {global_params.d:.3f}*std, "
                f"CRPS = {global_params.crps_train:.4f}"
            )

    # Assign params to each city: per-city > group > global
    for city, td in data_by_city.items():
        group = get_city_group(city)

        # Try per-city if enough data
        if td.n >= min_samples * 3:
            log.info(f"Training per-city for {city}: {td.n} samples")
            params, _ = cross_validate_emos(td, city=city)
            results[city] = params
        elif group in group_params:
            # Use group params, but set city name
            gp = group_params[group]
            results[city] = EMOSParams(
                a=gp.a, b=gp.b, c=gp.c, d=gp.d,
                city=city,
                n_training=gp.n_training,
                crps_train=gp.crps_train,
                crps_test=gp.crps_test,
            )
            log.info(f"  {city}: using group '{group}' params")
        else:
            log.warning(f"  {city}: no group params available, skipping")

    return results


# ---------------------------------------------------------------------------
# Diagnostic output
# ---------------------------------------------------------------------------

def print_diagnostics(
    data_by_city: dict[str, TrainingData],
    params_by_city: dict[str, EMOSParams],
):
    """Print diagnostic summary comparing raw vs EMOS predictions."""
    print(f"\n{'City':<15} {'n':>4} {'Raw CRPS':>10} {'EMOS CRPS':>10} {'Improve':>8} "
          f"{'a':>7} {'b':>7} {'c':>7} {'d':>7} {'bias':>7}")
    print("-" * 95)

    for city in sorted(data_by_city.keys()):
        td = data_by_city[city]
        params = params_by_city.get(city)
        if not params:
            continue

        raw_crps = mean_crps(
            (0.0, 1.0, 0.0, 1.0),
            td.ens_means, td.ens_stds, td.observations,
        )
        emos_crps = mean_crps(
            (params.a, params.b, params.c, params.d),
            td.ens_means, td.ens_stds, td.observations,
        )
        improve = (raw_crps - emos_crps) / raw_crps * 100 if raw_crps > 0 else 0

        # Compute mean bias (predicted - observed)
        bias = np.mean([
            params.a + params.b * m - o
            for m, o in zip(td.ens_means, td.observations)
        ])

        print(f"{city:<15} {td.n:>4} {raw_crps:>10.3f} {emos_crps:>10.3f} "
              f"{improve:>7.1f}% {params.a:>7.3f} {params.b:>7.3f} "
              f"{params.c:>7.3f} {params.d:>7.3f} {bias:>+7.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-samples", type=int, default=10)
    parser.add_argument("--grouped", action="store_true",
                        help="Use city grouping for pooled training")
    parser.add_argument("--unit", default="C", choices=["C", "F"],
                        help="Unit for training (default: C for all)")
    parser.add_argument("--save", action="store_true",
                        help="Save params to DB")
    parser.add_argument("--combined", action="store_true",
                        help="Combine signal + history data")
    args = parser.parse_args()

    # Extract data
    if args.combined:
        data_by_city = extract_combined_training_data(unit=args.unit)
    else:
        data_by_city = extract_signal_training_data(unit=args.unit)

    total = sum(td.n for td in data_by_city.values())
    print(f"\nTraining data: {total} samples across {len(data_by_city)} cities")
    for city, td in sorted(data_by_city.items()):
        city_cfg = config.CITIES.get(city)
        unit_label = city_cfg.temp_unit if city_cfg else "?"
        print(f"  {city:>13}: {td.n:>3} samples, "
              f"ens_mean range=[{td.ens_means.min():.1f}, {td.ens_means.max():.1f}]°{args.unit}, "
              f"obs range=[{td.observations.min():.1f}, {td.observations.max():.1f}]°{args.unit}")

    if args.grouped:
        params_by_city = train_grouped(data_by_city, args.min_samples)
    else:
        # Per-city training
        params_by_city = {}
        for city, td in data_by_city.items():
            if td.n < args.min_samples:
                log.warning(f"Skipping {city}: only {td.n} samples (need {args.min_samples})")
                continue
            params, _ = cross_validate_emos(td, city=city)
            params_by_city[city] = params

    if params_by_city:
        print_diagnostics(data_by_city, params_by_city)

    if args.save and params_by_city:
        for city, params in params_by_city.items():
            params.trained_at = datetime.now(timezone.utc).isoformat()
            save_emos_params(params)
        print(f"\nSaved EMOS params for {len(params_by_city)} cities to DB")


if __name__ == "__main__":
    main()
