"""
Telegram Bot API helper for the Wethr command bot.

Outbound trade/audit push notifications live in ``ntfy.py``; this module
exists only to send replies for the command bot
in ``telegram_bot.py``, which needs interactive Telegram commands that ntfy
cannot serve.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)


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
