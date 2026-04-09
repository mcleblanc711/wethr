"""
Bayesian Model Averaging (BMA) — Phase 4.

Instead of pooling all 143 ensemble members with equal weight,
BMA weights each model by its historical forecasting skill.

Why this matters:
- ECMWF IFS is consistently the best global ensemble for 1-7 day temperature
- ICON-EPS has edge for European cities
- GEM punches above its weight for North American winter
- Equal-weight dilutes ECMWF's signal with noisier models

BMA approach:
1. For each model, compute historical CRPS against observations
2. Convert CRPS to weights: w_i ∝ exp(-CRPS_i / temperature)
3. The "temperature" parameter controls how sharply we favour better models
4. Apply weights when counting members across brackets

The result is a weighted mixture of Gaussians (one per model), which
is a better predictive distribution than a single Gaussian fit to all
members.

Training data comes from the same historical_forecasts table used
for EMOS. We track per-model CRPS to derive weights.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
from scipy import stats as sp_stats

from . import config
from .ensemble import EnsembleForecast, EnsembleMember, c_to_f
from .markets import Bracket
from .paper_trader import get_db
from .calibration import (
    EMOSParams,
    crps_gaussian,
    calibrated_bracket_probability,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BMA weights
# ---------------------------------------------------------------------------

@dataclass
class BMAWeights:
    """Per-model weights derived from historical CRPS."""
    weights: dict[str, float]   # model_name → weight (sums to 1.0)
    crps_scores: dict[str, float] = field(default_factory=dict)
    city: str = ""
    n_samples: int = 0
    temperature: float = 1.0    # Softmax temperature

    def get(self, model: str) -> float:
        """Get weight for a model, defaulting to equal-weight if unknown."""
        if model in self.weights:
            return self.weights[model]
        # Unknown model — give it equal share of remaining weight
        n = len(self.weights)
        return 1.0 / (n + 1) if n > 0 else 1.0

    def to_dict(self) -> dict:
        return {
            "weights": self.weights,
            "crps_scores": self.crps_scores,
            "city": self.city,
            "n_samples": self.n_samples,
            "temperature": self.temperature,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BMAWeights":
        return cls(
            weights=d.get("weights", {}),
            crps_scores=d.get("crps_scores", {}),
            city=d.get("city", ""),
            n_samples=d.get("n_samples", 0),
            temperature=d.get("temperature", 1.0),
        )


def compute_model_crps(
    city_slug: str,
    db_path: Path | None = None,
) -> dict[str, float]:
    """
    Compute per-model mean CRPS from historical forecasts vs observations.
    
    For each (city, date, model) where we have both forecast stats and
    observed temperature, compute CRPS for a Gaussian N(ens_mean, ens_std²).
    Returns dict of model → mean CRPS.
    """
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT 
                f.model, f.ens_mean_c, f.ens_std_c, o.observed_max_c
            FROM historical_forecasts f
            JOIN historical_observations o
                ON f.city = o.city AND f.target_date = o.target_date
            WHERE f.city = ? AND f.n_members > 1
            ORDER BY f.model
            """,
            (city_slug,),
        ).fetchall()

    if not rows:
        return {}

    model_crps: dict[str, list[float]] = {}
    for row in rows:
        model = row["model"]
        mu = row["ens_mean_c"]
        sigma = max(row["ens_std_c"], 0.5)
        obs = row["observed_max_c"]
        crps = crps_gaussian(mu, sigma, obs)
        model_crps.setdefault(model, []).append(crps)

    return {
        model: float(np.mean(values))
        for model, values in model_crps.items()
    }


def compute_bma_weights(
    city_slug: str,
    temperature: float = 1.0,
    db_path: Path | None = None,
) -> BMAWeights:
    """
    Compute BMA weights from historical per-model CRPS.
    
    Weight formula: w_i = exp(-CRPS_i / T) / Σ exp(-CRPS_j / T)
    
    Lower temperature → sharper weighting (more weight to best model).
    temperature=1.0 is a reasonable default. At temperature=0.5, the
    best model gets ~4x the weight of a model with 2x worse CRPS.
    
    At temperature→∞, all models get equal weight (current behavior).
    """
    crps_scores = compute_model_crps(city_slug, db_path)

    if not crps_scores:
        # No historical data — fall back to equal weights
        n = len(config.ENSEMBLE_MODELS)
        return BMAWeights(
            weights={m: 1.0 / n for m in config.ENSEMBLE_MODELS},
            city=city_slug,
            temperature=temperature,
        )

    # Softmax weighting: w_i ∝ exp(-CRPS_i / T)
    models = list(crps_scores.keys())
    crps_vals = np.array([crps_scores[m] for m in models])

    if temperature <= 0:
        temperature = 1.0

    log_weights = -crps_vals / temperature
    # Numerical stability: subtract max before exp
    log_weights -= np.max(log_weights)
    raw_weights = np.exp(log_weights)
    normalized = raw_weights / raw_weights.sum()

    weights = {m: round(float(w), 4) for m, w in zip(models, normalized)}

    # Include any configured models not in historical data with minimum weight
    min_weight = 0.01
    for model in config.ENSEMBLE_MODELS:
        if model not in weights:
            weights[model] = min_weight

    # Re-normalize
    total = sum(weights.values())
    weights = {m: round(w / total, 4) for m, w in weights.items()}

    n_samples = sum(len(v) for v in [
        [r for r in [] ]  # placeholder — we already computed means
    ])

    bma = BMAWeights(
        weights=weights,
        crps_scores={m: round(v, 4) for m, v in crps_scores.items()},
        city=city_slug,
        n_samples=sum(1 for _ in crps_scores.values()),  # number of models
        temperature=temperature,
    )

    log.info(f"BMA weights for {city_slug}: {weights}")
    for m in sorted(weights, key=weights.get, reverse=True):
        crps = crps_scores.get(m, 0)
        log.info(f"  {m}: weight={weights[m]:.3f}, CRPS={crps:.4f}")

    return bma


# ---------------------------------------------------------------------------
# Weighted probability estimation
# ---------------------------------------------------------------------------

def weighted_bracket_probability(
    forecast: EnsembleForecast,
    lower: float | None,
    upper: float | None,
    unit: str,
    bma_weights: BMAWeights,
    emos_params: dict[str, EMOSParams] | None = None,
) -> float:
    """
    Compute bracket probability using BMA-weighted model contributions.
    
    For each model:
    1. Extract that model's members from the pooled forecast
    2. Compute mean/std for that model's members
    3. If EMOS params exist, apply calibration
    4. Compute bracket probability from the (calibrated) Gaussian
    5. Weight by BMA weight
    
    Final probability = Σ w_i * P_i(bracket)
    
    This is a weighted mixture of Gaussians, which is more flexible
    than a single Gaussian — it can represent bimodal distributions
    when models disagree.
    """
    # Group members by model
    by_model: dict[str, list[float]] = {}
    for m in forecast.members:
        val = m.daily_max_f if unit == "F" else m.daily_max
        by_model.setdefault(m.model, []).append(val)

    if not by_model:
        return 0.0

    total_prob = 0.0
    total_weight = 0.0

    for model, values in by_model.items():
        arr = np.array(values)
        if len(arr) < 2:
            continue

        model_mean = float(np.mean(arr))
        model_std = float(np.std(arr, ddof=1))

        # Apply per-model EMOS if available
        if emos_params and model in emos_params:
            mu, sigma = emos_params[model].predict(model_mean, model_std)
        else:
            mu, sigma = model_mean, max(model_std, 0.5)

        prob = calibrated_bracket_probability(lower, upper, mu, sigma)
        weight = bma_weights.get(model)

        total_prob += weight * prob
        total_weight += weight

    # Normalize in case weights don't sum to 1 for available models
    if total_weight > 0:
        return total_prob / total_weight
    return 0.0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_BMA_SCHEMA = """
CREATE TABLE IF NOT EXISTS bma_weights (
    city TEXT PRIMARY KEY,
    weights_json TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def save_bma_weights(bma: BMAWeights, db_path: Path | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(_BMA_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO bma_weights (city, weights_json) VALUES (?, ?)",
            (bma.city, json.dumps(bma.to_dict())),
        )


def load_bma_weights(city: str, db_path: Path | None = None) -> BMAWeights | None:
    with get_db(db_path) as conn:
        conn.executescript(_BMA_SCHEMA)
        row = conn.execute(
            "SELECT weights_json FROM bma_weights WHERE city = ?", (city,)
        ).fetchone()

    if not row:
        return None
    return BMAWeights.from_dict(json.loads(row["weights_json"]))


def load_all_bma_weights(db_path: Path | None = None) -> dict[str, BMAWeights]:
    with get_db(db_path) as conn:
        conn.executescript(_BMA_SCHEMA)
        rows = conn.execute("SELECT city, weights_json FROM bma_weights").fetchall()
    return {
        row["city"]: BMAWeights.from_dict(json.loads(row["weights_json"]))
        for row in rows
    }
