"""
Forecast latency exploitation — trade ensemble model update shifts.

Ensemble models update on known schedules:
    ECMWF IFS:  00Z, 06Z, 12Z, 18Z (every 6 hours)
    GFS/GEFS:   00Z, 06Z, 12Z, 18Z (every 6 hours)
    ICON-EPS:   00Z, 12Z (every 12 hours)
    GEM:        00Z, 12Z (every 12 hours)

When a new model run drops, the ensemble distribution shifts. If the
market was priced on the old run, there's a temporary edge until
market-makers update. This module:

1. Tracks the previous ensemble distribution per city/date
2. Detects when a new model run arrives (distribution shift > threshold)
3. Measures the shift in terms of bracket probability changes
4. Signals the main pipeline to re-evaluate with boosted urgency

The key insight: weather prediction markets have ~5-15 minute latency
in repricing after model updates. On high-impact days (cold fronts,
heat waves), the shift can be 10-20% on a single bracket.

Usage:
    detector = LatencyDetector()
    shift = detector.check_shift(city, target_date, new_forecast)
    if shift.is_significant:
        # Re-run probability estimation with higher urgency
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import numpy as np

from . import config
from .ensemble import EnsembleForecast, c_to_f

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model update schedule (approximate UTC times)
# ---------------------------------------------------------------------------

MODEL_UPDATE_SCHEDULE = {
    # model_name: list of (hour, minute) UTC update times
    # These are when Open-Meteo typically makes the data available,
    # which lags the nominal model init time by 4-6 hours.
    "ecmwf_ifs025_ensemble": [(5, 0), (11, 0), (17, 0), (23, 0)],
    "icon_seamless_eps": [(7, 0), (19, 0)],
    "gem_global_ensemble": [(9, 0), (21, 0)],
    # GFS — all candidate names share the same schedule
    "gfs_seamless_eps": [(5, 30), (11, 30), (17, 30), (23, 30)],
    "gfs025_eps": [(5, 30), (11, 30), (17, 30), (23, 30)],
    "gfs05_eps": [(5, 30), (11, 30), (17, 30), (23, 30)],
    "ncep_gefs025": [(5, 30), (11, 30), (17, 30), (23, 30)],
    "gefs025": [(5, 30), (11, 30), (17, 30), (23, 30)],
}

# Minimum distribution shift to flag as significant (°C)
MIN_MEAN_SHIFT = 0.8   # ~1.4°F — enough to move a bracket boundary
MIN_STD_SHIFT = 0.3    # Spread changes also matter for tail brackets


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DistributionSnapshot:
    """Snapshot of ensemble distribution at a point in time."""
    city_slug: str
    target_date: date
    timestamp: datetime
    mean: float           # °C
    std: float            # °C
    median: float         # °C
    p10: float            # 10th percentile
    p90: float            # 90th percentile
    n_members: int
    # Per-model means for tracking which model shifted
    model_means: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_forecast(cls, fc: EnsembleForecast) -> "DistributionSnapshot":
        vals = fc.daily_maxes_c
        if len(vals) == 0:
            return cls(
                city_slug=fc.city_slug, target_date=fc.target_date,
                timestamp=datetime.now(timezone.utc),
                mean=0, std=0, median=0, p10=0, p90=0, n_members=0,
            )

        # Per-model means
        model_means: dict[str, float] = {}
        for m in fc.members:
            model_means.setdefault(m.model, []).append(m.daily_max)
        model_means = {k: float(np.mean(v)) for k, v in model_means.items()}

        return cls(
            city_slug=fc.city_slug,
            target_date=fc.target_date,
            timestamp=fc.fetch_time,
            mean=float(np.mean(vals)),
            std=float(np.std(vals, ddof=1)) if len(vals) > 1 else 0,
            median=float(np.median(vals)),
            p10=float(np.percentile(vals, 10)),
            p90=float(np.percentile(vals, 90)),
            n_members=len(vals),
            model_means=model_means,
        )


@dataclass
class ForecastShift:
    """Detected shift between two ensemble snapshots."""
    city_slug: str
    target_date: date
    mean_shift: float        # °C change in ensemble mean
    std_shift: float         # °C change in ensemble std
    median_shift: float      # °C change in median
    p10_shift: float
    p90_shift: float
    time_elapsed: float      # Seconds between snapshots
    model_shifts: dict[str, float] = field(default_factory=dict)  # Per-model mean shifts
    dominant_model: str = ""  # Which model drove the shift

    @property
    def is_significant(self) -> bool:
        """Is this shift large enough to potentially create tradeable edge?"""
        return (
            abs(self.mean_shift) >= MIN_MEAN_SHIFT
            or abs(self.std_shift) >= MIN_STD_SHIFT
        )

    @property
    def mean_shift_f(self) -> float:
        """Mean shift in Fahrenheit."""
        return self.mean_shift * 9.0 / 5.0

    @property
    def severity(self) -> str:
        """Human-readable severity classification."""
        abs_shift = abs(self.mean_shift)
        if abs_shift >= 3.0:
            return "MAJOR"    # ~5.4°F — regime change, likely front passage
        elif abs_shift >= 1.5:
            return "MODERATE" # ~2.7°F — meaningful for bracket pricing
        elif abs_shift >= MIN_MEAN_SHIFT:
            return "MINOR"    # ~1.4°F — edge case, tread carefully
        return "NONE"


# ---------------------------------------------------------------------------
# Latency Detector
# ---------------------------------------------------------------------------

class LatencyDetector:
    """
    Tracks ensemble distribution snapshots and detects significant shifts.
    
    Maintains the previous snapshot per (city, target_date). On each new
    forecast, computes the shift and flags significant changes.
    """

    def __init__(self):
        self._previous: dict[tuple[str, date], DistributionSnapshot] = {}
        self._shift_history: list[ForecastShift] = []

    def check_shift(
        self,
        forecast: EnsembleForecast,
    ) -> ForecastShift | None:
        """
        Compare new forecast against previous snapshot.
        
        Returns ForecastShift if a previous snapshot exists, None otherwise.
        Updates the stored snapshot with the new forecast.
        """
        key = (forecast.city_slug, forecast.target_date)
        new_snap = DistributionSnapshot.from_forecast(forecast)

        if new_snap.n_members == 0:
            return None

        prev = self._previous.get(key)
        self._previous[key] = new_snap

        if prev is None:
            return None  # First observation — no comparison possible

        # Compute shifts
        elapsed = (new_snap.timestamp - prev.timestamp).total_seconds()

        # Per-model shifts
        model_shifts = {}
        all_models = set(prev.model_means.keys()) | set(new_snap.model_means.keys())
        for model in all_models:
            old_mean = prev.model_means.get(model)
            new_mean = new_snap.model_means.get(model)
            if old_mean is not None and new_mean is not None:
                model_shifts[model] = new_mean - old_mean

        # Find dominant model (largest absolute shift)
        dominant = ""
        if model_shifts:
            dominant = max(model_shifts, key=lambda k: abs(model_shifts[k]))

        shift = ForecastShift(
            city_slug=forecast.city_slug,
            target_date=forecast.target_date,
            mean_shift=new_snap.mean - prev.mean,
            std_shift=new_snap.std - prev.std,
            median_shift=new_snap.median - prev.median,
            p10_shift=new_snap.p10 - prev.p10,
            p90_shift=new_snap.p90 - prev.p90,
            time_elapsed=elapsed,
            model_shifts=model_shifts,
            dominant_model=dominant,
        )

        if shift.is_significant:
            self._shift_history.append(shift)
            log.info(
                f"🔄 Forecast shift [{shift.severity}] "
                f"{forecast.city_slug} {forecast.target_date}: "
                f"mean {shift.mean_shift:+.1f}°C ({shift.mean_shift_f:+.1f}°F), "
                f"std {shift.std_shift:+.2f}°C, "
                f"driven by {shift.dominant_model} "
                f"({shift.model_shifts.get(shift.dominant_model, 0):+.1f}°C), "
                f"elapsed={elapsed/60:.0f}min"
            )
        else:
            log.debug(
                f"  Shift check {forecast.city_slug} {forecast.target_date}: "
                f"mean {shift.mean_shift:+.2f}°C — below threshold"
            )

        return shift

    def get_recent_shifts(
        self,
        city_slug: str | None = None,
        min_severity: str = "MINOR",
    ) -> list[ForecastShift]:
        """Get recent significant shifts, optionally filtered by city."""
        severity_order = {"NONE": 0, "MINOR": 1, "MODERATE": 2, "MAJOR": 3}
        min_level = severity_order.get(min_severity, 0)

        shifts = self._shift_history
        if city_slug:
            shifts = [s for s in shifts if s.city_slug == city_slug]

        return [
            s for s in shifts
            if severity_order.get(s.severity, 0) >= min_level
        ]

    def clear_stale(self, max_age_hours: int = 24) -> None:
        """Remove snapshots for dates that have passed."""
        today = datetime.now(timezone.utc).date()
        stale_keys = [
            key for key in self._previous
            if key[1] < today
        ]
        for key in stale_keys:
            del self._previous[key]

    @property
    def tracked_count(self) -> int:
        return len(self._previous)

    @property
    def total_shifts_detected(self) -> int:
        return len(self._shift_history)
