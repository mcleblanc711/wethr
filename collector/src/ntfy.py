"""
ntfy.sh push notifications for Wethr trading events.

Notifications are optional. If a topic URL is not configured, calls are
no-ops. Failures are logged but never raised into the trading pipeline.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from . import config
from .sizing import PositionSize

log = logging.getLogger(__name__)


MUTE_CATEGORIES = ("positions", "settlements", "calibration")
MUTE_SETTING_PREFIX = "ntfy_mute_"


def is_configured() -> bool:
    """Return True when an ntfy topic URL is set."""
    return bool(config.NTFY_TOPIC_URL)


def is_muted(category: str, db_path: Path | None = None) -> bool:
    """
    Return True when a push category is muted in the ledger settings table.

    Mutes are read on every send so a change from the Telegram bot applies to
    the running collector immediately. A settings read failure never
    suppresses a push.
    """
    from .paper_trader import get_db

    try:
        with get_db(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (MUTE_SETTING_PREFIX + category,),
            ).fetchone()
    except Exception as exc:
        log.warning("ntfy mute lookup failed: %s", type(exc).__name__)
        return False
    return bool(row) and row["value"] == "1"


def set_muted(category: str, muted: bool, db_path: Path | None = None) -> None:
    """Persist the mute flag for one push category."""
    from .paper_trader import get_db

    if category not in MUTE_CATEGORIES:
        raise ValueError(f"Unknown ntfy category: {category}")
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (MUTE_SETTING_PREFIX + category, "1" if muted else "0"),
        )


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
    category: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """
    Send an ntfy message with an httpx-like async client.

    Returns True when ntfy accepts the message. Returns False for missing
    configuration, a muted ``category``, or API/network failures.
    """
    topic_url = topic_url or config.NTFY_TOPIC_URL
    if not topic_url:
        log.debug("ntfy not configured; skipping notification")
        return False
    if category and is_muted(category, db_path):
        log.debug("ntfy category %s muted; skipping notification", category)
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
        client, message, title="New Wethr Position", tags="moneybag",
        category="positions",
    )


def trade_won(trade: dict[str, Any]) -> bool:
    """A YES trade wins when its bracket hit; a NO trade wins when it missed."""
    return bool(trade["outcome"]) == (trade["side"] == "YES")


def build_trade_settled_message(
    trade: dict[str, Any],
    epoch_pnl: float | None = None,
    wins: int | None = None,
    losses: int | None = None,
    epoch_label: str | None = None,
) -> str:
    """Build the ntfy text for one settled position.

    The running-total line covers the trade's strategy epoch.
    """
    city = trade["city"]
    city_name = config.CITIES.get(city).name if city in config.CITIES else city
    result = "WIN" if trade_won(trade) else "LOSS"

    lines = [
        f"{result} #{trade['id']} {city_name} {trade['target_date']}",
        f"{trade['bracket_label']} {trade['side']} @ {trade['entry_price']:.2f}",
        f"Stake: ${trade['size_usd']:.2f} · Edge at entry: {trade['edge']:+.1%}",
        f"P/L: ${trade['pnl']:+.2f}",
    ]
    if epoch_pnl is not None:
        record = f" ({wins}W / {losses}L)" if wins is not None and losses is not None else ""
        label = epoch_label or trade.get("strategy_version") or "current"
        lines.append(f"Epoch {label}: ${epoch_pnl:+,.2f}{record}")
    return "\n".join(lines)


async def notify_trade_settled(
    client: Any,
    trade: dict[str, Any],
    epoch_pnl: float | None = None,
    wins: int | None = None,
    losses: int | None = None,
    epoch_label: str | None = None,
) -> bool:
    """Notify ntfy that a position settled."""
    won = trade_won(trade)
    message = build_trade_settled_message(trade, epoch_pnl, wins, losses, epoch_label)
    return await send_message(
        client,
        message,
        title=f"Wethr {'WIN' if won else 'LOSS'} #{trade['id']}",
        tags="white_check_mark" if won else "x",
        category="settlements",
    )
