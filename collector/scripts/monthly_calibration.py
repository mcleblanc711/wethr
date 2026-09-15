#!/usr/bin/env python3
"""Archive the finalized prior month and create new shadow candidates.

Each step runs independently: an archive that cannot be finalized must not
skip training, and one failed lead bucket must not skip the others. Every
failure is reported in a single calibration alert and the job exits non-zero.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from src import config
from src.calibration_ops import LEAD_BUCKETS, archive_month, evaluate_model, train_candidate
from src.paper_trader import get_db, init_db
from src.ntfy import send_message

FALLBACK_ALGORITHM = "raw-ensemble-fallback"


def prior_month(today: date) -> str:
    prior = today.replace(day=1) - timedelta(days=1)
    return f"{prior.year:04d}-{prior.month:02d}"


def _algorithm(model_version: str, db_path: Path | None) -> str | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT algorithm FROM model_versions WHERE id=?", (model_version,)
        ).fetchone()
    return str(row["algorithm"]) if row else None


def run(today: date | None = None, db_path: Path | None = None) -> dict[str, Any]:
    init_db(db_path)
    month = prior_month(today or date.today())
    summary: dict[str, Any] = {"month": month, "archive": None, "candidates": {}, "errors": []}

    try:
        summary["archive"] = {"path": str(archive_month(month, db_path=db_path))}
    except Exception as exc:
        summary["archive"] = {"error": str(exc)}
        summary["errors"].append(f"archive {month}: {exc}")

    for bucket in LEAD_BUCKETS:
        entry: dict[str, Any] = {}
        summary["candidates"][bucket] = entry
        try:
            model_version = train_candidate(bucket, db_path=db_path)
        except Exception as exc:
            entry["error"] = str(exc)
            summary["errors"].append(f"train {bucket}: {exc}")
            continue
        entry["id"] = model_version
        entry["algorithm"] = _algorithm(model_version, db_path)
        entry["fallback"] = entry["algorithm"] == FALLBACK_ALGORITHM
        # Evaluating immediately moves the candidate to shadow, so its shadow
        # clock starts at training time rather than at a later manual evaluate.
        try:
            evaluate_model(model_version, db_path)
            entry["shadow"] = True
        except Exception as exc:
            entry["shadow"] = False
            entry["evaluate_error"] = str(exc)
            summary["errors"].append(f"evaluate {bucket} ({model_version}): {exc}")
    return summary


def failure_message(summary: dict[str, Any]) -> str:
    lines = [f"Wethr monthly archive/model job for {summary['month']} had failures:"]
    lines.extend(f"- {error}" for error in summary["errors"])
    trained = [
        f"{bucket}: {entry['id']}" + (" (raw fallback)" if entry.get("fallback") else "")
        for bucket, entry in summary["candidates"].items()
        if entry.get("id")
    ]
    if trained:
        lines.append("Trained: " + "; ".join(trained))
    return "\n".join(lines)


async def _alert(message: str) -> None:
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        await send_message(
            client,
            message,
            title="Wethr Monthly Job Failed",
            tags="rotating_light",
            category="calibration",
        )


def main() -> int:
    try:
        summary = run()
    except Exception as exc:
        asyncio.run(_alert(f"Wethr monthly archive/model job failed: {exc}"))
        raise
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["errors"]:
        asyncio.run(_alert(failure_message(summary)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
