"""
Wethr main orchestrator.

CLI entry point that ties market discovery, ensemble forecasting,
edge detection, and paper trading together.

Usage:
    python -m src.main scan          # Discover markets + find edges
    python -m src.main trade         # Scan + place paper trades
    python -m src.main report        # Show performance report
    python -m src.main loop          # Continuous scan/trade loop
    python -m src.main settle DATE   # Settle trades for a date (manual)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

from . import config
from .markets import WeatherMarket, discover_markets
from .ensemble import fetch_all_ensembles, EnsembleForecast, RateLimited
from .probability import find_edges, estimate_bracket_probabilities, BracketProbability
from .calibration import estimate_calibrated_probabilities
from .sizing import size_position
from .paper_trader import (
    init_db,
    record_paper_trade,
    record_signal,
    get_stats,
    get_pending_trades,
    get_daily_pnl,
    print_report,
)
from .settlement import settle_date, settle_yesterday
from .trading import TradingClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wethr")


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def scan(
    client: httpx.AsyncClient,
    city_slugs: list[str] | None = None,
) -> tuple[list[WeatherMarket], list[BracketProbability]]:
    """
    Full scan pipeline:
    1. Discover active weather markets on Polymarket
    2. Fetch ensemble forecasts for relevant cities
    3. Find brackets with edge above threshold
    
    Returns (markets, actionable_edges)
    """
    log.info("🔍 Starting scan...")

    # Step 1: Discover markets
    markets = await discover_markets(client)
    if not markets:
        log.warning("No active weather markets found")
        return [], []

    # Filter to requested cities
    if city_slugs:
        markets = [m for m in markets if m.city in city_slugs]

    log.info(f"Found {len(markets)} active weather markets")
    for m in markets:
        log.info(
            f"  {m.city} {m.target_date}: {len(m.brackets)} brackets, "
            f"vol=${m.total_volume:,.0f}"
        )

    # Step 2: Fetch ensemble forecasts (only for cities with active markets)
    active_cities = list({m.city for m in markets})
    log.info(f"Fetching ensembles for: {', '.join(active_cities)}")

    forecasts = await fetch_all_ensembles(client, active_cities)

    # Step 3: Find edges
    edges = find_edges(markets, forecasts, min_edge=config.MIN_EDGE_THRESHOLD)

    if edges:
        log.info(f"🎯 Found {len(edges)} brackets with |edge| >= {config.MIN_EDGE_THRESHOLD:.0%}")
    else:
        log.info("No actionable edges found this scan")

    # Record all signals (for Brier score tracking)
    for market in markets:
        city_fc = forecasts.get(market.city, {})
        fc = city_fc.get(market.target_date)
        if not fc:
            continue
        mp = estimate_bracket_probabilities(market, fc)
        for bp in mp.brackets:
            record_signal(
                market.city, market.target_date, bp, market.total_volume,
                ensemble_mean=fc.mean, ensemble_std=fc.std,
            )

    return markets, edges


async def scan_and_trade(
    client: httpx.AsyncClient,
    city_slugs: list[str] | None = None,
    trading_client: "TradingClient | None" = None,
    latency_detector: "LatencyDetector | None" = None,
) -> list[int]:
    """
    Full pipeline: discover → forecast → detect shifts → calibrate → size → trade.
    
    Uses EMOS calibration when trained params are available for a city,
    falls back to raw ensemble counting otherwise. Detects forecast model
    update shifts for latency exploitation. Executes live trades via
    trading_client if provided and live mode is enabled.
    """
    log.info("🔍 Starting scan + trade pipeline...")

    markets = await discover_markets(client)
    if city_slugs:
        markets = [m for m in markets if m.city in city_slugs]

    if not markets:
        log.warning("No active weather markets found")
        return []

    active_cities = list({m.city for m in markets})
    forecasts = await fetch_all_ensembles(client, active_cities)

    # Detect forecast shifts (latency exploitation)
    if latency_detector:
        for city_fc in forecasts.values():
            for fc in city_fc.values():
                shift = latency_detector.check_shift(fc)
                if shift and shift.is_significant:
                    log.info(
                        f"⚡ Latency opportunity: {shift.city_slug} {shift.target_date} "
                        f"shifted {shift.mean_shift_f:+.1f}°F [{shift.severity}]"
                    )
        latency_detector.clear_stale()

    # Load EMOS params for calibrated estimation
    emos_params = _load_emos_params_safe()
    bma_weights = _load_bma_weights_safe()

    stats = get_stats()
    bankroll = stats.bankroll
    daily_pnl = get_daily_pnl()
    pending = len(get_pending_trades())

    trade_ids = []

    for market in markets:
        city_fc = forecasts.get(market.city, {})
        fc = city_fc.get(market.target_date)
        if not fc:
            continue

        # Probability estimation priority:
        # 1. BMA weighted (if BMA weights trained for this city)
        # 2. EMOS calibrated (if EMOS params trained)
        # 3. Raw ensemble counting (baseline)
        city_bma = bma_weights.get(market.city)
        city_emos = emos_params.get(market.city)

        if city_bma and city_emos:
            mp = _estimate_bma_probabilities(market, fc, city_bma, city_emos)
        elif city_emos:
            mp = estimate_calibrated_probabilities(market, fc, city_emos)
        else:
            mp = estimate_bracket_probabilities(market, fc)

        for bp in mp.brackets:
            # Record signal for Brier tracking
            record_signal(
                market.city, market.target_date, bp, market.total_volume,
                ensemble_mean=fc.mean, ensemble_std=fc.std,
            )

            # Check edge threshold
            if abs(bp.edge) < config.MIN_EDGE_THRESHOLD:
                continue

            # Size position
            ps = size_position(bp, bankroll, daily_pnl, pending)
            if ps.is_valid:
                # Paper trade (always — for tracking)
                tid = record_paper_trade(
                    market.city, market.target_date, ps, market.total_volume,
                )
                if tid is not None:
                    trade_ids.append(tid)
                    pending += 1

                    # Live trade (if client provided and live mode on)
                    if trading_client and trading_client.is_live:
                        from .trading import execute_trade
                        result = execute_trade(
                            trading_client,
                            market.city,
                            market.target_date.isoformat(),
                            ps,
                        )
                        if not result.success:
                            log.warning(f"Live trade failed: {result.error}")

    if trade_ids:
        log.info(f"📝 Placed {len(trade_ids)} paper trades")
    else:
        log.info("No trades placed this scan")

    return trade_ids


def _load_emos_params_safe() -> dict:
    """Load EMOS params, returning empty dict on any failure."""
    try:
        from .calibration import load_all_emos_params
        return load_all_emos_params()
    except Exception:
        return {}


def _load_bma_weights_safe() -> dict:
    """Load BMA weights, returning empty dict on any failure."""
    try:
        from .bma import load_all_bma_weights
        return load_all_bma_weights()
    except Exception:
        return {}


def _estimate_bma_probabilities(market, fc, bma_weights, emos_params):
    """
    Estimate bracket probabilities using BMA-weighted model contributions.
    
    Each model's members get their own Gaussian (optionally EMOS-calibrated),
    then weighted by BMA skill weights.
    """
    from .bma import weighted_bracket_probability
    from .probability import MarketProbabilities, BracketProbability, count_members_in_bracket

    city_cfg = market.city_config
    unit = city_cfg.temp_unit
    daily_maxes = fc.daily_maxes(unit)
    n_total = len(daily_maxes)

    if n_total == 0:
        return MarketProbabilities(market=market, forecast=fc)

    result = MarketProbabilities(market=market, forecast=fc)

    for bracket in market.brackets:
        model_prob = weighted_bracket_probability(
            fc, bracket.lower, bracket.upper, unit,
            bma_weights, emos_params=None,  # EMOS applied per-model inside BMA
        )

        raw_count = count_members_in_bracket(daily_maxes, bracket.lower, bracket.upper)
        edge = model_prob - bracket.market_prob
        confidence = max(model_prob, 1.0 - model_prob)

        bp = BracketProbability(
            bracket=bracket,
            model_prob=model_prob,
            market_prob=bracket.market_prob,
            edge=edge,
            member_count=raw_count,
            total_members=n_total,
            confidence=confidence,
        )
        result.brackets.append(bp)

    return result


async def run_loop(
    city_slugs: list[str] | None = None,
    interval: int = config.SCAN_INTERVAL_SECONDS,
) -> None:
    """
    Continuous scan/trade loop with daily auto-settlement and
    forecast latency detection.
    
    Settlement runs once per day at the first scan after midnight UTC.
    Latency detector persists across scan cycles to track distribution shifts.
    Scans run every `interval` seconds.
    """
    from .latency import LatencyDetector

    log.info(f"Starting continuous loop (interval={interval}s)")
    last_settle_date: date | None = None
    detector = LatencyDetector()
    trading_client = TradingClient()
    trading_client.initialize()

    async with httpx.AsyncClient(
        headers={"User-Agent": config.USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    ) as client:
        while True:
            today = datetime.now(timezone.utc).date()

            # Auto-settle: try last 3 days of unsettled trades (once per day).
            # Gamma API resolves markets same-day or next-day, so we check
            # yesterday through 3 days ago. If Gamma hasn't resolved yet,
            # the fallback NWS/Open-Meteo path handles it.
            if last_settle_date != today:
                for days_back in range(1, 4):
                    try:
                        settle_target = today - timedelta(days=days_back)
                        result = await settle_date(
                            client, settle_target, trading_client=trading_client,
                        )
                        if result["trades_settled"] or result["signals_settled"]:
                            log.info(
                                f"🔔 Auto-settled {settle_target}: "
                                f"{result['trades_settled']} trades, "
                                f"{result['signals_settled']} signals, "
                                f"P&L: ${result['total_pnl']:+.2f}"
                            )
                    except Exception as e:
                        log.error(f"Settlement error for {settle_target}: {e}", exc_info=True)
                last_settle_date = today

            # Scan + trade with latency detection
            try:
                trade_ids = await scan_and_trade(
                    client, city_slugs,
                    latency_detector=detector,
                )
                sleep_time = interval
            except RateLimited:
                sleep_time = 900  # 15 minutes
                log.warning(
                    f"⚠️ Rate limited by Open-Meteo — "
                    f"backing off {sleep_time}s (15 min)"
                )
            except Exception as e:
                log.error(f"Scan error: {e}", exc_info=True)
                sleep_time = interval

            log.info(f"💤 Sleeping {sleep_time}s until next scan...")
            await asyncio.sleep(sleep_time)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Wethr — weather prediction market trading agent"
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # scan
    p_scan = sub.add_parser("scan", help="Discover markets and find edges")
    p_scan.add_argument("--cities", nargs="*", help="City slugs to scan")

    # trade
    p_trade = sub.add_parser("trade", help="Scan + place paper trades")
    p_trade.add_argument("--cities", nargs="*", help="City slugs")

    # report
    sub.add_parser("report", help="Show performance report")

    # loop
    p_loop = sub.add_parser("loop", help="Continuous scan/trade loop")
    p_loop.add_argument("--cities", nargs="*", help="City slugs")
    p_loop.add_argument(
        "--interval", type=int, default=config.SCAN_INTERVAL_SECONDS,
        help="Seconds between scans",
    )

    # pending
    sub.add_parser("pending", help="Show pending trades")

    # settle
    p_settle = sub.add_parser("settle", help="Settle trades for a date")
    p_settle.add_argument(
        "date", nargs="?", default=None,
        help="Date to settle (YYYY-MM-DD), defaults to yesterday",
    )

    # train
    p_train = sub.add_parser("train", help="Collect historical data and train EMOS")
    p_train.add_argument("--city", help="City slug (or --all)")
    p_train.add_argument("--all", action="store_true", help="Train all cities")
    p_train.add_argument("--days", type=int, default=90, help="Lookback days")

    # emos
    sub.add_parser("emos", help="Show EMOS parameters for all cities")

    # export-settled
    p_export = sub.add_parser("export-settled", help="Export settled trades for n8n")
    p_export.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output JSON path, defaults to n8n-wethr/wethr-output/settled_trades.json",
    )
    p_export.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Lookback window for settled_at timestamps",
    )

    # doctor
    sub.add_parser("doctor", help="Show local database/export wiring status")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Init database
    init_db()

    if args.command == "scan":
        async def _run():
            async with httpx.AsyncClient(
                headers={"User-Agent": config.USER_AGENT},
                timeout=config.HTTP_TIMEOUT,
            ) as client:
                markets, edges = await scan(client, args.cities)
                if edges:
                    print(f"\n{'='*60}")
                    print(f"  ACTIONABLE EDGES (|edge| >= {config.MIN_EDGE_THRESHOLD:.0%})")
                    print(f"{'='*60}")
                    for bp in edges:
                        print(
                            f"  {bp.bracket.label}: "
                            f"model={bp.model_prob:.1%} vs market={bp.market_prob:.1%} "
                            f"→ edge={bp.edge:+.1%} "
                            f"({bp.member_count}/{bp.total_members} members)"
                        )
                    print(f"{'='*60}")
        asyncio.run(_run())

    elif args.command == "trade":
        async def _run():
            async with httpx.AsyncClient(
                headers={"User-Agent": config.USER_AGENT},
                timeout=config.HTTP_TIMEOUT,
            ) as client:
                trades = await scan_and_trade(client, args.cities)
                print(f"\nPlaced {len(trades)} paper trades")
        asyncio.run(_run())

    elif args.command == "report":
        print(print_report())

    elif args.command == "loop":
        asyncio.run(run_loop(args.cities, args.interval))

    elif args.command == "pending":
        pending = get_pending_trades()
        if pending:
            print(f"\n{len(pending)} pending trades:")
            for t in pending:
                vol = t.get('market_volume') or 0
                vol_str = f" vol=${vol:,.0f}" if vol else ""
                print(
                    f"  #{t['id']}: {t['city']} {t['target_date']} "
                    f"{t['bracket_label']} {t['side']} @ {t['entry_price']:.2f} "
                    f"${t['size_usd']:.2f}{vol_str}"
                )
        else:
            print("No pending trades")

    elif args.command == "settle":
        async def _run():
            if args.date:
                target = date.fromisoformat(args.date)
            else:
                # Default to yesterday — Gamma API resolves same-day,
                # fallback to NWS/Open-Meteo if not yet resolved
                target = (datetime.now(timezone.utc) - timedelta(days=1)).date()

            async with httpx.AsyncClient(
                headers={"User-Agent": config.USER_AGENT},
                timeout=config.HTTP_TIMEOUT,
            ) as client:
                tc = TradingClient()
                tc.initialize()
                result = await settle_date(client, target, trading_client=tc)
                print(
                    f"\nSettled {result['trades_settled']} trades, "
                    f"{result['signals_settled']} signals for {result['date']}"
                )
                print(f"P&L: ${result['total_pnl']:+.2f}")
                if result.get("redeemed") or result.get("redeem_failed"):
                    print(
                        f"Redeemed: {result['redeemed']} trade(s), "
                        f"failed: {result['redeem_failed']}"
                    )
                for city, info in result.get("cities", {}).items():
                    print(f"  {city}: {info['trades']} trades, ${info['pnl']:+.2f}")
        asyncio.run(_run())

    elif args.command == "train":
        from .history import collect_and_train, train_all_cities
        if args.all:
            results = asyncio.run(train_all_cities(args.days))
            print(f"\nTrained EMOS for {len(results)} cities:")
            for slug, params in results.items():
                print(
                    f"  {slug}: μ = {params.a:.3f} + {params.b:.3f}*mean, "
                    f"σ = {params.c:.3f} + {params.d:.3f}*std, "
                    f"CRPS = {params.crps_train:.4f} (n={params.n_training})"
                )
        elif args.city:
            async def _run():
                async with httpx.AsyncClient(
                    headers={"User-Agent": config.USER_AGENT}, timeout=60,
                ) as client:
                    return await collect_and_train(client, args.city, args.days)
            params = asyncio.run(_run())
            if params:
                print(
                    f"\nEMOS for {args.city}: "
                    f"μ = {params.a:.3f} + {params.b:.3f}*mean, "
                    f"σ = {params.c:.3f} + {params.d:.3f}*std, "
                    f"CRPS = {params.crps_train:.4f} (n={params.n_training})"
                )
            else:
                print("Training failed — insufficient data")
        else:
            print("Specify --city NYC or --all")

    elif args.command == "emos":
        from .calibration import load_all_emos_params
        params_dict = load_all_emos_params()
        if not params_dict:
            print("No EMOS parameters trained yet. Run: python run.py train --all")
        else:
            print(f"\n{'City':<15} {'a':>8} {'b':>8} {'c':>8} {'d':>8} {'CRPS':>8} {'CV':>8} {'n':>6}")
            print("-" * 80)
            for slug, p in sorted(params_dict.items()):
                print(
                    f"{slug:<15} {p.a:>8.3f} {p.b:>8.3f} "
                    f"{p.c:>8.3f} {p.d:>8.3f} "
                    f"{p.crps_train:>8.4f} {p.crps_test:>8.4f} {p.n_training:>6d}"
                )

    elif args.command == "export-settled":
        from pathlib import Path
        from .ops import export_settled_trades

        out_path = Path(args.out).expanduser() if args.out else None
        path, count = export_settled_trades(out_path=out_path, since_hours=args.since_hours)
        print(f"Exported {count} settled trade(s) to {path}")

    elif args.command == "doctor":
        from .ops import doctor_report

        print(doctor_report())


if __name__ == "__main__":
    main()
