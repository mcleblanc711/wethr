#!/usr/bin/env python3
"""Daily prospective capture, all-age outcome retry, and reconciliation."""
from __future__ import annotations

import asyncio
import json

import httpx

from src import config
from src.calibration_ops import (
    backfill_city_dates, collect_prospective_forecasts, collection_status,
    due_resolution_items, reconcile_all,
)
from src.paper_trader import init_db
from src.telegram import send_message


async def run() -> None:
    init_db()
    queue = due_resolution_items()
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        prospective = await collect_prospective_forecasts(client)
    backfill = await backfill_city_dates(queue)
    totals = {
        "prospective": prospective,
        "backfill": backfill,
        "queue": {
            "limit": config.DAILY_BACKFILL_LIMIT,
            "selected": [
                {"city": city, "target_date": target.isoformat()}
                for city, target in queue
            ],
        },
        "reconcile": reconcile_all(),
    }
    status = collection_status()
    totals["status"] = status
    alerts = []
    if backfill["failure_details"]:
        alerts.append(
            f"{len(backfill['failure_details'])} provider grains failed; "
            "details were persisted for retry"
        )
    if not status["prospective_window_uninterrupted"]:
        alerts.append(
            "the seven-day prospective window overlaps an explicit host-downtime gap"
        )
    if status["forecast_stale"]:
        alerts.append("forecast freshness exceeds 30 minutes")
    if status["member_count_failures"]:
        alerts.append("expected ensemble members are missing")
    if status["unresolved_items_older_than_3d"]:
        overdue = status["unresolved_items_older_than_3d"]
        sample = overdue[:20]
        remainder = len(overdue) - len(sample)
        alerts.append(
            f"{len(overdue)} unresolved outcomes older than 3 days; oldest sample: "
            + ", ".join(
                f"{item['city']}/{item['target_date']}"
                for item in sample
            )
            + (f" (+{remainder} more)" if remainder else "")
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
