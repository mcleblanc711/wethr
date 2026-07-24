#!/usr/bin/env python3
"""Daily prospective capture, all-age outcome retry, and reconciliation."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx

from src import config
from src.calibration_ops import (
    backfill_range, collect_prospective_forecasts, collection_status, reconcile_all,
    unresolved_target_dates,
)
from src.ledger import utc_now
from src.paper_trader import init_db
from src.telegram import send_message


async def run() -> None:
    init_db()
    today = utc_now().date()
    targets = set(unresolved_target_dates())
    targets.add(today - timedelta(days=1))
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        prospective = await collect_prospective_forecasts(client)
    totals = {"prospective": prospective, "backfill": {}, "reconcile": {}}
    for target in sorted(targets):
        totals["backfill"][target.isoformat()] = await backfill_range(target, target)
    totals["reconcile"] = reconcile_all()
    status = collection_status()
    totals["status"] = status
    alerts = []
    if status["forecast_stale"]:
        alerts.append("forecast freshness exceeds 30 minutes")
    if status["member_count_failures"]:
        alerts.append("expected ensemble members are missing")
    if status["unresolved_items_older_than_3d"]:
        alerts.append(
            "unresolved outcomes older than 3 days: "
            + ", ".join(
                f"{item['city']}/{item['target_date']}"
                for item in status["unresolved_items_older_than_3d"]
            )
        )
    if status["reconciliation_discrepancies"]:
        alerts.append(
            f"{status['reconciliation_discrepancies']} resolutions disagree with station truth"
        )
    if status["missing_resolution_metadata"]:
        alerts.append(
            f"{status['missing_resolution_metadata']} resolutions lack a declared "
            "station or rounding rule and can never reconcile"
        )
    if alerts:
        async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
            await send_message(client, "Wethr calibration alert: " + "; ".join(alerts))
    print(json.dumps(totals, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as exc:
        async def alert() -> None:
            async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
                await send_message(client, f"Wethr daily calibration job failed: {exc}")
        asyncio.run(alert())
        raise
