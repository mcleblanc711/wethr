"""
EMOS/NGR calibration — Ensemble Model Output Statistics.

Raw ensemble counting underestimates tail probabilities because ensembles
are under-dispersed (the spread is too narrow). EMOS fixes this by:

1. Fitting a Gaussian N(μ, σ²) to the ensemble where:
      μ = a + b * ensemble_mean
      σ = c + d * ensemble_std
   The coefficients (a, b, c, d) are trained on historical data by
   minimizing CRPS (Continuous Ranked Probability Score).

2. Integrating the fitted Gaussian over each bracket to get calibrated
   probabilities.

Why this works:
- The bias correction (a, b) fixes systematic over/under-prediction
- The spread correction (c, d) inflates the distribution to match
  observed variability — this is where most of the improvement comes from
- CRPS is the proper scoring rule for continuous distributions, so
  optimizing it produces well-calibrated probabilistic forecasts

Training data: pairs of (ensemble_mean, ensemble_std, observed_max)
collected from historical forecasts and observations.

Reference:
  Gneiting et al. (2005) "Calibrated Probabilistic Forecasting Using
  Ensemble Model Output Statistics and Minimum CRPS Estimation"
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
from scipy import optimize, stats

from . import config
from .ensemble import EnsembleForecast, c_to_f
from .markets import Bracket, WeatherMarket
from .probability import BracketProbability, MarketProbabilities

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EMOS model parameters
# ---------------------------------------------------------------------------

@dataclass
class EMOSParams:
    """
    EMOS regression coefficients.
    
    μ = a + b * ensemble_mean
    σ = c + d * ensemble_std
    
    Constraints: b > 0 (ensemble mean should correlate with truth),
                 c > 0, d >= 0 (variance must be positive)
    """
    a: float = 0.0     # Mean intercept (bias correction)
    b: float = 1.0     # Mean slope (should be ~1.0 for well-calibrated ensemble)
    c: float = 1.0     # Std intercept (minimum spread)
    d: float = 1.0     # Std slope (ensemble spread scaling)
    
    # Metadata
    city: str = ""
    lead_days: int = -1          # -1 = all lead times pooled
    n_training: int = 0
    crps_train: float = float("inf")
    crps_test: float = float("inf")
    trained_at: str = ""

    def predict(self, ens_mean: float, ens_std: float) -> tuple[float, float]:
        """Return (calibrated_mean, calibrated_std) from ensemble stats."""
        mu = self.a + self.b * ens_mean
        sigma = max(self.c + self.d * ens_std, 0.1)  # Floor at 0.1 to avoid degenerate
        return mu, sigma

    def to_dict(self) -> dict:
        return {
            "a": self.a, "b": self.b, "c": self.c, "d": self.d,
            "city": self.city, "lead_days": self.lead_days,
            "n_training": self.n_training,
            "crps_train": self.crps_train,
            "crps_test": self.crps_test,
            "trained_at": self.trained_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EMOSParams":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# CRPS computation
# ---------------------------------------------------------------------------

def crps_gaussian(mu: float, sigma: float, obs: float) -> float:
    """
    CRPS for a Gaussian predictive distribution.
    
    Closed-form: CRPS(N(μ,σ²), y) = σ * [z*(2Φ(z)-1) + 2φ(z) - 1/√π]
    where z = (y - μ) / σ, Φ = CDF, φ = PDF of standard normal.
    
    Lower is better. Perfect = 0.
    """
    if sigma <= 0:
        return abs(obs - mu)  # Degenerate case
    
    z = (obs - mu) / sigma
    return sigma * (z * (2.0 * stats.norm.cdf(z) - 1.0) 
                    + 2.0 * stats.norm.pdf(z) 
                    - 1.0 / np.sqrt(np.pi))


def mean_crps(
    params: tuple[float, float, float, float],
    ens_means: np.ndarray,
    ens_stds: np.ndarray,
    observations: np.ndarray,
) -> float:
    """
    Mean CRPS across all training samples — the objective to minimize.
    """
    a, b, c, d = params
    total = 0.0
    n = len(observations)
    
    for i in range(n):
        mu = a + b * ens_means[i]
        sigma = max(c + d * ens_stds[i], 0.1)
        total += crps_gaussian(mu, sigma, observations[i])
    
    return total / n


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@dataclass
class TrainingData:
    """Historical (ensemble_mean, ensemble_std, observed_max) triples."""
    ens_means: np.ndarray
    ens_stds: np.ndarray
    observations: np.ndarray
    
    @property
    def n(self) -> int:
        return len(self.observations)


def train_emos(
    data: TrainingData,
    city: str = "",
    lead_days: int = -1,
) -> EMOSParams:
    """
    Fit EMOS coefficients by minimizing CRPS.
    
    Uses scipy.optimize.minimize with L-BFGS-B (bounded optimization).
    Bounds enforce physical constraints:
        b > 0 (positive correlation with ensemble mean)
        c > 0 (minimum spread)
        d >= 0 (non-negative spread scaling)
    """
    if data.n < 10:
        log.warning(
            f"Only {data.n} training samples for {city or 'global'} — "
            f"EMOS needs 30+ for reliable coefficients. Using defaults."
        )
        return EMOSParams(city=city, lead_days=lead_days, n_training=data.n)

    # Initial guess: identity transform
    x0 = [0.0, 1.0, 0.5, 1.0]
    
    # Bounds
    bounds = [
        (-20.0, 20.0),   # a: bias can be substantial
        (0.01, 3.0),     # b: must be positive, typically 0.8-1.2
        (0.1, 10.0),     # c: minimum spread (°C or °F)
        (0.0, 5.0),      # d: spread scaling
    ]

    result = optimize.minimize(
        mean_crps,
        x0,
        args=(data.ens_means, data.ens_stds, data.observations),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 1000, "ftol": 1e-8},
    )

    if not result.success:
        log.warning(f"EMOS optimization did not converge: {result.message}")

    a, b, c, d = result.x
    crps_val = result.fun

    params = EMOSParams(
        a=round(a, 4),
        b=round(b, 4),
        c=round(c, 4),
        d=round(d, 4),
        city=city,
        lead_days=lead_days,
        n_training=data.n,
        crps_train=round(crps_val, 4),
    )

    log.info(
        f"EMOS trained for {city or 'global'}: "
        f"μ = {a:.3f} + {b:.3f}*mean, "
        f"σ = {c:.3f} + {d:.3f}*std, "
        f"CRPS = {crps_val:.4f} (n={data.n})"
    )

    # Diagnostic: compare to uncalibrated baseline
    baseline_crps = mean_crps(
        (0.0, 1.0, 0.0, 1.0),  # Identity transform
        data.ens_means, data.ens_stds, data.observations,
    )
    improvement = (baseline_crps - crps_val) / baseline_crps * 100
    log.info(
        f"  Baseline CRPS = {baseline_crps:.4f}, "
        f"improvement = {improvement:.1f}%"
    )

    return params


def cross_validate_emos(
    data: TrainingData,
    city: str = "",
    n_folds: int = 5,
) -> tuple[EMOSParams, float]:
    """
    K-fold cross-validation to estimate out-of-sample CRPS.
    
    Returns (best_params, cv_crps).
    """
    if data.n < n_folds * 5:
        log.warning(f"Too few samples ({data.n}) for {n_folds}-fold CV")
        params = train_emos(data, city)
        return params, params.crps_train

    indices = np.arange(data.n)
    np.random.shuffle(indices)
    folds = np.array_split(indices, n_folds)
    
    cv_crps_values = []

    for fold_idx in range(n_folds):
        test_idx = folds[fold_idx]
        train_idx = np.concatenate([folds[j] for j in range(n_folds) if j != fold_idx])

        train_data = TrainingData(
            ens_means=data.ens_means[train_idx],
            ens_stds=data.ens_stds[train_idx],
            observations=data.observations[train_idx],
        )

        params = train_emos(train_data, city)

        # Evaluate on test fold
        test_crps = mean_crps(
            (params.a, params.b, params.c, params.d),
            data.ens_means[test_idx],
            data.ens_stds[test_idx],
            data.observations[test_idx],
        )
        cv_crps_values.append(test_crps)

    cv_crps = float(np.mean(cv_crps_values))
    cv_std = float(np.std(cv_crps_values))
    log.info(
        f"  CV CRPS for {city or 'global'}: {cv_crps:.4f} ± {cv_std:.4f}"
    )

    # Train final model on all data
    final_params = train_emos(data, city)
    final_params.crps_test = round(cv_crps, 4)

    return final_params, cv_crps


# ---------------------------------------------------------------------------
# Calibrated probability estimation
# ---------------------------------------------------------------------------

def calibrated_bracket_probability(
    lower: float | None,
    upper: float | None,
    mu: float,
    sigma: float,
) -> float:
    """
    Probability that temp falls in [lower, upper) under N(mu, sigma²).
    
    Uses CDF integration instead of member counting.
    """
    if sigma <= 0:
        sigma = 0.1

    if lower is None and upper is not None:
        # "Below X": P(T < upper) = Φ((upper - μ) / σ)
        return float(stats.norm.cdf(upper, mu, sigma))
    elif lower is not None and upper is None:
        # "Above X": P(T >= lower) = 1 - Φ((lower - μ) / σ)
        return float(1.0 - stats.norm.cdf(lower, mu, sigma))
    elif lower is not None and upper is not None:
        # Interior: P(lower <= T < upper)
        return float(
            stats.norm.cdf(upper, mu, sigma) 
            - stats.norm.cdf(lower, mu, sigma)
        )
    return 0.0


def estimate_calibrated_probabilities(
    market: WeatherMarket,
    forecast: EnsembleForecast,
    emos_params: EMOSParams | None = None,
) -> MarketProbabilities:
    """
    Estimate bracket probabilities using EMOS-calibrated Gaussian.
    
    Falls back to raw ensemble counting if no EMOS params available.
    """
    city_cfg = market.city_config
    if not city_cfg:
        raise ValueError(f"No city config for {market.city}")

    unit = city_cfg.temp_unit
    daily_maxes = forecast.daily_maxes(unit)
    n_total = len(daily_maxes)

    if n_total == 0:
        return MarketProbabilities(market=market, forecast=forecast)

    # Compute ensemble statistics in the market's unit
    ens_mean = float(np.mean(daily_maxes))
    ens_std = float(np.std(daily_maxes, ddof=1)) if n_total > 1 else 1.0

    # Get calibrated distribution
    if emos_params is not None:
        mu, sigma = emos_params.predict(ens_mean, ens_std)
        method = "EMOS"
    else:
        mu, sigma = ens_mean, ens_std
        method = "raw"

    result = MarketProbabilities(market=market, forecast=forecast)

    for bracket in market.brackets:
        model_prob = calibrated_bracket_probability(
            bracket.lower, bracket.upper, mu, sigma
        )

        # Also compute raw count for comparison / logging
        from .probability import count_members_in_bracket
        raw_count = count_members_in_bracket(daily_maxes, bracket.lower, bracket.upper)
        raw_prob = raw_count / n_total

        edge = model_prob - bracket.market_prob
        confidence = max(model_prob, 1.0 - model_prob)

        bp = BracketProbability(
            bracket=bracket,
            model_prob=model_prob,
            market_prob=bracket.market_prob,
            edge=edge,
            member_count=raw_count,  # Keep raw count for logging
            total_members=n_total,
            confidence=confidence,
        )
        result.brackets.append(bp)

    # Log comparison
    prob_sum = result.probabilities_sum
    if abs(prob_sum - 1.0) > 0.02:
        log.warning(
            f"Calibrated probs sum to {prob_sum:.3f} "
            f"(method={method}, μ={mu:.1f}, σ={sigma:.1f})"
        )

    actionable = [bp for bp in result.brackets if abs(bp.edge) >= 0.08]
    if actionable:
        log.info(
            f"  {market.city} {market.target_date} [{method}]: "
            f"μ={mu:.1f}°{unit}, σ={sigma:.1f}°{unit}, "
            f"{len(actionable)} edges ≥ 8%"
        )
        for bp in actionable:
            raw_p = bp.member_count / bp.total_members if bp.total_members > 0 else 0
            log.info(
                f"    {bp.bracket.label}: "
                f"{method}={bp.model_prob:.1%} (raw={raw_p:.1%}) "
                f"vs market={bp.market_prob:.1%} → edge={bp.edge:+.1%}"
            )

    return result


# ---------------------------------------------------------------------------
# Persistence — save/load EMOS params
# ---------------------------------------------------------------------------

_EMOS_SCHEMA = """
CREATE TABLE IF NOT EXISTS emos_params (
    city TEXT NOT NULL,
    lead_days INTEGER NOT NULL DEFAULT -1,
    params_json TEXT NOT NULL,
    trained_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (city, lead_days)
);
"""


def save_emos_params(
    params: EMOSParams,
    db_path: Path | None = None,
) -> None:
    """Save trained EMOS params to database."""
    from .paper_trader import get_db
    
    with get_db(db_path) as conn:
        conn.executescript(_EMOS_SCHEMA)
        conn.execute(
            """
            INSERT OR REPLACE INTO emos_params (city, lead_days, params_json)
            VALUES (?, ?, ?)
            """,
            (params.city, params.lead_days, json.dumps(params.to_dict())),
        )
    log.info(f"Saved EMOS params for {params.city or 'global'} (lead={params.lead_days}d)")


def load_emos_params(
    city: str,
    lead_days: int = -1,
    db_path: Path | None = None,
) -> EMOSParams | None:
    """Load trained EMOS params from database."""
    from .paper_trader import get_db

    with get_db(db_path) as conn:
        conn.executescript(_EMOS_SCHEMA)
        row = conn.execute(
            "SELECT params_json FROM emos_params WHERE city = ? AND lead_days = ?",
            (city, lead_days),
        ).fetchone()

    if not row:
        return None

    return EMOSParams.from_dict(json.loads(row["params_json"]))


def load_all_emos_params(
    db_path: Path | None = None,
) -> dict[str, EMOSParams]:
    """Load all EMOS params, keyed by city slug."""
    from .paper_trader import get_db

    with get_db(db_path) as conn:
        conn.executescript(_EMOS_SCHEMA)
        rows = conn.execute(
            "SELECT city, params_json FROM emos_params WHERE lead_days = -1"
        ).fetchall()

    return {
        row["city"]: EMOSParams.from_dict(json.loads(row["params_json"]))
        for row in rows
    }
