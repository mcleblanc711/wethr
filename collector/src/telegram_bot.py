"""Read-only Telegram command bot for the Wethr paper-trading ledger."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from . import config
from .paper_trader import get_db, get_pending_trades, get_stats
from .telegram import _failure_detail, send_message

log = logging.getLogger(__name__)

OFFSET_SETTING_KEY = "telegram_bot_update_offset"
MAX_MESSAGE_LENGTH = 4096
MAX_POSITIONS = 20
HELP_TEXT = (
    "Wethr paper-trading bot\n\n"
    "/positions — open paper positions\n"
    "/pnl — lifetime realized paper P/L\n"
    "/status — ledger activity summary\n"
    "/help — show this message"
)


def _limit_message(text: str) -> str:
    """Keep Bot API messages within Telegram's text limit."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    suffix = "\n… truncated"
    return text[: MAX_MESSAGE_LENGTH - len(suffix)] + suffix


def _format_timestamp(value: str | None) -> str:
    return value or "no recorded activity"


def format_positions(db_path: Path | None = None) -> str:
    """Format open paper positions, bounded for a Telegram response."""
    positions = get_pending_trades(db_path)
    if not positions:
        return "No open paper positions."

    lines = [f"Open paper positions: {len(positions)}"]
    for position in positions[:MAX_POSITIONS]:
        lines.append(
            f"#{position['id']} {position['city']} {position['target_date']}\n"
            f"{position['bracket_label']} {position['side']} @ "
            f"{position['entry_price']:.2f} · ${position['size_usd']:.2f}"
        )
    omitted = len(positions) - MAX_POSITIONS
    if omitted:
        lines.append(f"… {omitted} additional position(s) not shown")
    return _limit_message("\n".join(lines))


def format_pnl(db_path: Path | None = None) -> str:
    """Format lifetime realized paper P/L from the canonical trade ledger."""
    stats = get_stats(db_path)
    roi = (stats.gross_pnl / stats.settled_stake) if stats.settled_stake else 0.0
    return _limit_message(
        "Lifetime paper P/L\n"
        f"Realized P/L: ${stats.gross_pnl:+,.2f}\n"
        f"Realized ROI: {roi:+.2%}\n"
        f"Settled: {stats.settled_trades} ({stats.wins}W / {stats.losses}L, "
        f"{stats.win_rate:.1%})\n"
        f"Open: {stats.pending_trades}\n"
        f"Displayed bankroll: ${stats.bankroll:,.2f}"
    )


def format_status(db_path: Path | None = None) -> str:
    """Report ledger freshness without inferring collector-service health."""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS latest_trade, "
            "MAX(CASE WHEN settled = 1 THEN settled_at END) AS latest_settlement, "
            "SUM(CASE WHEN settled = 0 THEN 1 ELSE 0 END) AS open_positions "
            "FROM trades"
        ).fetchone()
    return _limit_message(
        "Wethr status\n"
        "Mode: paper trading only\n"
        f"Open positions: {row['open_positions'] or 0}\n"
        f"Latest trade: {_format_timestamp(row['latest_trade'])}\n"
        f"Latest settlement: {_format_timestamp(row['latest_settlement'])}\n"
        "Ledger timestamps do not prove collector-service health."
    )


def command_response(text: str, db_path: Path | None = None) -> str:
    """Return the response for a supported slash command."""
    command = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    # Telegram clients can send /command@bot_name in group conversations.
    command = command.split("@", maxsplit=1)[0]
    if command == "/positions":
        return format_positions(db_path)
    if command == "/pnl":
        return format_pnl(db_path)
    if command == "/status":
        return format_status(db_path)
    return HELP_TEXT


class TelegramCommandBot:
    """Long-poll Telegram updates and answer authorized read-only commands."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        db_path: Path | None = None,
        poll_timeout: int = 30,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.db_path = db_path
        self.poll_timeout = poll_timeout

    @property
    def _api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    def get_offset(self) -> int | None:
        with get_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (OFFSET_SETTING_KEY,)
            ).fetchone()
        return int(row["value"]) if row else None

    def save_offset(self, offset: int) -> None:
        with get_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (OFFSET_SETTING_KEY, str(offset)),
            )

    def _is_authorized(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat")
        return isinstance(chat, dict) and str(chat.get("id")) == self.chat_id

    async def _send_reply(
        self, client: Any, message: dict[str, Any], text: str
    ) -> bool:
        thread_id = message.get("message_thread_id")
        return await send_message(
            client,
            _limit_message(text),
            token=self.token,
            chat_id=self.chat_id,
            thread_id=thread_id if isinstance(thread_id, int) else None,
        )

    async def handle_update(self, client: Any, update: dict[str, Any]) -> bool:
        """Handle one update; false means leave it queued for a later retry."""
        message = update.get("message")
        if not isinstance(message, dict) or not self._is_authorized(message):
            return True
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return True
        return await self._send_reply(client, message, command_response(text, self.db_path))

    async def poll_once(self, client: Any) -> int:
        """Fetch and process pending updates, returning the number acknowledged."""
        params: dict[str, Any] = {
            "timeout": self.poll_timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        offset = self.get_offset()
        if offset is not None:
            params["offset"] = offset
        response = await client.get(
            f"{self._api_base}/getUpdates", params=params, timeout=self.poll_timeout + 10
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok", False) or not isinstance(payload.get("result"), list):
            raise RuntimeError(f"Unexpected Telegram getUpdates response: {payload!r}")

        acknowledged = 0
        for update in payload["result"]:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                log.warning("Ignoring malformed Telegram update: %r", update)
                continue
            if not await self.handle_update(client, update):
                break
            self.save_offset(update["update_id"] + 1)
            acknowledged += 1
        return acknowledged

    async def run_forever(self) -> None:
        """Run the single long-poll consumer until the service is stopped."""
        async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
            while True:
                try:
                    await self.poll_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Telegram polling failed: %s", _failure_detail(exc))
                    await asyncio.sleep(5)


async def run_bot() -> None:
    """Start the configured bot, failing clearly when its credentials are absent."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Telegram bot requires WETHR_TELEGRAM_BOT_TOKEN and WETHR_TELEGRAM_CHAT_ID"
        )
    bot = TelegramCommandBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    log.info("Starting authorized Wethr Telegram command bot")
    await bot.run_forever()
