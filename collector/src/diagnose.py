"""
Wethr diagnostics — validate each data source independently.

Usage:
    python -m src.diagnose                  # Run all checks
    python -m src.diagnose gamma            # Only Gamma API
    python -m src.diagnose ensemble         # Only Open-Meteo ensemble
    python -m src.diagnose ensemble --city nyc
    python -m src.diagnose nws              # Only NWS observations
    python -m src.diagnose nws --city nyc --date 2026-03-17

Run this FIRST before trusting the trading pipeline.
It surfaces response format mismatches, parsing failures, and API issues.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

from . import config
from .markets import (
    fetch_weather_events,
    parse_events_to_markets,
    parse_bracket_label,
)
from .ensemble import (
    _build_ensemble_url,
    _parse_ensemble_response,
    c_to_f,
)
from .settlement import fetch_nws_observed_high, fetch_openmeteo_observed_high

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Suppress HTTP connection noise — we only care about request/response summaries
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("diagnose")


def _header(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Gamma API diagnostics
# ---------------------------------------------------------------------------

async def diagnose_gamma(client: httpx.AsyncClient) -> bool:
    """Test Gamma API: fetch events, parse brackets, report anomalies."""
    _header("GAMMA API — Market Discovery")
    ok = True

    # Step 1: Raw fetch
    print("\n[1/3] Fetching weather events from Gamma API...")
    try:
        events = await fetch_weather_events(client)
        print(f"  ✅ Got {len(events)} events")
    except Exception as e:
        print(f"  ❌ Fetch failed: {e}")
        return False

    if not events:
        print("  ⚠️  No events found — markets may be closed or search terms may need updating")
        return True  # Not a code error

    # Step 2: Show raw event structure for first event
    print(f"\n[2/3] First event structure:")
    first = events[0]
    print(f"  title: {first.get('title', 'N/A')}")
    print(f"  id:    {first.get('id', 'N/A')}")
    print(f"  slug:  {first.get('slug', 'N/A')}")
    sub_markets = first.get("markets", [])
    print(f"  sub-markets: {len(sub_markets)}")
    if sub_markets:
        sm = sub_markets[0]
        print(f"  first sub-market keys: {sorted(sm.keys())}")
        print(f"  groupItemTitle: {sm.get('groupItemTitle', 'N/A')}")
        print(f"  outcomes: {sm.get('outcomes', 'N/A')}")
        print(f"  outcomePrices: {sm.get('outcomePrices', 'N/A')}")
        print(f"  clobTokenIds: {sm.get('clobTokenIds', 'N/A')}")

    # Step 3: Parse and validate
    print(f"\n[3/3] Parsing events into markets...")
    markets = parse_events_to_markets(events)
    print(f"  ✅ Parsed {len(markets)} markets")

    for mkt in markets:
        city_cfg = config.CITIES.get(mkt.city)
        city_name = city_cfg.name if city_cfg else mkt.city
        print(f"\n  📍 {city_name} — {mkt.target_date}")
        print(f"     Volume: ${mkt.total_volume:,.0f}")
        print(f"     Brackets ({len(mkt.brackets)}):")

        prob_sum = 0.0
        for b in mkt.brackets:
            prob_sum += b.market_prob
            lo = f"{b.lower:.0f}" if b.lower is not None else "—"
            hi = f"{b.upper:.0f}" if b.upper is not None else "—"
            print(f"       [{lo:>4s}, {hi:>4s})°{b.unit}  price={b.market_prob:.2f}  {b.label}")

        # Sanity checks
        if abs(prob_sum - 1.0) > 0.15:
            print(f"     ⚠️  Prices sum to {prob_sum:.2f} (expected ~1.0)")
            ok = False
        else:
            print(f"     ✅ Prices sum to {prob_sum:.2f}")

        # Check for unparsed brackets
        unparsed = [b for b in mkt.brackets if b.lower is None and b.upper is None]
        if unparsed:
            print(f"     ❌ {len(unparsed)} brackets failed to parse!")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# Ensemble API diagnostics
# ---------------------------------------------------------------------------

async def diagnose_ensemble(
    client: httpx.AsyncClient,
    city_slug: str = "nyc",
) -> bool:
    """Test Open-Meteo ensemble API: fetch each model, validate response shape."""
    city = config.CITIES.get(city_slug)
    if not city:
        print(f"❌ Unknown city: {city_slug}")
        return False

    _header(f"ENSEMBLE API — {city.name}")
    ok = True

    for model in config.ENSEMBLE_MODELS:
        expected_members = config.MODEL_MEMBER_COUNTS.get(model, "?")
        print(f"\n  [{model}] (expected: {expected_members} members)")

        url = _build_ensemble_url(city, model)
        print(f"  URL: {url}")

        try:
            resp = await client.get(url, timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 400:
                # Show the API error message — helps debug model name issues
                try:
                    err_body = resp.json()
                    print(f"  ❌ 400 Bad Request: {err_body.get('reason', resp.text[:200])}")
                except Exception:
                    print(f"  ❌ 400 Bad Request: {resp.text[:200]}")
                ok = False
                continue
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            print(f"  ❌ HTTP error: {e}")
            ok = False
            continue
        except Exception as e:
            print(f"  ❌ Parse error: {e}")
            ok = False
            continue

        # Check response shape
        if "error" in data and data["error"]:
            print(f"  ❌ API error: {data.get('reason', 'unknown')}")
            ok = False
            continue

        daily = data.get("daily", {})
        times = daily.get("time", [])
        member_keys = [k for k in daily.keys() if "member" in k]

        print(f"  Response: lat={data.get('latitude')}, lon={data.get('longitude')}")
        print(f"  Dates: {len(times)} ({times[0] if times else 'N/A'} → {times[-1] if times else 'N/A'})")
        print(f"  Member keys: {len(member_keys)}")

        if member_keys:
            # Show first member's first few values
            first_key = sorted(member_keys)[0]
            vals = daily[first_key][:3]
            print(f"  Sample ({first_key}): {vals}")

            # Parse and verify
            parsed = _parse_ensemble_response(data, city_slug, model)
            if parsed:
                first_date = sorted(parsed.keys())[0]
                members = parsed[first_date]
                print(
                    f"  ✅ Parsed: {len(members)} members for {first_date}, "
                    f"range: {min(m.daily_max for m in members):.1f}°C – "
                    f"{max(m.daily_max for m in members):.1f}°C"
                )
                if isinstance(expected_members, int) and len(members) != expected_members:
                    print(
                        f"  ⚠️  Expected {expected_members} members, got {len(members)}"
                    )
            else:
                print(f"  ❌ Parse returned no data")
                ok = False
        else:
            print(f"  ❌ No member keys in response — wrong variable name?")
            print(f"  Available keys: {sorted(daily.keys())}")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# NWS / Open-Meteo observation diagnostics
# ---------------------------------------------------------------------------

async def diagnose_nws(
    client: httpx.AsyncClient,
    city_slug: str = "nyc",
    target_date: date | None = None,
) -> bool:
    """Test observation fetching: NWS for US, Open-Meteo for international."""
    city = config.CITIES.get(city_slug)
    if not city:
        print(f"❌ Unknown city: {city_slug}")
        return False

    if target_date is None:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    _header(f"OBSERVATIONS — {city.name} on {target_date}")
    ok = True

    # Try NWS for US stations
    if city.station.startswith("K"):
        print(f"\n  [NWS API] Station: {city.station}")
        try:
            temp_c = await fetch_nws_observed_high(client, city.station, target_date)
            if temp_c is not None:
                temp_f = c_to_f(temp_c)
                print(f"  ✅ Daily high: {temp_c:.1f}°C / {temp_f:.1f}°F")
            else:
                print(f"  ⚠️  No data returned (may not be available yet)")
        except Exception as e:
            print(f"  ❌ Error: {e}")
            ok = False
    else:
        print(f"  (NWS not available for non-US station {city.station})")

    # Try Open-Meteo historical
    print(f"\n  [Open-Meteo Historical] ({city.lat}, {city.lon})")
    try:
        temp_c = await fetch_openmeteo_observed_high(
            client, city.lat, city.lon, target_date
        )
        if temp_c is not None:
            temp_f = c_to_f(temp_c)
            print(f"  ✅ Daily high: {temp_c:.1f}°C / {temp_f:.1f}°F")
        else:
            print(f"  ⚠️  No data (historical API has ~24h delay)")
    except Exception as e:
        print(f"  ❌ Error: {e}")
        ok = False

    return ok


# ---------------------------------------------------------------------------
# Bracket parsing stress test
# ---------------------------------------------------------------------------

def diagnose_parsing() -> bool:
    """Test bracket label parsing against known formats."""
    _header("BRACKET LABEL PARSING")

    test_cases = [
        # (input_label, expected_lower, expected_upper, expected_unit)
        ("72°F - 74°F", 72, 74, "F"),
        ("72°F to 74°F", 72, 74, "F"),
        ("72°F – 74°F", 72, 74, "F"),  # en-dash
        ("Below 68°F", None, 68, "F"),
        ("76°F or above", 76, None, "F"),
        ("76°F+", 76, None, "F"),
        ("22°C - 23°C", 22, 23, "C"),
        ("Below 20°C", None, 20, "C"),
        ("24°C or above", 24, None, "C"),
        ("24°C or more", 24, None, "C"),
        ("Under 65°F", None, 65, "F"),
        ("Above 80°F", 80, None, "F"),
        ("Less than 15°C", None, 15, "C"),
        ("Greater than 30°C", 30, None, "C"),
        # Known edge cases to test
        ("68°F or higher", 68, None, "F"),
        ("< 60°F", None, 60, "F"),
        ("> 85°F", 85, None, "F"),
    ]

    ok = True
    for label, exp_lo, exp_hi, exp_unit in test_cases:
        lo, hi, unit = parse_bracket_label(label)

        match = (lo == exp_lo and hi == exp_hi and unit == exp_unit)
        status = "✅" if match else "❌"
        if not match:
            ok = False

        print(
            f"  {status} {label!r:30s} → "
            f"lo={lo}, hi={hi}, unit={unit}"
            f"{'' if match else f'  (expected lo={exp_lo}, hi={exp_hi}, unit={exp_unit})'}"
        )

    return ok


# ---------------------------------------------------------------------------
# Full diagnostic
# ---------------------------------------------------------------------------

async def run_all(city_slug: str = "nyc") -> bool:
    results = {}

    # Offline tests first
    results["parsing"] = diagnose_parsing()

    # Online tests
    async with httpx.AsyncClient(
        headers={"User-Agent": config.USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    ) as client:
        results["gamma"] = await diagnose_gamma(client)
        results["ensemble"] = await diagnose_ensemble(client, city_slug)
        results["nws"] = await diagnose_nws(client, city_slug)

    # Summary
    _header("SUMMARY")
    all_ok = True
    for name, ok in results.items():
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {status}  {name}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\n  All checks passed — ready to scan.")
    else:
        print("\n  Fix failures above before running the trading pipeline.")

    return all_ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Wethr diagnostics")
    parser.add_argument(
        "check", nargs="?", default="all",
        choices=["all", "gamma", "ensemble", "nws", "parsing"],
        help="Which check to run",
    )
    parser.add_argument("--city", default="nyc", help="City slug for tests")
    parser.add_argument("--date", default=None, help="Date for NWS check (YYYY-MM-DD)")

    args = parser.parse_args()

    if args.check == "parsing":
        ok = diagnose_parsing()
        sys.exit(0 if ok else 1)

    async def _run():
        async with httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.HTTP_TIMEOUT,
        ) as client:
            if args.check == "all":
                return await run_all(args.city)
            elif args.check == "gamma":
                return await diagnose_gamma(client)
            elif args.check == "ensemble":
                return await diagnose_ensemble(client, args.city)
            elif args.check == "nws":
                target = date.fromisoformat(args.date) if args.date else None
                return await diagnose_nws(client, args.city, target)
        return False

    ok = asyncio.run(_run())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
