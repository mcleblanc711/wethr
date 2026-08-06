"""Explicit exclusions from prospective calibration evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import config


@dataclass(frozen=True)
class CaptureExclusion:
    id: str
    kind: str
    started_at: datetime
    ended_at: datetime
    reason: str

    def as_manifest_dict(self) -> dict[str, str]:
        item = asdict(self)
        item["started_at"] = _iso_utc(self.started_at)
        item["ended_at"] = _iso_utc(self.ended_at)
        return item


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("exclusion timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_capture_exclusions(path: Path | None = None) -> list[CaptureExclusion]:
    source = path or config.CALIBRATION_EXCLUSIONS_PATH
    try:
        raw: Any = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"calibration exclusion file is missing: {source}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(f"unsupported calibration exclusion file: {source}")
    items = raw.get("exclusions")
    if not isinstance(items, list):
        raise ValueError("calibration exclusions must be a list")
    result: list[CaptureExclusion] = []
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("calibration exclusion entries must be objects")
        exclusion = CaptureExclusion(
            id=str(item.get("id") or ""),
            kind=str(item.get("kind") or ""),
            started_at=_parse_utc(str(item.get("started_at") or "")),
            ended_at=_parse_utc(str(item.get("ended_at") or "")),
            reason=str(item.get("reason") or ""),
        )
        if not exclusion.id or exclusion.id in ids:
            raise ValueError(
                f"duplicate or empty calibration exclusion id: {exclusion.id!r}"
            )
        if exclusion.kind != "prospective_capture_gap":
            raise ValueError(
                f"unsupported calibration exclusion kind: {exclusion.kind!r}"
            )
        if exclusion.ended_at <= exclusion.started_at or not exclusion.reason:
            raise ValueError(f"invalid calibration exclusion: {exclusion.id}")
        ids.add(exclusion.id)
        result.append(exclusion)
    return sorted(result, key=lambda item: (item.started_at, item.id))


def overlapping_exclusions(
    start: datetime,
    end: datetime,
    exclusions: Sequence[CaptureExclusion] | None = None,
) -> list[CaptureExclusion]:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("eligibility windows must include a timezone")
    if end < start:
        raise ValueError("eligibility window end precedes start")
    return [
        item
        for item in (
            exclusions if exclusions is not None else load_capture_exclusions()
        )
        if item.started_at < end and item.ended_at > start
    ]


def exclusions_manifest(
    exclusions: Sequence[CaptureExclusion] | None = None,
) -> dict[str, Any]:
    items = [
        item.as_manifest_dict()
        for item in (
            exclusions if exclusions is not None else load_capture_exclusions()
        )
    ]
    encoded = json.dumps(
        items, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "policy": "prospective-capture-gap-v1",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "exclusions": items,
    }
