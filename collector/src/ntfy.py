"""
ntfy.sh push notifications for Wethr trading events.

Notifications are optional. If a topic URL is not configured, calls are
no-ops. Failures are logged but never raised into the trading pipeline.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from . import config
from .sizing import PositionSize

log = logging.getLogger(__name__)


def is_configured() -> bool:
    """Return True when an ntfy topic URL is set."""
    return bool(config.NTFY_TOPIC_URL)


def _failure_detail(exc: Exception) -> str:
    """Describe an ntfy failure without logging the topic URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


async def _post_ntfy(
    client: Any,
    *,
    topic_url: str,
    text: str,
    title: str | None,
    tags: str | None,
    priority: str | None,
) -> bool:
    """POST a message to an ntfy topic."""
    headers: dict[str, str] = {"Markdown": "yes"}
    if title:
        headers["Title"] = title
    if tags:
        headers["Tags"] = tags
    if priority:
        headers["Priority"] = priority
    try:
        response = await client.post(
            topic_url, content=text.encode("utf-8"), headers=headers, timeout=10
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("ntfy message failed: %s", _failure_detail(exc))
        return False
    return True


def build_trade_opened_message(
    trade_id: int,
    city: str,
    target_date: date,
    ps: PositionSize,
    market_volume: float = 0.0,
    pending_count: int | None = None,
) -> str:
    """Build the ntfy text for a newly opened position."""
    bp = ps.bracket_prob
    bracket = bp.bracket
    city_name = config.CITIES.get(city).name if city in config.CITIES else city

    lines = [
        "New Wethr position",
        f"#{trade_id} {city_name} {target_date.isoformat()}",
        f"{bracket.label} {ps.side} @ {ps.entry_price:.2f}",
        f"Size: ${ps.capped_size_usd:.2f}",
        (
            f"Edge: {bp.edge:+.1%} "
            f"(model {bp.model_prob:.1%}, market {bp.market_prob:.1%})"
        ),
        f"Win/Loss: ${ps.win_pnl:+.2f} / ${ps.loss_pnl:+.2f}",
    ]

    if market_volume > 0:
        lines.append(f"Market volume: ${market_volume:,.0f}")
    if pending_count is not None:
        lines.append(f"Open positions: {pending_count}")

    return "\n".join(lines)


async def send_message(
    client: Any,
    text: str,
    *,
    topic_url: str | None = None,
    title: str | None = None,
    tags: str | None = None,
    priority: str | None = None,
) -> bool:
    """
    Send an ntfy message with an httpx-like async client.

    Returns True when ntfy accepts the message. Returns False for missing
    configuration or API/network failures.
    """
    topic_url = topic_url or config.NTFY_TOPIC_URL
    if not topic_url:
        log.debug("ntfy not configured; skipping notification")
        return False

    return await _post_ntfy(
        client, topic_url=topic_url, text=text, title=title, tags=tags, priority=priority
    )


async def notify_trade_opened(
    client: Any,
    trade_id: int,
    city: str,
    target_date: date,
    ps: PositionSize,
    market_volume: float = 0.0,
    pending_count: int | None = None,
) -> bool:
    """Notify ntfy that a new position was opened."""
    message = build_trade_opened_message(
        trade_id=trade_id,
        city=city,
        target_date=target_date,
        ps=ps,
        market_volume=market_volume,
        pending_count=pending_count,
    )
    return await send_message(
        client, message, title="New Wethr Position", tags="moneybag"
    )
