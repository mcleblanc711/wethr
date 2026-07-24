"""
Market discovery via Polymarket Gamma API.

Finds active weather temperature markets, parses bracket boundaries,
and returns structured market data for signal generation.

Gamma API is free, no auth required. Rate limits are generous (~100 req/min).
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Bracket:
    """A single temperature bracket within a market."""
    token_id: str          # CLOB token ID for YES outcome
    label: str             # e.g., "72°F - 74°F" or "Below 68°F"
    lower: float | None    # Lower bound (None for "Below X")
    upper: float | None    # Upper bound (None for "Above X")
    unit: str              # "F" or "C"
    market_prob: float     # Current YES price (0-1)
    condition_id: str      # Market condition ID
    no_token_id: str = ""  # CLOB token ID for NO outcome


@dataclass
class WeatherMarket:
    """A complete weather market (all brackets for one city/date)."""
    event_id: str
    event_slug: str
    city: str              # Matched city slug from config
    target_date: date
    question: str          # Full event question
    brackets: list[Bracket] = field(default_factory=list)
    total_volume: float = 0.0
    active: bool = True
    resolution_url: str = ""
    declared_station: str | None = None
    declared_precision: float | None = None

    @property
    def city_config(self) -> config.CityConfig | None:
        return config.CITIES.get(self.city)


def extract_resolution_metadata(event: dict, city_slug: str) -> tuple[str, str | None, float | None]:
    """Extract declared source URL, station text, and rounding precision."""
    markets = event.get("markets", []) or []
    combined = "\n".join(str(value) for value in (
        event.get("description", ""), event.get("title", ""),
        *(market.get("description", "") for market in markets),
    ))
    resolution_url = str(event.get("resolutionSource") or "")
    if not resolution_url:
        for market in markets:
            if market.get("resolutionSource"):
                resolution_url = str(market["resolutionSource"])
                break
    if not resolution_url:
        match = re.search(r"https?://[^\s)]+", combined)
        resolution_url = match.group(0).rstrip(".,") if match else ""
    station_match = re.search(
        r"(?:weather station at|station(?: located)? at|reported by)\s+([^.;\n]+)",
        combined, re.IGNORECASE,
    )
    declared_station = station_match.group(1).strip() if station_match else None
    configured_station = config.CITIES.get(city_slug).station if city_slug in config.CITIES else None
    if declared_station is None and configured_station and configured_station.lower() in combined.lower():
        declared_station = configured_station
    lower = combined.lower()
    if "nearest tenth" in lower or "one decimal" in lower or "0.1 degree" in lower:
        precision = 0.1
    elif "nearest whole" in lower or "whole degree" in lower or "integer" in lower:
        precision = 1.0
    else:
        # Deliberately not inferred from CityConfig: guessing the rounding rule
        # would silently decide whether station truth matches the winning
        # bracket. Surface it so the market can be fixed or excluded — the
        # count is reported by collection_status as missing_resolution_metadata.
        precision = None
        log.warning(
            f"No declared rounding precision for {city_slug}; market cannot "
            f"reconcile against station truth until the rule is known"
        )
    return resolution_url, declared_station, precision


# ---------------------------------------------------------------------------
# Bracket parsing
# ---------------------------------------------------------------------------

# Patterns for temperature bracket labels from Polymarket:
# Full ranges:    "72°F - 74°F", "1.10–1.14ºC"
# Compact ranges: "44-45°F", "66-67°F" (2°F bins, one degree sign at end)
# Below:          "Below 68°F", "31°F or below", "-6°C or below", "< 60°F"
# Above:          "76°F or above", "23°C or higher", "> 85°F", "76°F+"
# Single:         "13°C", "-1°C" (1°C bins)

_RANGE_PAT = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*°?\s*([FCº])\s*[-–to]+\s*(-?\d+(?:\.\d+)?)\s*°?\s*([FCº])",
    re.IGNORECASE,
)
# Compact range: "44-45°F" — no degree sign on first number
_COMPACT_RANGE_PAT = re.compile(
    r"^(-?\d+)\s*[-–]\s*(-?\d+)\s*°\s*([FC])$",
    re.IGNORECASE,
)
_BELOW_PAT = re.compile(
    r"(?:below|under|less than|<)\s*(-?\d+(?:\.\d+)?)\s*°\s*([FC])|"
    r"(-?\d+(?:\.\d+)?)\s*°\s*([FC])\s*(?:or below|or less|or lower|or under)",
    re.IGNORECASE,
)
_ABOVE_PAT = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*°\s*([FC])\s*(?:or above|\+|or more|or higher)|"
    r"(?:above|over|more than|greater than|>)\s*(-?\d+(?:\.\d+)?)\s*°\s*([FC])",
    re.IGNORECASE,
)
# Single value: "13°C", "-1°C" — exactly "number°unit" with nothing else
_SINGLE_PAT = re.compile(
    r"^(-?\d+(?:\.\d+)?)\s*°\s*([FC])$",
    re.IGNORECASE,
)


def parse_bracket_label(label: str) -> tuple[float | None, float | None, str]:
    """
    Parse a bracket label into (lower, upper, unit).
    
    Returns:
        (lower_bound, upper_bound, unit)
        lower=None for "Below X" / "X or below"
        upper=None for "Above X" / "X or higher"
    
    Polymarket bracket formats:
        "44-45°F"     → [44, 46)  — 2°F bin, compact range
        "13°C"        → [13, 14)  — 1°C bin, single value
        "72°F - 74°F" → [72, 74)  — explicit range with degree signs
        "31°F or below" → [−∞, 31) — tail bracket
        "23°C or higher" → [23, +∞) — tail bracket
    """
    label = label.strip()

    def _norm_unit(u: str) -> str:
        u = u.upper()
        if u == "º":
            return "C"
        return u

    # Try below first (before range, to avoid "31°F or below" matching as single)
    m = _BELOW_PAT.search(label)
    if m:
        if m.group(1) is not None:
            return None, float(m.group(1)), _norm_unit(m.group(2))
        else:
            return None, float(m.group(3)), _norm_unit(m.group(4))

    # Try above
    m = _ABOVE_PAT.search(label)
    if m:
        if m.group(1) is not None:
            return float(m.group(1)), None, _norm_unit(m.group(2))
        else:
            return float(m.group(3)), None, _norm_unit(m.group(4))

    # Try full range: "72°F - 74°F"
    m = _RANGE_PAT.search(label)
    if m:
        return float(m.group(1)), float(m.group(3)), _norm_unit(m.group(2))

    # Try compact range: "44-45°F" → [44, 46)
    m = _COMPACT_RANGE_PAT.search(label)
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2))
        unit = _norm_unit(m.group(3))
        # Upper bound is hi + 1 because "44-45" covers [44, 46)
        return lo, hi + 1, unit

    # Try single value: "13°C" → [13, 14)
    m = _SINGLE_PAT.search(label)
    if m:
        val = float(m.group(1))
        unit = _norm_unit(m.group(2))
        # 1-degree bin: [val, val+1)
        return val, val + 1, unit

    log.warning(f"Could not parse bracket label: {label!r}")
    return None, None, ""


# ---------------------------------------------------------------------------
# Date parsing from event titles
# ---------------------------------------------------------------------------

_DATE_PATTERNS = [
    # "on March 20" / "on March 20, 2026"
    re.compile(
        r"on\s+(\w+)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?",
        re.IGNORECASE,
    ),
    # "March 20, 2026" standalone
    re.compile(
        r"(\w+)\s+(\d{1,2})\s*,\s*(\d{4})",
        re.IGNORECASE,
    ),
    # ISO-ish: "2026-03-20"
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
]

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_target_date(text: str) -> date | None:
    """Extract target date from event title/description."""
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()

        # ISO format
        if groups[0].isdigit() and len(groups[0]) == 4:
            return date(int(groups[0]), int(groups[1]), int(groups[2]))

        # "Month Day [Year]"
        month_str = groups[0].lower()
        month = _MONTHS.get(month_str)
        if month is None:
            continue
        day = int(groups[1])
        year = int(groups[2]) if groups[2] else datetime.now(timezone.utc).year
        try:
            return date(year, month, day)
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# City matching
# ---------------------------------------------------------------------------

def match_city(text: str) -> str | None:
    """Match event text to a known city slug."""
    text_lower = text.lower()
    for slug, cfg in config.CITIES.items():
        # Check both the display name and gamma_tag
        if cfg.gamma_tag.lower() in text_lower:
            return slug
        if cfg.name.lower() in text_lower:
            return slug
    return None


# ---------------------------------------------------------------------------
# Gamma API fetching
# ---------------------------------------------------------------------------

async def fetch_weather_events(client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch weather temperature events from Gamma API.
    
    Strategy: use tag_slug=weather on the /events endpoint (works even
    though /tags doesn't list it), paginate, then client-side filter
    to "highest temperature" events only.
    """
    events = []
    seen_ids = set()
    offset = 0
    limit = 50
    max_pages = 10

    for _ in range(max_pages):
        try:
            resp = await client.get(
                config.GAMMA_EVENTS_URL,
                params={
                    "active": "true",
                    "closed": "false",
                    "tag_slug": "weather",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=config.HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            if not isinstance(data, list) or len(data) == 0:
                break

            for event in data:
                eid = event.get("id", "")
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    events.append(event)

            if len(data) < limit:
                break

            offset += limit

        except httpx.HTTPError as e:
            log.warning(f"Gamma API error (offset={offset}): {e}")
            break

    # Client-side filter to temperature markets
    temp_events = [
        e for e in events
        if "highest temperature" in e.get("title", "").lower()
    ]

    log.info(
        f"Discovered {len(temp_events)} temperature events "
        f"(from {len(events)} total weather events)"
    )
    return temp_events


async def _discover_weather_tag(client: httpx.AsyncClient) -> str | None:
    """
    Find the tag_id for weather markets by searching /tags.
    
    Polymarket tags weather markets under various labels.
    Logs all found tags to help debug when matching fails.
    """
    try:
        resp = await client.get(
            f"{config.GAMMA_API_BASE}/tags",
            params={"limit": 200},
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        tags = resp.json()

        if not isinstance(tags, list):
            return None

        # Log candidate weather tags for debugging
        weather_candidates = [
            t for t in tags
            if any(kw in t.get("label", "").lower() + " " + t.get("slug", "").lower()
                   for kw in ("weather", "climate", "temperature", "forecast"))
        ]
        if weather_candidates:
            log.info(f"Weather-related tags found: {[(t.get('id'), t.get('label'), t.get('slug')) for t in weather_candidates]}")
        else:
            log.debug(
                f"No weather tags found in {len(tags)} tags. "
                f"Sample: {[(t.get('id'), t.get('slug')) for t in tags[:10]]}"
            )

        # Match in priority order
        for tag in tags:
            slug = tag.get("slug", "").lower()
            label = tag.get("label", "").lower()

            # Exact slug matches
            if slug in ("weather", "climate-weather", "climate-&-weather",
                         "climate--weather", "temperature"):
                tag_id = str(tag.get("id", ""))
                log.info(f"Found weather tag: id={tag_id}, label={tag.get('label')}")
                return tag_id

        # Fuzzy label matches (second pass)
        for tag in tags:
            label = tag.get("label", "").lower()
            if "weather" in label and "space" not in label:
                tag_id = str(tag.get("id", ""))
                log.info(f"Found weather tag (fuzzy): id={tag_id}, label={tag.get('label')}")
                return tag_id

    except httpx.HTTPError as e:
        log.warning(f"Failed to fetch tags: {e}")

    return None


async def _fallback_slug_search(client: httpx.AsyncClient) -> list[dict]:
    """
    Fallback: paginate through all active events and filter client-side.
    
    This is slower (~10 API calls) but guaranteed to find temperature
    markets regardless of tagging. Used when tag discovery fails.
    """
    events = []
    seen_ids = set()
    offset = 0
    limit = 50
    max_pages = 15  # 750 events max — covers all active markets

    log.info("Fallback: paginating all active events to find temperature markets...")

    for page in range(max_pages):
        try:
            resp = await client.get(
                config.GAMMA_EVENTS_URL,
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=config.HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            if not isinstance(data, list) or len(data) == 0:
                break

            for event in data:
                eid = event.get("id", "")
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    events.append(event)

            if len(data) < limit:
                break  # Last page

            offset += limit

        except httpx.HTTPError as e:
            log.warning(f"Fallback page {page} failed: {e}")
            break

    log.info(f"Fallback: scanned {len(events)} total active events")
    return events


def _extract_outcomes(market: dict) -> list[dict]:
    """Extract outcome data from a Gamma market dict."""
    outcomes = []

    # Gamma format: outcomes is a JSON string or list
    raw_outcomes = market.get("outcomes", "[]")
    if isinstance(raw_outcomes, str):
        import json
        try:
            raw_outcomes = json.loads(raw_outcomes)
        except (json.JSONDecodeError, TypeError):
            raw_outcomes = []

    outcome_prices = market.get("outcomePrices", "[]")
    if isinstance(outcome_prices, str):
        import json
        try:
            outcome_prices = json.loads(outcome_prices)
        except (json.JSONDecodeError, TypeError):
            outcome_prices = []

    clob_token_ids = market.get("clobTokenIds", "[]")
    if isinstance(clob_token_ids, str):
        import json
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except (json.JSONDecodeError, TypeError):
            clob_token_ids = []

    for i, outcome_name in enumerate(raw_outcomes):
        price = float(outcome_prices[i]) if i < len(outcome_prices) else 0.0
        token_id = clob_token_ids[i] if i < len(clob_token_ids) else ""
        outcomes.append({
            "name": outcome_name,
            "price": price,
            "token_id": token_id,
        })

    return outcomes


def parse_events_to_markets(events: list[dict]) -> list[WeatherMarket]:
    """
    Parse raw Gamma events into structured WeatherMarket objects.
    
    Each event is a "Highest temperature in [City] on [Date]?" question
    with multiple sub-markets (brackets).
    """
    markets = []

    for event in events:
        title = event.get("title", "")
        slug = event.get("slug", "")
        event_id = event.get("id", "")

        # Match city
        city_slug = match_city(title)
        if not city_slug:
            log.debug(f"No city match for event: {title}")
            continue

        # Parse date
        target = parse_target_date(title)
        if not target:
            log.debug(f"No date parsed from event: {title}")
            continue

        # Skip past dates
        today = datetime.now(timezone.utc).date()
        if target < today:
            continue

        city_cfg = config.CITIES[city_slug]

        resolution_url, declared_station, declared_precision = extract_resolution_metadata(event, city_slug)

        wm = WeatherMarket(
            event_id=event_id,
            event_slug=slug,
            city=city_slug,
            target_date=target,
            question=title,
            total_volume=float(event.get("volume", 0) or 0),
            resolution_url=resolution_url,
            declared_station=declared_station,
            declared_precision=declared_precision,
        )

        # Parse each sub-market as a bracket
        sub_markets = event.get("markets", [])
        for mkt in sub_markets:
            if not mkt.get("active", True):
                continue

            group_label = mkt.get("groupItemTitle", "") or mkt.get("question", "")
            lower, upper, unit = parse_bracket_label(group_label)

            if not unit:
                # Couldn't parse — skip
                continue

            # Get YES and NO outcome prices and tokens
            outcomes = _extract_outcomes(mkt)
            yes_outcome = next(
                (o for o in outcomes if o["name"].lower() == "yes"),
                outcomes[0] if outcomes else None,
            )
            no_outcome = next(
                (o for o in outcomes if o["name"].lower() == "no"),
                outcomes[1] if len(outcomes) > 1 else None,
            )

            if not yes_outcome:
                continue

            bracket = Bracket(
                token_id=yes_outcome["token_id"],
                no_token_id=no_outcome["token_id"] if no_outcome else "",
                label=group_label,
                lower=lower,
                upper=upper,
                unit=unit,
                market_prob=yes_outcome["price"],
                condition_id=mkt.get("conditionId", ""),
            )
            wm.brackets.append(bracket)

        if wm.brackets:
            # Sort brackets by lower bound
            wm.brackets.sort(
                key=lambda b: (b.lower if b.lower is not None else float("-inf"))
            )
            markets.append(wm)
            log.info(
                f"Parsed market: {city_cfg.name} on {target} — "
                f"{len(wm.brackets)} brackets, vol=${wm.total_volume:,.0f}"
            )

    return markets


async def discover_markets(
    client: httpx.AsyncClient | None = None,
) -> list[WeatherMarket]:
    """
    Full discovery pipeline: fetch events → parse → return structured markets.
    
    Optionally pass an existing httpx.AsyncClient for connection reuse.
    """
    if client is None:
        async with httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT}
        ) as c:
            events = await fetch_weather_events(c)
            return parse_events_to_markets(events)
    else:
        events = await fetch_weather_events(client)
        return parse_events_to_markets(events)
