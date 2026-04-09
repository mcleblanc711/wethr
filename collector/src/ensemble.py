"""
Ensemble weather forecast fetching from Open-Meteo.

Fetches multi-model ensemble forecasts and extracts daily max temperatures
per member for probability estimation.

Why ensemble counting works for weather betting:
- Each ensemble member is a plausible future weather state
- The fraction of members exceeding a threshold IS a probability estimate
- More members = better probability resolution
- Pooling models (143 total) gives better coverage than any single model

Limitations addressed in later phases:
- Ensembles are under-dispersed (too narrow) -> EMOS/NGR calibration (Phase 3)
- Models have station-specific biases -> bias correction (Phase 3)
- Equal weighting is suboptimal -> BMA weighting (Phase 4)

API efficiency:
- Open-Meteo supports comma-separated lat/lon for batch requests
- One request per model fetches ALL cities simultaneously
- 15 cities x 4 models = 4 requests (not 60+)
- Free tier: ~10,000 calls/day, but rate limited per minute
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class EnsembleMember:
    """One ensemble member's daily max temperature for a target date."""
    model: str
    member_idx: int
    daily_max: float       # In the native unit (deg C from API)
    daily_max_f: float     # Converted to Fahrenheit


@dataclass
class EnsembleForecast:
    """All ensemble members for a city/date combination."""
    city_slug: str
    target_date: date
    members: list[EnsembleMember] = field(default_factory=list)
    fetch_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def n_members(self) -> int:
        return len(self.members)

    @property
    def daily_maxes_f(self) -> np.ndarray:
        """All member daily max temps in Fahrenheit."""
        return np.array([m.daily_max_f for m in self.members])

    @property
    def daily_maxes_c(self) -> np.ndarray:
        """All member daily max temps in Celsius."""
        return np.array([m.daily_max for m in self.members])

    def daily_maxes(self, unit: str = "F") -> np.ndarray:
        """Daily maxes in the requested unit."""
        if unit.upper() == "F":
            return self.daily_maxes_f
        return self.daily_maxes_c

    @property
    def mean(self) -> float:
        """Ensemble mean daily max (deg C)."""
        return float(np.mean(self.daily_maxes_c)) if self.members else 0.0

    @property
    def std(self) -> float:
        """Ensemble std dev (deg C) -- proxy for forecast uncertainty."""
        return float(np.std(self.daily_maxes_c, ddof=1)) if len(self.members) > 1 else 0.0

    @property
    def spread(self) -> float:
        """Ensemble range (deg C) -- max minus min."""
        if not self.members:
            return 0.0
        vals = self.daily_maxes_c
        return float(np.max(vals) - np.min(vals))


def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


# ---------------------------------------------------------------------------
# Open-Meteo Ensemble API fetching (batched multi-city)
# ---------------------------------------------------------------------------

# Inter-request delay to stay well under rate limits.
# Open-Meteo free tier allows ~10K/day but enforces per-minute limits.
_REQUEST_DELAY = 2.0  # seconds between API calls


def _build_batch_url(
    cities: list[config.CityConfig],
    model: str,
) -> str:
    """
    Build a multi-city ensemble API URL requesting HOURLY data.

    We fetch hourly temperature_2m and compute daily max ourselves in each
    city's local timezone. This avoids Open-Meteo's UTC-boundary daily
    aggregation which produces wrong results for far-from-UTC cities (e.g.
    Seoul KST=UTC+9).

    No timezone parameter — responses come in UTC so we can convert
    to each city's local time explicitly.
    """
    lats = ",".join(str(c.lat) for c in cities)
    lons = ",".join(str(c.lon) for c in cities)
    return (
        f"{config.ENSEMBLE_API_BASE}"
        f"?latitude={lats}&longitude={lons}"
        f"&hourly=temperature_2m"
        f"&models={model}"
        f"&temperature_unit=celsius"
        f"&forecast_days={config.ENSEMBLE_FORECAST_DAYS}"
    )


def _build_single_url(city: config.CityConfig, model: str) -> str:
    """Build a single-city URL (used for GFS probe)."""
    return (
        f"{config.ENSEMBLE_API_BASE}"
        f"?latitude={city.lat}&longitude={city.lon}"
        f"&hourly=temperature_2m"
        f"&models={model}"
        f"&temperature_unit=celsius"
        f"&forecast_days={config.ENSEMBLE_FORECAST_DAYS}"
    )


def _parse_ensemble_response(
    data: dict,
    city_slug: str,
    model_name: str,
    tz_name: str = "UTC",
) -> dict[date, list[EnsembleMember]]:
    """
    Parse a single model's ensemble HOURLY response into per-member daily
    max temperatures grouped by LOCAL date.

    Open-Meteo returns hourly data as:
        hourly.time = ["2026-03-18T00:00", "2026-03-18T01:00", ...]
        hourly.temperature_2m_member01 = [5.3, 5.1, ...]
        hourly.temperature_2m_member02 = [5.0, 4.8, ...]

    We convert each UTC hour to local time, group by local date, and take
    the max per member per local date. This produces correct daily max
    values regardless of city timezone offset.
    """
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    if not times:
        log.warning(f"No hourly times in response for {model_name}/{city_slug}")
        return {}

    # Find all member keys
    member_keys = sorted([
        k for k in hourly.keys()
        if k.startswith("temperature_2m_member")
    ])

    if not member_keys:
        log.warning(f"No ensemble members found for {model_name}/{city_slug}")
        return {}

    local_tz = ZoneInfo(tz_name)
    utc = ZoneInfo("UTC")

    # Pre-compute local date for each hourly timestamp
    local_dates: list[date | None] = []
    for t_str in times:
        try:
            dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=utc)
            local_dates.append(dt_utc.astimezone(local_tz).date())
        except ValueError:
            local_dates.append(None)

    # For each member, group hourly temps by local date and take max
    # member_key -> local_date -> max_temp
    results: dict[date, list[EnsembleMember]] = {}

    for member_key in member_keys:
        values = hourly.get(member_key, [])

        m = re.search(r"member(\d+)", member_key)
        member_idx = int(m.group(1)) if m else 0

        # Accumulate max per local date for this member
        date_maxes: dict[date, float] = {}
        for i, val in enumerate(values):
            if val is None or i >= len(local_dates) or local_dates[i] is None:
                continue
            ld = local_dates[i]
            temp = float(val)
            if ld not in date_maxes or temp > date_maxes[ld]:
                date_maxes[ld] = temp

        # Add this member's daily max to results
        for d, temp_c in date_maxes.items():
            results.setdefault(d, []).append(EnsembleMember(
                model=model_name,
                member_idx=member_idx,
                daily_max=temp_c,
                daily_max_f=c_to_f(temp_c),
            ))

    log.debug(
        f"  {model_name}/{city_slug}: {len(member_keys)} members x {len(results)} dates"
    )
    return results


# ---------------------------------------------------------------------------
# Batch fetching with 429 backoff
# ---------------------------------------------------------------------------

class RateLimited(Exception):
    """Raised when Open-Meteo returns 429. Signals the caller to stop all requests."""
    pass


async def _fetch_with_backoff(
    client: httpx.AsyncClient,
    url: str,
    label: str,
    retries: int = config.HTTP_RETRIES,
    base_delay: float = 5.0,
) -> dict | list | None:
    """
    Fetch a URL with exponential backoff for transient errors.

    On 429 (rate limited): raises RateLimited immediately.
    Retrying a 429 just burns more quota against the rate limiter.
    The caller should stop all requests and let the loop back off.

    On other errors: retries with exponential backoff.
    """
    last_err = None

    for attempt in range(retries):
        try:
            resp = await client.get(url, timeout=config.HTTP_TIMEOUT)

            if resp.status_code == 429:
                log.warning(f"Rate limited (429) on {label} — stopping all requests")
                raise RateLimited(label)

            resp.raise_for_status()
            return resp.json()

        except RateLimited:
            raise  # Don't catch this in the generic handler

        except httpx.HTTPError as e:
            last_err = e
            if attempt < retries - 1:
                wait = base_delay * (2 ** attempt)
                log.debug(
                    f"Retry {attempt + 1}/{retries} for {label}: "
                    f"{e} -- waiting {wait:.0f}s"
                )
                await asyncio.sleep(wait)

    log.warning(f"All {retries} attempts failed for {label}: {last_err}")
    return None


async def _fetch_batch_model(
    client: httpx.AsyncClient,
    city_slugs: list[str],
    model: str,
) -> dict[str, dict[date, list[EnsembleMember]]]:
    """
    Fetch one model for ALL cities in a single API call.

    Open-Meteo returns a JSON array when multiple lat/lon are provided,
    one element per location in the same order as the input coordinates.
    Single city returns a plain dict (not wrapped in array).

    Returns: city_slug -> date -> [EnsembleMember]
    """
    cities = [config.CITIES[s] for s in city_slugs if s in config.CITIES]
    if not cities:
        return {}

    url = _build_batch_url(cities, model)
    label = f"{model} (batch, {len(cities)} cities)"

    data = await _fetch_with_backoff(client, url, label)
    if data is None:
        return {}

    # Normalize: single city returns dict, multiple returns list
    if isinstance(data, dict):
        responses = [data]
    elif isinstance(data, list):
        responses = data
    else:
        log.warning(f"Unexpected response type for {label}: {type(data)}")
        return {}

    if len(responses) != len(cities):
        log.warning(
            f"Response count mismatch for {label}: "
            f"got {len(responses)}, expected {len(cities)}"
        )
        # Use what we got
        responses = responses[:len(cities)]

    results: dict[str, dict[date, list[EnsembleMember]]] = {}
    for i, resp_item in enumerate(responses):
        if i >= len(city_slugs):
            break
        slug = city_slugs[i]

        if not isinstance(resp_item, dict):
            continue
        if "hourly" not in resp_item or "time" not in resp_item.get("hourly", {}):
            continue

        city_cfg = config.CITIES.get(slug)
        tz_name = city_cfg.timezone if city_cfg else "UTC"
        parsed = _parse_ensemble_response(resp_item, slug, model, tz_name=tz_name)
        if parsed:
            results[slug] = parsed

    log.info(
        f"  {model}: {len(results)}/{len(cities)} cities OK"
    )
    return results


# ---------------------------------------------------------------------------
# GFS model probe (single city, cached)
# ---------------------------------------------------------------------------

_gfs_model_name: str | None = None
_gfs_probed: bool = False


async def _probe_gfs_model(
    client: httpx.AsyncClient,
    city: config.CityConfig,
) -> str | None:
    """
    Try each GFS candidate model string until one returns 200.
    Caches the result for the session. Uses a single city to minimize
    API calls during probing.
    """
    global _gfs_model_name, _gfs_probed

    if _gfs_probed:
        return _gfs_model_name

    for candidate in config.GFS_ENSEMBLE_CANDIDATES:
        url = _build_single_url(city, candidate)
        try:
            resp = await client.get(url, timeout=15)
            if resp.status_code == 429:
                log.warning(f"GFS probe: rate limited on '{candidate}'")
                raise RateLimited("GFS probe")
            if resp.status_code == 200:
                data = resp.json()
                member_keys = [
                    k for k in data.get("hourly", {}).keys()
                    if "member" in k
                ]
                if member_keys:
                    _gfs_model_name = candidate
                    _gfs_probed = True
                    log.info(
                        f"GFS probe: '{candidate}' works "
                        f"({len(member_keys)} members)"
                    )
                    return candidate
            await asyncio.sleep(0.5)  # Be polite between probe attempts
        except Exception:
            pass
        log.debug(f"GFS probe: '{candidate}' failed")

    _gfs_probed = True
    log.warning(
        f"GFS probe: none of {config.GFS_ENSEMBLE_CANDIDATES} worked. "
        f"GFS ensemble will be excluded."
    )
    return None


# ---------------------------------------------------------------------------
# Legacy single-city fetch (kept for EMOS training / history)
# ---------------------------------------------------------------------------

async def fetch_ensemble_for_city(
    client: httpx.AsyncClient,
    city_slug: str,
) -> dict[date, EnsembleForecast]:
    """
    Fetch all ensemble models for a single city.

    For backward compatibility with EMOS training and history collection.
    For live scanning, use fetch_all_ensembles() which batches all cities.
    """
    city = config.CITIES.get(city_slug)
    if not city:
        raise ValueError(f"Unknown city: {city_slug}")

    models = list(config.ENSEMBLE_MODELS)
    gfs = await _probe_gfs_model(client, city)
    if gfs and gfs not in models:
        models.append(gfs)

    all_by_date: dict[date, list[EnsembleMember]] = {}
    models_ok = 0

    for model in models:
        url = _build_single_url(city, model)
        data = await _fetch_with_backoff(
            client, url, f"{model}/{city.name}"
        )
        if not data or not isinstance(data, dict):
            continue

        parsed = _parse_ensemble_response(data, city_slug, model, tz_name=city.timezone)
        for d, members in parsed.items():
            all_by_date.setdefault(d, []).extend(members)

        if parsed:
            models_ok += 1

        await asyncio.sleep(_REQUEST_DELAY)

    if models_ok == 0:
        log.error(f"No ensemble data for {city.name} -- all models failed")
        return {}

    forecasts = {}
    for d, members in all_by_date.items():
        ef = EnsembleForecast(
            city_slug=city_slug,
            target_date=d,
            members=members,
        )
        forecasts[d] = ef

    log.info(
        f"Fetched ensemble for {city.name}: "
        f"{models_ok} models, {len(forecasts)} dates, "
        f"{sum(f.n_members for f in forecasts.values())} total member-days"
    )
    return forecasts


# ---------------------------------------------------------------------------
# Main entry point: batched multi-city fetch
# ---------------------------------------------------------------------------

async def fetch_all_ensembles(
    client: httpx.AsyncClient,
    city_slugs: list[str] | None = None,
) -> dict[str, dict[date, EnsembleForecast]]:
    """
    Fetch ensemble forecasts for all cities using batched API calls.

    Instead of N_cities x N_models requests, makes just N_models requests
    (one per model, each containing all cities). For 15 cities x 4 models,
    this is 4 API calls instead of 60+.

    Returns:
        Nested dict: city_slug -> target_date -> EnsembleForecast
    """
    if city_slugs is None:
        city_slugs = list(config.CITIES.keys())

    # Probe GFS model name first (single-city request)
    # RateLimited propagates up — caller should back off
    first_city = config.CITIES.get(city_slugs[0])
    if first_city:
        gfs = await _probe_gfs_model(client, first_city)
        await asyncio.sleep(_REQUEST_DELAY)
    else:
        gfs = None

    # Build model list
    models = list(config.ENSEMBLE_MODELS)
    if gfs and gfs not in models:
        models.append(gfs)

    # Accumulate members: city_slug -> date -> [EnsembleMember]
    all_members: dict[str, dict[date, list[EnsembleMember]]] = {
        slug: {} for slug in city_slugs
    }
    models_ok = 0

    for model in models:
        # RateLimited on any model = stop trying all models
        batch_result = await _fetch_batch_model(client, city_slugs, model)

        if batch_result:
            models_ok += 1
            for slug, date_members in batch_result.items():
                for d, members in date_members.items():
                    all_members[slug].setdefault(d, []).extend(members)

        # Polite delay between model requests
        await asyncio.sleep(_REQUEST_DELAY)

    # Build EnsembleForecast objects
    results: dict[str, dict[date, EnsembleForecast]] = {}
    total_member_days = 0

    for slug in city_slugs:
        city_dates = all_members.get(slug, {})
        if not city_dates:
            log.warning(f"No ensemble data for {slug}")
            continue

        forecasts = {}
        for d, members in city_dates.items():
            ef = EnsembleForecast(
                city_slug=slug,
                target_date=d,
                members=members,
            )
            forecasts[d] = ef
            total_member_days += ef.n_members

        results[slug] = forecasts

    log.info(
        f"Ensemble fetch complete: {len(results)}/{len(city_slugs)} cities, "
        f"{models_ok} models OK, "
        f"{total_member_days} total member-forecasts "
        f"({len(models) + 1} API calls)"  # +1 for GFS probe
    )
    return results
