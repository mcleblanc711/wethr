"""
Probability estimation from ensemble forecasts.

Phase 1: Raw ensemble counting
    P(bracket) = (# members with daily max in bracket) / (total members)

This is the simplest unbiased estimator. It works because each ensemble
member is an equally likely future state. With 143 pooled members,
resolution is ~0.7% per count.

Known limitations (addressed in later phases):
    - Ensembles are under-dispersed → tail probabilities are underestimated
    - No station bias correction → systematic offset from resolution station
    - Equal model weighting → suboptimal when one model dominates skill

Phase 3 will add EMOS/NGR: fit a Gaussian to the ensemble, then integrate
over brackets. This corrects under-dispersion and bias simultaneously.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .ensemble import EnsembleForecast, c_to_f
from .markets import Bracket, WeatherMarket

log = logging.getLogger(__name__)


@dataclass
class BracketProbability:
    """Model probability for a single bracket."""
    bracket: Bracket
    model_prob: float          # Our ensemble-derived probability
    market_prob: float         # Current market price
    edge: float                # model_prob - market_prob
    member_count: int          # How many members fell in this bracket
    total_members: int         # Total ensemble members
    confidence: float          # Ensemble agreement metric (0-1)


@dataclass
class MarketProbabilities:
    """Full probability distribution for a weather market."""
    market: WeatherMarket
    forecast: EnsembleForecast
    brackets: list[BracketProbability] = field(default_factory=list)

    @property
    def probabilities_sum(self) -> float:
        """Sum of model probabilities — should be ~1.0."""
        return sum(bp.model_prob for bp in self.brackets)

    @property
    def market_prices_sum(self) -> float:
        """Sum of market prices — Polymarket vig shows as sum > 1.0."""
        return sum(bp.market_prob for bp in self.brackets)

    @property
    def max_edge(self) -> float:
        """Largest absolute edge across all brackets."""
        return max((abs(bp.edge) for bp in self.brackets), default=0.0)


def count_members_in_bracket(
    daily_maxes: np.ndarray,
    lower: float | None,
    upper: float | None,
) -> int:
    """
    Count ensemble members whose daily max falls within [lower, upper).
    
    Boundary convention:
        - "Below X":  [-inf, X)
        - "X to Y":   [X, Y)
        - "X or above": [X, +inf)
    
    This matches Polymarket's inclusive-lower, exclusive-upper convention
    for interior brackets. Tail brackets capture everything beyond.
    """
    if lower is None and upper is not None:
        # "Below X" bracket
        return int(np.sum(daily_maxes < upper))
    elif lower is not None and upper is None:
        # "Above X" bracket
        return int(np.sum(daily_maxes >= lower))
    elif lower is not None and upper is not None:
        # Interior bracket [lower, upper)
        return int(np.sum((daily_maxes >= lower) & (daily_maxes < upper)))
    else:
        # No bounds parsed — shouldn't happen
        return 0


def estimate_bracket_probabilities(
    market: WeatherMarket,
    forecast: EnsembleForecast,
) -> MarketProbabilities:
    """
    Estimate probability for each bracket using ensemble member counting.
    
    Steps:
    1. Get all ensemble daily max temps in the bracket's unit (°F or °C)
    2. For each bracket, count members in range
    3. Probability = count / total
    4. Calculate edge vs market price
    """
    city_cfg = market.city_config
    if not city_cfg:
        raise ValueError(f"No city config for {market.city}")

    # Get temperatures in the unit the market uses
    unit = city_cfg.temp_unit
    daily_maxes = forecast.daily_maxes(unit)
    n_total = len(daily_maxes)

    if n_total == 0:
        log.warning(f"No ensemble members for {market.city} on {market.target_date}")
        return MarketProbabilities(market=market, forecast=forecast)

    result = MarketProbabilities(market=market, forecast=forecast)

    for bracket in market.brackets:
        count = count_members_in_bracket(daily_maxes, bracket.lower, bracket.upper)
        model_prob = count / n_total
        edge = model_prob - bracket.market_prob

        # Confidence: how concentrated is the ensemble?
        # High confidence = most members agree on this bracket's direction
        # Use max(model_prob, 1-model_prob) as simple confidence proxy
        confidence = max(model_prob, 1.0 - model_prob)

        bp = BracketProbability(
            bracket=bracket,
            model_prob=model_prob,
            market_prob=bracket.market_prob,
            edge=edge,
            member_count=count,
            total_members=n_total,
            confidence=confidence,
        )
        result.brackets.append(bp)

    # Sanity check: probabilities should sum to ~1.0
    prob_sum = result.probabilities_sum
    if abs(prob_sum - 1.0) > 0.02:
        log.warning(
            f"Model probabilities sum to {prob_sum:.3f} for "
            f"{market.city} {market.target_date} "
            f"(expected ~1.0, n={n_total})"
        )

    # Log summary
    actionable = [bp for bp in result.brackets if abs(bp.edge) >= 0.08]
    if actionable:
        log.info(
            f"  {market.city} {market.target_date}: "
            f"{len(actionable)} brackets with |edge| >= 8% "
            f"(n={n_total} members)"
        )
        for bp in actionable:
            log.info(
                f"    {bp.bracket.label}: "
                f"model={bp.model_prob:.1%} vs market={bp.market_prob:.1%} "
                f"→ edge={bp.edge:+.1%} ({bp.member_count}/{bp.total_members})"
            )

    return result


def find_edges(
    markets: list[WeatherMarket],
    forecasts: dict,  # city_slug → date → EnsembleForecast
    min_edge: float = 0.08,
) -> list[BracketProbability]:
    """
    Scan all markets for brackets with edge above threshold.
    
    Returns list of BracketProbability sorted by |edge| descending.
    """
    edges = []

    for market in markets:
        city_forecasts = forecasts.get(market.city, {})
        forecast = city_forecasts.get(market.target_date)
        if not forecast:
            log.debug(f"No forecast for {market.city} {market.target_date}")
            continue

        mp = estimate_bracket_probabilities(market, forecast)
        for bp in mp.brackets:
            if abs(bp.edge) >= min_edge:
                edges.append(bp)

    edges.sort(key=lambda bp: abs(bp.edge), reverse=True)
    return edges
