"""
Telegram notifications for Wethr trading events.

Notifications are optional. If Telegram credentials are not configured, calls
are no-ops. Failures are logged but never raised into the trading pipeline.
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
    """Return True when the Telegram Bot API can be called."""
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _failure_detail(exc: Exception) -> str:
    """Describe a Telegram failure without logging its token-bearing URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


async def _post_telegram(
    client: Any,
    *,
    token: str,
    payload: dict[str, Any],
) -> bool:
    """Post a Bot API message while keeping credentials out of logs."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = await client.post(url, json=payload, timeout=10)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok", False):
            raise RuntimeError("Telegram rejected sendMessage")
    except Exception as exc:
        log.warning("Telegram message failed: %s", _failure_detail(exc))
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
    """Build the Telegram text for a newly opened position."""
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
    token: str | None = None,
    chat_id: str | None = None,
    thread_id: int | None = None,
) -> bool:
    """
    Send a Telegram message with an httpx-like async client.

    Returns True when the Bot API accepts the message. Returns False for
    missing configuration or API/network failures.
    """
    token = token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.debug("Telegram not configured; skipping notification")
        return False

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    elif config.TELEGRAM_MESSAGE_THREAD_ID:
        try:
            payload["message_thread_id"] = int(config.TELEGRAM_MESSAGE_THREAD_ID)
        except ValueError:
            log.warning(
                "Invalid WETHR_TELEGRAM_MESSAGE_THREAD_ID=%r; sending without it",
                config.TELEGRAM_MESSAGE_THREAD_ID,
            )

    return await _post_telegram(client, token=token, payload=payload)


async def notify_trade_opened(
    client: Any,
    trade_id: int,
    city: str,
    target_date: date,
    ps: PositionSize,
    market_volume: float = 0.0,
    pending_count: int | None = None,
) -> bool:
    """Notify Telegram that a new position was opened."""
    message = build_trade_opened_message(
        trade_id=trade_id,
        city=city,
        target_date=target_date,
        ps=ps,
        market_volume=market_volume,
        pending_count=pending_count,
    )
    return await send_message(client, message)
