"""
Settlement engine — resolve trades using Polymarket's own outcomes.

Primary method: Gamma API
    Fetch closed weather events, match sub-markets to our trades/signals
    by condition_id or bracket_label, read outcomePrices to determine winner.
    This is authoritative — it's exactly what Polymarket used to settle.

Fallback method: NWS / Open-Meteo observed temperatures
    Only used if Gamma hasn't resolved the market yet (rare edge case).
    Kept for backward compatibility and manual verification.

Settlement flow:
1. Find unsettled trades/signals for a target date
2. Fetch closed weather events from Gamma API
3. Match each trade's condition_id to a resolved sub-market
4. outcomePrices "[1, 0]" = YES won (bracket hit), "[0, 1]" = NO won
5. Settle trades and signals accordingly
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import config
from .ensemble import c_to_f
from .markets import match_city, parse_target_date, parse_bracket_label
from .paper_trader import (
    get_db,
    settle_trade,
    settle_signal,
    mark_trades_redeemed,
    get_winning_unredeemed_conditions,
)
from .trading import TradingClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gamma API: fetch resolved outcomes
# ---------------------------------------------------------------------------

async def fetch_resolved_weather_events(
    client: httpx.AsyncClient,
    target_date: date,
) -> list[dict]:
    """
    Fetch closed weather temperature events from Gamma API for a specific date.

    Uses end_date_min/max to jump directly to the right date range
    instead of paginating through all historical weather events.
    """
    events = []
    offset = 0
    limit = 50
    max_pages = 5

    for _ in range(max_pages):
        try:
            resp = await client.get(
                config.GAMMA_EVENTS_URL,
                params={
                    "active": "false",
                    "closed": "true",
                    "tag_slug": "weather",
                    "limit": limit,
                    "offset": offset,
                    "end_date_min": target_date.isoformat(),
                    "end_date_max": (target_date + timedelta(days=1)).isoformat(),
                },
                timeout=config.HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            if not isinstance(data, list) or len(data) == 0:
                break

            for event in data:
                title = event.get("title", "").lower()
                if "highest temperature" not in title:
                    continue

                # Verify date match (belt and suspenders)
                event_date = parse_target_date(event.get("title", ""))
                if event_date == target_date:
                    events.append(event)

            if len(data) < limit:
                break
            offset += limit

        except httpx.HTTPError as e:
            log.warning(f"Gamma API error fetching resolved events: {e}")
            break

    log.info(f"Found {len(events)} resolved weather events for {target_date}")
    return events


def extract_outcomes(event: dict) -> dict[str, bool]:
    """
    Extract resolution outcomes from a Gamma event.

    Returns: condition_id -> True if YES won (bracket hit)

    outcomePrices "[1, 0]" means YES=$1, NO=$0 -> YES won -> bracket hit
    outcomePrices "[0, 1]" means YES=$0, NO=$1 -> NO won -> bracket didn't hit
    """
    outcomes = {}

    for mkt in event.get("markets", []):
        condition_id = mkt.get("conditionId", "")
        resolution_status = mkt.get("umaResolutionStatus", "")

        if resolution_status != "resolved":
            continue

        # Parse outcomePrices
        prices_raw = mkt.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            prices = prices_raw

        if len(prices) < 2:
            continue

        # YES price = 1 means bracket hit
        yes_price = float(prices[0])
        yes_won = yes_price > 0.5

        if condition_id:
            outcomes[condition_id] = yes_won

        # Also index by bracket label for signal matching
        bracket_label = mkt.get("groupItemTitle", "") or mkt.get("question", "")
        if bracket_label:
            outcomes[f"label:{bracket_label}"] = yes_won

    return outcomes


def extract_outcomes_by_label(event: dict) -> dict[str, bool]:
    """
    Extract resolution outcomes indexed by bracket label.

    Returns: bracket_label -> True if YES won (bracket hit)
    Used for matching signals which don't store condition_id.
    """
    outcomes = {}

    for mkt in event.get("markets", []):
        if mkt.get("umaResolutionStatus", "") != "resolved":
            continue

        prices_raw = mkt.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            prices = prices_raw

        if len(prices) < 2:
            continue

        yes_price = float(prices[0])
        yes_won = yes_price > 0.5

        bracket_label = mkt.get("groupItemTitle", "") or mkt.get("question", "")
        if bracket_label:
            outcomes[bracket_label] = yes_won

    return outcomes


# ---------------------------------------------------------------------------
# Settlement pipeline (Gamma-first, NWS/Open-Meteo fallback)
# ---------------------------------------------------------------------------

async def settle_date(
    client: httpx.AsyncClient,
    target_date: date,
    db_path: Path | None = None,
    trading_client: TradingClient | None = None,
) -> dict:
    """
    Settle all trades and signals for a given date.

    Uses Polymarket's Gamma API as the primary resolution source.
    Falls back to NWS/Open-Meteo observed temperatures if Gamma
    hasn't resolved yet.
    """
    summary = {
        "date": target_date.isoformat(),
        "trades_settled": 0,
        "signals_settled": 0,
        "total_pnl": 0.0,
        "cities": {},
        "redeemed": 0,
        "redeem_failed": 0,
    }

    with get_db(db_path) as conn:
        trades = conn.execute(
            "SELECT * FROM trades WHERE target_date = ? AND settled = 0",
            (target_date.isoformat(),),
        ).fetchall()

        signals = conn.execute(
            "SELECT * FROM signals WHERE target_date = ? AND outcome IS NULL",
            (target_date.isoformat(),),
        ).fetchall()

    if not trades and not signals:
        log.info(f"Nothing to settle for {target_date}")
        return summary

    # --- Primary: Gamma API resolution ---
    events = await fetch_resolved_weather_events(client, target_date)

    # Build lookup maps: condition_id -> outcome, and (city, label) -> outcome
    condition_outcomes: dict[str, bool] = {}
    label_outcomes: dict[tuple[str, str], bool] = {}  # (city_slug, bracket_label) -> bool

    for event in events:
        city_slug = match_city(event.get("title", ""))
        if not city_slug:
            continue

        outcomes = extract_outcomes(event)
        for key, won in outcomes.items():
            if key.startswith("label:"):
                label_outcomes[(city_slug, key[6:])] = won
            else:
                condition_outcomes[key] = won

        # Also build label-based outcomes for signal matching
        label_results = extract_outcomes_by_label(event)
        for label, won in label_results.items():
            label_outcomes[(city_slug, label)] = won

    gamma_resolved = len(condition_outcomes) > 0 or len(label_outcomes) > 0
    if gamma_resolved:
        log.info(
            f"Gamma resolution: {len(condition_outcomes)} conditions, "
            f"{len(label_outcomes)} labels resolved"
        )

    # --- Settle trades ---
    trades_remaining = []
    for trade in trades:
        condition_id = trade["condition_id"]
        city = trade["city"]
        bracket_label = trade["bracket_label"]

        # Try condition_id match first (most reliable)
        if condition_id and condition_id in condition_outcomes:
            outcome = condition_outcomes[condition_id]
            pnl = settle_trade(trade["id"], outcome, db_path)
            summary["trades_settled"] += 1
            summary["total_pnl"] += pnl
            city_summary = summary["cities"].setdefault(city, {"pnl": 0.0, "trades": 0})
            city_summary["pnl"] += pnl
            city_summary["trades"] += 1
            log.info(f"  Trade #{trade['id']} settled via Gamma (condition_id)")
            continue

        # Try label match
        if (city, bracket_label) in label_outcomes:
            outcome = label_outcomes[(city, bracket_label)]
            pnl = settle_trade(trade["id"], outcome, db_path)
            summary["trades_settled"] += 1
            summary["total_pnl"] += pnl
            city_summary = summary["cities"].setdefault(city, {"pnl": 0.0, "trades": 0})
            city_summary["pnl"] += pnl
            city_summary["trades"] += 1
            log.info(f"  Trade #{trade['id']} settled via Gamma (label match)")
            continue

        trades_remaining.append(trade)

    # --- Settle signals ---
    signals_remaining = []
    # Track winning bracket per city to compute resolved_value
    city_winning_bracket: dict[str, str] = {}  # city -> winning bracket_label

    for signal in signals:
        city = signal["city"]
        bracket_label = signal["bracket_label"]

        if (city, bracket_label) in label_outcomes:
            outcome = label_outcomes[(city, bracket_label)]
            settle_signal(signal["id"], outcome, db_path)
            summary["signals_settled"] += 1
            if outcome:
                city_winning_bracket[city] = bracket_label
        else:
            signals_remaining.append(signal)

    # Compute and store resolved_value (winning bracket midpoint) for EMOS training
    for city, winning_label in city_winning_bracket.items():
        lower, upper, unit = parse_bracket_label(winning_label)
        if lower is not None and upper is not None:
            resolved_value = (lower + upper) / 2.0
        elif lower is not None:
            # "X or higher" tail — use lower + 0.5
            resolved_value = lower + 0.5
        elif upper is not None:
            # "X or below" tail — use upper - 0.5
            resolved_value = upper - 0.5
        else:
            continue

        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE signals SET resolved_value = ?
                WHERE city = ? AND target_date = ? AND outcome IS NOT NULL
                """,
                (resolved_value, city, target_date.isoformat()),
            )
        log.debug(
            f"  Resolved value for {city} {target_date}: "
            f"{resolved_value:.1f}°{unit} (bracket: {winning_label})"
        )

    # --- Anything Gamma didn't resolve: just wait ---
    unresolved_trades = len(trades_remaining)
    unresolved_signals = len(signals_remaining)
    if unresolved_trades or unresolved_signals:
        log.info(
            f"Gamma didn't resolve {unresolved_trades} trades, "
            f"{unresolved_signals} signals for {target_date} — "
            f"will retry on next settlement cycle"
        )

    # --- Auto-close: redeem winning positions on-chain ---
    if trading_client is not None:
        winning_conditions = get_winning_unredeemed_conditions(db_path)
        if winning_conditions:
            log.info(
                f"Auto-redeem: {len(winning_conditions)} winning condition(s) "
                f"to close on-chain"
            )
            for condition_id in winning_conditions:
                if trading_client.redeem_position(condition_id):
                    n = mark_trades_redeemed(condition_id, db_path)
                    summary["redeemed"] += n
                else:
                    summary["redeem_failed"] += 1

    log.info(
        f"Settlement for {target_date}: "
        f"{summary['trades_settled']} trades, "
        f"{summary['signals_settled']} signals, "
        f"P&L: ${summary['total_pnl']:+.2f}, "
        f"redeemed: {summary['redeemed']}"
    )
    return summary


# ---------------------------------------------------------------------------
# Observation fetching (used by diagnose.py for API validation)
# ---------------------------------------------------------------------------

async def fetch_nws_observed_high(
    client: httpx.AsyncClient,
    station: str,
    target_date: date,
    tz_name: str = "America/New_York",
) -> float | None:
    """Fetch observed daily max temperature from NWS API."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
    except (ImportError, KeyError):
        tz = timezone.utc

    local_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    start_str = local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = local_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"https://api.weather.gov/stations/{station}/observations"
        f"?start={start_str}&end={end_str}"
    )

    try:
        resp = await client.get(
            url,
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/geo+json"},
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning(f"NWS API error for {station}: {e}")
        return None

    data = resp.json()
    features = data.get("features", [])
    if not features:
        return None

    temps = []
    for obs in features:
        value = obs.get("properties", {}).get("temperature", {}).get("value")
        if value is not None:
            temps.append(float(value))

    if not temps:
        return None

    max_c = max(temps)
    log.info(f"NWS observed high for {station} on {target_date}: {max_c:.1f} deg C / {c_to_f(max_c):.1f} deg F")
    return max_c


async def fetch_openmeteo_observed_high(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    target_date: date,
    tz_name: str = "GMT",
) -> float | None:
    """Fetch observed daily max from Open-Meteo Historical Weather API."""
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=celsius"
        f"&timezone={tz_name}"
        f"&start_date={target_date.isoformat()}"
        f"&end_date={target_date.isoformat()}"
    )

    try:
        resp = await client.get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning(f"Open-Meteo historical error: {e}")
        return None

    data = resp.json()
    temps = data.get("daily", {}).get("temperature_2m_max", [])
    if not temps or temps[0] is None:
        return None

    max_c = float(temps[0])
    log.info(
        f"Open-Meteo observed high for ({lat:.2f}, {lon:.2f}) on {target_date}: "
        f"{max_c:.1f} deg C / {c_to_f(max_c):.1f} deg F"
    )
    return max_c


async def get_observed_high(
    client: httpx.AsyncClient,
    city_slug: str,
    target_date: date,
) -> tuple[float | None, str]:
    """Get observed daily high, trying NWS first then Open-Meteo."""
    city = config.CITIES.get(city_slug)
    if not city:
        return None, ""

    if city.station.startswith("K"):
        temp = await fetch_nws_observed_high(
            client, city.station, target_date, tz_name=city.timezone,
        )
        if temp is not None:
            return temp, "NWS"

    temp = await fetch_openmeteo_observed_high(
        client, city.lat, city.lon, target_date, tz_name=city.timezone,
    )
    if temp is not None:
        return temp, "Open-Meteo"

    return None, ""


async def settle_yesterday(
    client: httpx.AsyncClient | None = None,
    db_path: Path | None = None,
    trading_client: TradingClient | None = None,
) -> dict:
    """Settle all trades for yesterday."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    if client is None:
        async with httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.HTTP_TIMEOUT,
        ) as c:
            return await settle_date(c, yesterday, db_path, trading_client)
    else:
        return await settle_date(client, yesterday, db_path, trading_client)
