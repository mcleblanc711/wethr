#!/usr/bin/env python3
"""Archive the finalized prior month and create new shadow candidates."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import httpx

from src import config
from src.calibration_ops import LEAD_BUCKETS, archive_month, train_candidate
from src.paper_trader import init_db
from src.telegram import send_message


def run() -> None:
    init_db()
    prior = date.today().replace(day=1) - timedelta(days=1)
    month = f"{prior.year:04d}-{prior.month:02d}"
    print(archive_month(month))
    for bucket in LEAD_BUCKETS:
        print(train_candidate(bucket))


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        async def alert() -> None:
            async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
                await send_message(client, f"Wethr monthly archive/model job failed: {exc}")
        asyncio.run(alert())
        raise
