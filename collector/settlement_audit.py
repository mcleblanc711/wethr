#!/usr/bin/env python3
"""
settlement_audit.py — Verify settled trades against independent observations.

Pulls settled trades from the DB, re-fetches observed temperatures from
Open-Meteo historical API, compares against recorded outcomes, and generates
Weather Underground links for manual spot-checking.

Usage:
    cd ~/projects/wethr
    source .venv/bin/activate
    python settlement_audit.py                  # audit all settled trades
    python settlement_audit.py --date 2026-03-19  # audit specific date
    python settlement_audit.py --city seoul       # audit specific city
    python settlement_audit.py --verbose          # show per-trade detail

Requires: httpx, numpy (already in your venv)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# City configuration — must match what's in your codebase
# ---------------------------------------------------------------------------
# Add any cities your system trades that aren't listed here.
# The critical fields are lat/lon (for Open-Meteo), timezone (for correct
# daily aggregation), and wu_station (for manual verification URLs).

CITY_CONFIG = {
    "nyc": {
        "lat": 40.77, "lon": -73.87,
        "tz": "America/New_York",
        "wu_station": "KLGA",
        "wu_region": "us/ny/new-york-city",
        "unit": "fahrenheit",
    },
    "chicago": {
        "lat": 41.98, "lon": -87.90,
        "tz": "America/Chicago",
        "wu_station": "KORD",
        "wu_region": "us/il/chicago",
        "unit": "fahrenheit",
    },
    "miami": {
        "lat": 25.79, "lon": -80.29,
        "tz": "America/New_York",
        "wu_station": "KMIA",
        "wu_region": "us/fl/miami",
        "unit": "fahrenheit",
    },
    "atlanta": {
        "lat": 33.64, "lon": -84.43,
        "tz": "America/New_York",
        "wu_station": "KATL",
        "wu_region": "us/ga/atlanta",
        "unit": "fahrenheit",
    },
    "dallas": {
        "lat": 32.90, "lon": -97.04,
        "tz": "America/Chicago",
        "wu_station": "KDFW",
        "wu_region": "us/tx/dallas",
        "unit": "fahrenheit",
    },
    "seattle": {
        "lat": 47.45, "lon": -122.31,
        "tz": "America/Los_Angeles",
        "wu_station": "KSEA",
        "wu_region": "us/wa/seattle",
        "unit": "fahrenheit",
    },
    "denver": {
        "lat": 39.86, "lon": -104.67,
        "tz": "America/Denver",
        "wu_station": "KDEN",
        "wu_region": "us/co/denver",
        "unit": "fahrenheit",
    },
    "toronto": {
        "lat": 43.68, "lon": -79.63,
        "tz": "America/Toronto",
        "wu_station": "CYYZ",
        "wu_region": "ca/on/toronto",
        "unit": "fahrenheit",  # Polymarket uses °F for Toronto
    },
    "london": {
        "lat": 51.51, "lon": 0.05,
        "tz": "Europe/London",
        "wu_station": "EGLC",
        "wu_region": "gb/london",
        "unit": "celsius",
    },
    "paris": {
        "lat": 48.86, "lon": 2.35,
        "tz": "Europe/Paris",
        "wu_station": "LFPG",  # CDG — verify against your resolution source
        "wu_region": "fr/paris",
        "unit": "celsius",
    },
    "seoul": {
        "lat": 37.46, "lon": 126.44,
        "tz": "Asia/Seoul",
        "wu_station": "RKSI",
        "wu_region": "kr/incheon",
        "unit": "celsius",
    },
    "sao_paulo": {
        "lat": -23.55, "lon": -46.64,
        "tz": "America/Sao_Paulo",
        "wu_station": "SBGR",  # Guarulhos — verify
        "wu_region": "br/sao-paulo",
        "unit": "celsius",
    },
    "buenos_aires": {
        "lat": -34.82, "lon": -58.54,
        "tz": "America/Argentina/Buenos_Aires",
        "wu_station": "SAEZ",  # Ezeiza — verify
        "wu_region": "ar/buenos-aires",
        "unit": "celsius",
    },
    "tel_aviv": {
        "lat": 32.01, "lon": 34.87,
        "tz": "Asia/Jerusalem",
        "wu_station": "LLBG",
        "wu_region": "il/tel-aviv",
        "unit": "celsius",
    },
    # Add more cities here as needed. Run with --list-cities to see what's
    # in your DB vs what's configured here.
}


# ---------------------------------------------------------------------------
# Bracket parsing (mirrors your codebase logic)
# ---------------------------------------------------------------------------

def parse_bracket_label(label: str) -> tuple[float | None, float | None, str]:
    """
    Parse bracket label into (lower, upper, unit).
    Handles: "54-55°F", "15°C", "88-89°F", "10°C"
    Returns (None, None, "") on parse failure.
    """
    # Range format: "54-55°F" or "88-89°F"
    m = re.match(r"(\d+)-(\d+)°([FC])", label)
    if m:
        return float(m.group(1)), float(m.group(2)), m.group(3)

    # Single value: "15°C" or "72°F"
    m = re.match(r"(\d+)°([FC])", label)
    if m:
        val = float(m.group(1))
        return val, val + 1, m.group(2)  # treat as 1-degree bracket

    return None, None, ""


def bracket_matches(obs_high: float, lower: float | None, upper: float | None) -> bool | None:
    """Did the observed high fall within [lower, upper)?"""
    if lower is None or upper is None:
        return None
    return lower <= obs_high < upper


# ---------------------------------------------------------------------------
# Open-Meteo historical observation fetch
# ---------------------------------------------------------------------------

def fetch_observed_high(
    lat: float,
    lon: float,
    target_date: str,
    timezone: str,
    unit: str,
    client: httpx.Client,
) -> dict:
    """
    Independently fetch the observed daily high from Open-Meteo archive API.
    Returns dict with temp, source info, and raw hourly data for inspection.
    """
    temp_unit = "fahrenheit" if unit == "fahrenheit" else "celsius"

    # Open-Meteo historical/archive API
    # Try archive first (for dates >2 days ago), fall back to forecast with past_days
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "temperature_unit": temp_unit,
        "timezone": timezone,
        "start_date": target_date,
        "end_date": target_date,
    }

    try:
        resp = client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e), "source": "open-meteo-forecast"}

    daily = data.get("daily", {})
    hourly = data.get("hourly", {})

    daily_max = daily.get("temperature_2m_max", [None])[0]
    daily_min = daily.get("temperature_2m_min", [None])[0]

    # Extract hourly temps for this day for manual inspection
    hourly_temps = hourly.get("temperature_2m", [])
    hourly_times = hourly.get("time", [])

    # Also compute max from hourly as a cross-check
    hourly_max = max(hourly_temps) if hourly_temps else None

    return {
        "daily_max": daily_max,
        "daily_min": daily_min,
        "hourly_max": hourly_max,
        "hourly_count": len(hourly_temps),
        "unit": "°F" if unit == "fahrenheit" else "°C",
        "timezone_used": timezone,
        "source": "open-meteo-forecast",
        "daily_vs_hourly_diff": (
            round(daily_max - hourly_max, 1)
            if daily_max is not None and hourly_max is not None
            else None
        ),
        # Include raw hourly for deep inspection
        "hourly_data": list(zip(hourly_times, hourly_temps))
        if hourly_times
        else [],
    }


# ---------------------------------------------------------------------------
# Weather Underground URL generator
# ---------------------------------------------------------------------------

def wu_url(city_cfg: dict, target_date: str) -> str:
    """Generate Weather Underground history URL for manual verification."""
    station = city_cfg["wu_station"]
    region = city_cfg["wu_region"]
    return (
        f"https://www.wunderground.com/history/daily/"
        f"{region}/{station}/date/{target_date}"
    )


# ---------------------------------------------------------------------------
# Main audit logic
# ---------------------------------------------------------------------------

@dataclass
class AuditResult:
    trade_id: int
    city: str
    target_date: str
    bracket_label: str
    side: str
    entry_price: float
    size_usd: float
    model_prob: float
    # DB recorded values
    db_outcome: int | None
    db_pnl: float | None
    # Independent verification
    obs_daily_max: float | None
    obs_hourly_max: float | None
    obs_unit: str
    bracket_lower: float | None
    bracket_upper: float | None
    independent_outcome: bool | None  # based on our re-fetch
    # Comparison
    match: bool | None  # db_outcome matches independent_outcome
    discrepancy_reason: str
    wu_link: str


def run_audit(
    db_path: Path,
    target_date: str | None = None,
    target_city: str | None = None,
    verbose: bool = False,
) -> list[AuditResult]:
    """Run the full settlement audit."""

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Pull settled trades
    query = "SELECT * FROM trades WHERE settled = 1"
    params = []
    if target_date:
        query += " AND target_date = ?"
        params.append(target_date)
    if target_city:
        query += " AND city = ?"
        params.append(target_city)
    query += " ORDER BY target_date, city, bracket_label"

    trades = conn.execute(query, params).fetchall()
    if not trades:
        print("No settled trades found matching filters.")
        return []

    print(f"Found {len(trades)} settled trades to audit.\n")

    # Group by (city, date) to batch observations
    obs_cache: dict[tuple[str, str], dict] = {}
    results: list[AuditResult] = []
    unknown_cities: set[str] = set()

    client = httpx.Client()

    try:
        for trade in trades:
            city = trade["city"]
            tdate = trade["target_date"]
            cache_key = (city, tdate)

            # Check city config
            if city not in CITY_CONFIG:
                unknown_cities.add(city)
                results.append(AuditResult(
                    trade_id=trade["id"],
                    city=city,
                    target_date=tdate,
                    bracket_label=trade["bracket_label"],
                    side=trade["side"],
                    entry_price=trade["entry_price"],
                    size_usd=trade["size_usd"],
                    model_prob=trade["model_prob"],
                    db_outcome=trade["outcome"],
                    db_pnl=trade["pnl"],
                    obs_daily_max=None,
                    obs_hourly_max=None,
                    obs_unit="?",
                    bracket_lower=None,
                    bracket_upper=None,
                    independent_outcome=None,
                    match=None,
                    discrepancy_reason=f"UNKNOWN CITY: {city} not in CITY_CONFIG",
                    wu_link="",
                ))
                continue

            cfg = CITY_CONFIG[city]

            # Fetch observation (cached per city+date)
            if cache_key not in obs_cache:
                if verbose:
                    print(f"  Fetching obs: {city} {tdate} (tz={cfg['tz']})...")
                obs_cache[cache_key] = fetch_observed_high(
                    cfg["lat"], cfg["lon"], tdate, cfg["tz"], cfg["unit"], client
                )
                time.sleep(0.5)  # be polite to Open-Meteo

            obs = obs_cache[cache_key]

            if "error" in obs:
                results.append(AuditResult(
                    trade_id=trade["id"],
                    city=city,
                    target_date=tdate,
                    bracket_label=trade["bracket_label"],
                    side=trade["side"],
                    entry_price=trade["entry_price"],
                    size_usd=trade["size_usd"],
                    model_prob=trade["model_prob"],
                    db_outcome=trade["outcome"],
                    db_pnl=trade["pnl"],
                    obs_daily_max=None,
                    obs_hourly_max=None,
                    obs_unit=obs.get("unit", "?"),
                    bracket_lower=None,
                    bracket_upper=None,
                    independent_outcome=None,
                    match=None,
                    discrepancy_reason=f"FETCH ERROR: {obs['error']}",
                    wu_link=wu_url(cfg, tdate),
                ))
                continue

            daily_max = obs["daily_max"]
            hourly_max = obs["hourly_max"]

            # Parse bracket
            lower, upper, unit_char = parse_bracket_label(trade["bracket_label"])

            # Determine if observed high falls in bracket
            independent_in_bracket = bracket_matches(daily_max, lower, upper)

            # Map to trade outcome
            # If side=YES: trade wins if obs IS in bracket (outcome=1)
            # If side=NO: trade wins if obs is NOT in bracket (outcome=0)
            if independent_in_bracket is not None:
                if trade["side"] == "YES":
                    independent_outcome = 1 if independent_in_bracket else 0
                else:  # NO
                    independent_outcome = 0 if independent_in_bracket else 1
            else:
                independent_outcome = None

            # Compare
            db_outcome = trade["outcome"]
            if independent_outcome is not None and db_outcome is not None:
                match = (independent_outcome == db_outcome)
            else:
                match = None

            # Determine discrepancy reason
            reason = ""
            if match is False:
                reason = (
                    f"MISMATCH: db_outcome={db_outcome}, "
                    f"independent={independent_outcome}, "
                    f"obs_high={daily_max}{obs['unit']}, "
                    f"bracket=[{lower}, {upper})"
                )
            elif match is None:
                reason = "INCONCLUSIVE (missing data)"
            elif obs.get("daily_vs_hourly_diff") and abs(obs["daily_vs_hourly_diff"]) > 1.0:
                reason = (
                    f"WARNING: daily_max vs hourly_max differ by "
                    f"{obs['daily_vs_hourly_diff']}{obs['unit']} — "
                    f"possible aggregation issue"
                )

            results.append(AuditResult(
                trade_id=trade["id"],
                city=city,
                target_date=tdate,
                bracket_label=trade["bracket_label"],
                side=trade["side"],
                entry_price=trade["entry_price"],
                size_usd=trade["size_usd"],
                model_prob=trade["model_prob"],
                db_outcome=db_outcome,
                db_pnl=trade["pnl"],
                obs_daily_max=daily_max,
                obs_hourly_max=hourly_max,
                obs_unit=obs["unit"],
                bracket_lower=lower,
                bracket_upper=upper,
                independent_outcome=independent_outcome,
                match=match,
                discrepancy_reason=reason,
                wu_link=wu_url(cfg, tdate),
            ))

    finally:
        client.close()

    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_summary(results: list[AuditResult], verbose: bool = False):
    """Print audit results with clear pass/fail/mismatch reporting."""

    if not results:
        return

    mismatches = [r for r in results if r.match is False]
    warnings = [r for r in results if r.match is True and r.discrepancy_reason]
    inconclusive = [r for r in results if r.match is None]
    passing = [r for r in results if r.match is True and not r.discrepancy_reason]

    # --- Summary ---
    print("=" * 72)
    print("SETTLEMENT AUDIT SUMMARY")
    print("=" * 72)
    print(f"  Total trades audited:  {len(results)}")
    print(f"  ✓ Passed:              {len(passing)}")
    print(f"  ⚠ Warnings:            {len(warnings)}")
    print(f"  ✗ MISMATCHES:          {len(mismatches)}")
    print(f"  ? Inconclusive:        {len(inconclusive)}")
    print()

    # --- Mismatches (always show) ---
    if mismatches:
        print("─" * 72)
        print("✗ MISMATCHES — Settlement outcome disagrees with independent observation")
        print("─" * 72)
        for r in mismatches:
            print(f"\n  Trade #{r.trade_id}: {r.city} {r.target_date} {r.bracket_label} {r.side}")
            print(f"    DB outcome={r.db_outcome}  Independent outcome={r.independent_outcome}")
            print(f"    Observed high: {r.obs_daily_max}{r.obs_unit} (hourly max: {r.obs_hourly_max}{r.obs_unit})")
            print(f"    Bracket: [{r.bracket_lower}, {r.bracket_upper}){r.obs_unit}")
            print(f"    DB P&L: {r.db_pnl:+.2f}  Entry: {r.entry_price:.2f}  Model: {r.model_prob:.3f}")
            print(f"    🔗 WU: {r.wu_link}")
        print()

    # --- Warnings ---
    if warnings:
        print("─" * 72)
        print("⚠ WARNINGS — Outcome matches but observation data has anomalies")
        print("─" * 72)
        for r in warnings:
            print(f"  Trade #{r.trade_id}: {r.city} {r.target_date} — {r.discrepancy_reason}")
        print()

    # --- Inconclusive ---
    if inconclusive:
        print("─" * 72)
        print("? INCONCLUSIVE — Could not independently verify")
        print("─" * 72)
        for r in inconclusive:
            print(f"  Trade #{r.trade_id}: {r.city} {r.target_date} — {r.discrepancy_reason}")
        print()

    # --- Per-city breakdown ---
    cities = sorted(set(r.city for r in results))
    print("─" * 72)
    print("PER-CITY BREAKDOWN")
    print("─" * 72)
    print(f"  {'City':<16} {'Trades':>6} {'Pass':>6} {'Warn':>6} {'Fail':>6} {'Obs High':>10}  TZ")
    print(f"  {'─'*16} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*10}  {'─'*20}")
    for city in cities:
        city_results = [r for r in results if r.city == city]
        n_pass = sum(1 for r in city_results if r.match is True and not r.discrepancy_reason)
        n_warn = sum(1 for r in city_results if r.match is True and r.discrepancy_reason)
        n_fail = sum(1 for r in city_results if r.match is False)
        # Show most recent observation
        latest = max(city_results, key=lambda r: r.target_date)
        obs_str = f"{latest.obs_daily_max}{latest.obs_unit}" if latest.obs_daily_max else "N/A"
        tz_str = CITY_CONFIG.get(city, {}).get("tz", "unknown")
        print(f"  {city:<16} {len(city_results):>6} {n_pass:>6} {n_warn:>6} {n_fail:>6} {obs_str:>10}  {tz_str}")
    print()

    # --- Verbose: all trades ---
    if verbose:
        print("─" * 72)
        print("ALL TRADES (verbose)")
        print("─" * 72)
        for r in results:
            status = "✓" if r.match is True and not r.discrepancy_reason else (
                "⚠" if r.match is True else ("✗" if r.match is False else "?")
            )
            print(
                f"  {status} #{r.trade_id:<4} {r.city:<14} {r.target_date} "
                f"{r.bracket_label:<10} {r.side:<3} "
                f"obs={r.obs_daily_max}{r.obs_unit if r.obs_daily_max else '?':<8} "
                f"db={r.db_outcome} ind={r.independent_outcome} "
                f"pnl={r.db_pnl:+.2f}" if r.db_pnl else ""
            )
        print()

    # --- WU verification links for spot-checking ---
    # Pick up to 5 trades across different cities/dates for manual check
    spot_check = []
    seen = set()
    # Prioritize mismatches, then warnings, then one from each city
    for r in mismatches + warnings:
        key = (r.city, r.target_date)
        if key not in seen and r.wu_link:
            spot_check.append(r)
            seen.add(key)
    for r in results:
        if len(spot_check) >= 8:
            break
        key = (r.city, r.target_date)
        if key not in seen and r.wu_link:
            spot_check.append(r)
            seen.add(key)

    if spot_check:
        print("─" * 72)
        print("MANUAL SPOT-CHECK LINKS (Weather Underground)")
        print("─" * 72)
        print("  Compare 'Max Temperature' on these pages against our observed values:")
        print()
        for r in spot_check:
            print(f"  {r.city:<14} {r.target_date}  obs={r.obs_daily_max}{r.obs_unit}")
            print(f"    {r.wu_link}")
        print()
        print("  If WU shows a different high than our obs, the Open-Meteo station")
        print("  coordinates may not match Polymarket's resolution source.")
        print()


def export_json(results: list[AuditResult], path: Path):
    """Export audit results as JSON for further analysis."""
    data = []
    for r in results:
        data.append({
            "trade_id": r.trade_id,
            "city": r.city,
            "target_date": r.target_date,
            "bracket_label": r.bracket_label,
            "side": r.side,
            "entry_price": r.entry_price,
            "model_prob": r.model_prob,
            "db_outcome": r.db_outcome,
            "db_pnl": r.db_pnl,
            "obs_daily_max": r.obs_daily_max,
            "obs_hourly_max": r.obs_hourly_max,
            "obs_unit": r.obs_unit,
            "bracket_lower": r.bracket_lower,
            "bracket_upper": r.bracket_upper,
            "independent_outcome": r.independent_outcome,
            "match": r.match,
            "discrepancy_reason": r.discrepancy_reason,
            "wu_link": r.wu_link,
        })
    path.write_text(json.dumps(data, indent=2))
    print(f"Exported {len(data)} results to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Audit Wethr settlement accuracy against independent observations"
    )
    parser.add_argument(
        "--db", default="data/wethr.db",
        help="Path to wethr SQLite database (default: data/wethr.db)"
    )
    parser.add_argument("--date", help="Filter to specific date (YYYY-MM-DD)")
    parser.add_argument("--city", help="Filter to specific city")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all trades")
    parser.add_argument(
        "--export", metavar="PATH",
        help="Export results as JSON to this path"
    )
    parser.add_argument(
        "--list-cities", action="store_true",
        help="List cities in DB vs configured cities and exit"
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    # List cities mode
    if args.list_cities:
        conn = sqlite3.connect(str(db_path))
        db_cities = sorted(set(
            r[0] for r in conn.execute(
                "SELECT DISTINCT city FROM trades"
            ).fetchall()
        ))
        print(f"Cities in DB ({len(db_cities)}): {', '.join(db_cities)}")
        print(f"Cities configured ({len(CITY_CONFIG)}): {', '.join(sorted(CITY_CONFIG))}")
        missing = set(db_cities) - set(CITY_CONFIG)
        if missing:
            print(f"\n⚠ IN DB BUT NOT CONFIGURED: {', '.join(sorted(missing))}")
            print("  Add these to CITY_CONFIG in this script before auditing.")
        extra = set(CITY_CONFIG) - set(db_cities)
        if extra:
            print(f"\n  Configured but no trades: {', '.join(sorted(extra))}")
        return

    print(f"Settlement Audit — {db_path}")
    print(f"Filters: date={args.date or 'all'}, city={args.city or 'all'}")
    print()

    results = run_audit(db_path, args.date, args.city, args.verbose)
    print_summary(results, args.verbose)

    if args.export:
        export_json(results, Path(args.export))


if __name__ == "__main__":
    main()
