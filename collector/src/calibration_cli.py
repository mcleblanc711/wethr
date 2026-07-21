"""CLI surface for the calibration-grade ledger."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

import httpx

from . import config
from .calibration_ops import (
    LEAD_BUCKETS,
    archive_month,
    backfill_range,
    collection_status,
    evaluate_model,
    migrate_legacy_inventory,
    promote_model,
    reconcile_all,
    rollback_model,
    train_candidate,
)
from .telegram import send_message

COMMANDS = {
    "collect-status", "backfill", "reconcile", "train-candidate", "evaluate",
    "promote", "rollback", "archive", "migrate-legacy",
}


def add_calibration_commands(sub: argparse._SubParsersAction) -> None:
    sub.add_parser("collect-status", help="Report calibration collection health")

    parser = sub.add_parser("backfill", help="Idempotently backfill provenance, outcomes, and observations")
    parser.add_argument("--from", dest="from_date", required=True, help="First target date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", required=True, help="Last target date (YYYY-MM-DD)")

    parser = sub.add_parser("reconcile", help="Compare official station truth with Gamma intervals")
    parser.add_argument("--date", default=None, help="Optional target date (YYYY-MM-DD)")

    parser = sub.add_parser("train-candidate", help="Create an immutable, non-active model candidate")
    parser.add_argument("--lead-bucket", choices=LEAD_BUCKETS, default=None)

    parser = sub.add_parser("evaluate", help="Evaluate a model version chronologically")
    parser.add_argument("model_version")

    parser = sub.add_parser("promote", help="Atomically promote a paper model after all gates pass")
    parser.add_argument("model_version")

    parser = sub.add_parser("rollback", help="Restore a previous active paper model")
    parser.add_argument("model_version", nargs="?", default=None)

    parser = sub.add_parser("archive", help="Write and verify immutable monthly Parquet partitions")
    parser.add_argument("--month", required=True, help="Month in YYYY-MM format")
    parser.add_argument("--out", type=Path, default=None)

    parser = sub.add_parser("migrate-legacy", help="Inventory or explicitly merge the secondary database")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inventory only (default)")
    mode.add_argument("--apply", action="store_true", help="Back up both DBs, then merge unique legacy rows")
    parser.add_argument("--legacy-db", type=Path, default=None)


def dispatch_calibration_command(args: argparse.Namespace) -> bool:
    if args.command not in COMMANDS:
        return False
    if args.command == "collect-status":
        print(json.dumps(collection_status(), indent=2, sort_keys=True))
    elif args.command == "backfill":
        result = asyncio.run(backfill_range(date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)))
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "reconcile":
        target = date.fromisoformat(args.date) if args.date else None
        print(json.dumps(reconcile_all(target), indent=2, sort_keys=True))
    elif args.command == "train-candidate":
        print(train_candidate(args.lead_bucket))
    elif args.command == "evaluate":
        print(json.dumps(evaluate_model(args.model_version), indent=2, sort_keys=True))
    elif args.command == "promote":
        gates = promote_model(args.model_version)
        async def notify() -> None:
            async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
                await send_message(client, f"Wethr paper model promoted: {args.model_version}\nLive trading remains disabled.")
        asyncio.run(notify())
        print(json.dumps({"promoted": args.model_version, "gates": gates, "live_trading": False}, indent=2))
    elif args.command == "rollback":
        restored = rollback_model(args.model_version)
        print(json.dumps({"active_model": restored, "live_trading": False}, indent=2))
    elif args.command == "archive":
        print(archive_month(args.month, out_dir=args.out))
    elif args.command == "migrate-legacy":
        result = migrate_legacy_inventory(args.legacy_db, apply=bool(args.apply))
        print(json.dumps(result, indent=2, sort_keys=True))
    return True
