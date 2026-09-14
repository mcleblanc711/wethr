"""
Telegram command bot for the Wethr paper-trading ledger.

Commands never change trades. The only writes are ntfy mute flags in the
``settings`` table (``/mute``, ``/unmute``), which the collector reads on
every push.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import config
from .ntfy import MUTE_CATEGORIES, is_muted, set_muted
from .ops import default_audit_db_path
from .paper_trader import get_db, get_pending_trades, get_stats
from .telegram import _failure_detail, send_message

log = logging.getLogger(__name__)

OFFSET_SETTING_KEY = "telegram_bot_update_offset"
MAX_MESSAGE_LENGTH = 4096
MAX_POSITIONS = 20
DEFAULT_SETTLED = 10
MAX_SETTLED = 20
HELP_TEXT = (
    "Wethr paper-trading bot\n\n"
    "/positions — open paper positions\n"
    "/pnl — lifetime realized paper P/L\n"
    "/settled [n] — last n settled trades (default 10, max 20)\n"
    "/trade <id> — one trade in detail, with audit divergence\n"
    "/today — positions opened and settled today (UTC)\n"
    "/audit — latest n8n divergence audit run\n"
    "/status — ledger activity summary\n"
    "/mutes — show ntfy mute flags\n"
    "/mute <positions|settlements|calibration|all> — silence ntfy pushes\n"
    "/unmute <positions|settlements|calibration|all> — resume ntfy pushes\n"
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


def _result_mark(pnl: float | None) -> str:
    if pnl is None:
        return "·"
    return "✅" if pnl > 0 else "❌"


def format_settled(limit: int = DEFAULT_SETTLED, db_path: Path | None = None) -> str:
    """Format the most recently settled paper trades."""
    limit = max(1, min(limit, MAX_SETTLED))
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id, city, target_date, bracket_label, side, pnl, settled_at "
            "FROM trades WHERE settled = 1 "
            "ORDER BY settled_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        return "No settled paper trades."

    lines = [f"Last {len(rows)} settled trade(s)"]
    total = 0.0
    for row in rows:
        pnl = row["pnl"] or 0.0
        total += pnl
        lines.append(
            f"{_result_mark(row['pnl'])} #{row['id']} {row['city']} {row['target_date']} "
            f"{row['bracket_label']} {row['side']} ${pnl:+.2f}"
        )
    lines.append(f"Window P/L: ${total:+,.2f}")
    return _limit_message("\n".join(lines))


def _open_audit_db(audit_db_path: Path | None) -> sqlite3.Connection | None:
    """Open the n8n audit ledger read-only, or return None when absent."""
    path = audit_db_path or default_audit_db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _format_temp(value: Any) -> str:
    return "n/a" if value is None else f"{value}°"


def format_trade(
    trade_id: int,
    db_path: Path | None = None,
    audit_db_path: Path | None = None,
) -> str:
    """Format one trade, adding its n8n divergence row when audited."""
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        return f"Trade #{trade_id} not found."

    lines = [
        f"Trade #{row['id']} {row['city']} {row['target_date']}",
        f"{row['bracket_label']} {row['side']} @ {row['entry_price']:.2f}",
        f"Stake: ${row['size_usd']:.2f}",
        (
            f"Edge: {row['edge']:+.1%} "
            f"(model {row['model_prob']:.1%}, market {row['market_prob']:.1%})"
        ),
        f"Opened: {row['created_at']}",
    ]
    if row["settled"]:
        lines.append(f"Settled: {row['settled_at']}")
        lines.append(f"Result: {_result_mark(row['pnl'])} P/L ${(row['pnl'] or 0.0):+.2f}")
    else:
        lines.append("Status: open")

    try:
        audit = _open_audit_db(audit_db_path)
        if audit is not None:
            try:
                div = audit.execute(
                    "SELECT * FROM divergences_trades WHERE trade_id = ?",
                    (str(trade_id),),
                ).fetchone()
            finally:
                audit.close()
            temps = ("wu_temp", "om_temp", "iem_temp")
            if div and any(div[key] is not None for key in temps):
                lines.append(
                    "Audit: WU {wu} · OM {om} · IEM {iem}".format(
                        wu=_format_temp(div["wu_temp"]),
                        om=_format_temp(div["om_temp"]),
                        iem=_format_temp(div["iem_temp"]),
                    )
                )
                flips = [
                    name for name, key in (("OM", "om_flipped"), ("IEM", "iem_flipped"))
                    if div[key]
                ]
                if flips:
                    lines.append(f"Bracket flipped under: {', '.join(flips)}")
    except sqlite3.Error as exc:
        log.warning("Audit lookup failed: %s", type(exc).__name__)
    return _limit_message("\n".join(lines))


def format_today(db_path: Path | None = None, today: str | None = None) -> str:
    """Summarize positions opened and settled on the current UTC day."""
    day = today or datetime.now(timezone.utc).date().isoformat()
    with get_db(db_path) as conn:
        opened = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_usd), 0) AS stake "
            "FROM trades WHERE DATE(created_at) = ?",
            (day,),
        ).fetchone()
        settled = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS pnl, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins "
            "FROM trades WHERE settled = 1 AND DATE(settled_at) = ?",
            (day,),
        ).fetchone()
    wins = settled["wins"] or 0
    return (
        f"Today ({day} UTC)\n"
        f"Opened: {opened['n']} (${opened['stake']:,.2f} staked)\n"
        f"Settled: {settled['n']} ({wins}W / {settled['n'] - wins}L)\n"
        f"Settled P/L: ${settled['pnl']:+,.2f}"
    )


def format_audit(audit_db_path: Path | None = None) -> str:
    """Report the latest n8n divergence audit run."""
    try:
        audit = _open_audit_db(audit_db_path)
        if audit is None:
            return "No n8n audit ledger found."
        try:
            run = audit.execute(
                "SELECT * FROM audit_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            audit.close()
    except sqlite3.Error as exc:
        log.warning("Audit lookup failed: %s", type(exc).__name__)
        return "The n8n audit ledger could not be read."
    if not run:
        return "No n8n audit runs recorded."
    lines = [
        "Latest divergence audit",
        f"Status: {run['status']} ({run['trigger_type']})",
        f"Started: {run['started_at']}",
        f"Finished: {run['finished_at'] or 'not finished'}",
        f"Trades audited: {run['n_trades_input'] or 0}",
    ]
    if run["error_message"]:
        lines.append(f"Error: {run['error_message']}")
    return _limit_message("\n".join(lines))


MUTE_USAGE = "Usage: /mute <" + "|".join(MUTE_CATEGORIES) + "|all>"
UNMUTE_USAGE = "Usage: /unmute <" + "|".join(MUTE_CATEGORIES) + "|all>"


def format_mutes(db_path: Path | None = None) -> str:
    lines = ["ntfy pushes"]
    for category in MUTE_CATEGORIES:
        state = "muted" if is_muted(category, db_path) else "on"
        lines.append(f"{category}: {state}")
    lines.append("n8n audit/error pushes are sent by n8n and are not muted here.")
    return "\n".join(lines)


def change_mute(argument: str, muted: bool, db_path: Path | None = None) -> str:
    """Set or clear mute flags for one category or all of them."""
    target = argument.strip().lower()
    if target == "all":
        categories = list(MUTE_CATEGORIES)
    elif target in MUTE_CATEGORIES:
        categories = [target]
    else:
        return MUTE_USAGE if muted else UNMUTE_USAGE
    for category in categories:
        set_muted(category, muted, db_path)
    return format_mutes(db_path)


def command_response(
    text: str,
    db_path: Path | None = None,
    audit_db_path: Path | None = None,
) -> str:
    """Return the response for a supported slash command."""
    parts = text.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    # Telegram clients can send /command@bot_name in group conversations.
    command = command.split("@", maxsplit=1)[0]
    if command == "/positions":
        return format_positions(db_path)
    if command == "/pnl":
        return format_pnl(db_path)
    if command == "/status":
        return format_status(db_path)
    if command == "/settled":
        if not argument:
            return format_settled(DEFAULT_SETTLED, db_path)
        if not argument.isdigit():
            return f"Usage: /settled [n] (1-{MAX_SETTLED})"
        return format_settled(int(argument), db_path)
    if command == "/trade":
        trade_id = argument.lstrip("#")
        if not trade_id.isdigit():
            return "Usage: /trade <id>"
        return format_trade(int(trade_id), db_path, audit_db_path)
    if command == "/today":
        return format_today(db_path)
    if command == "/audit":
        return format_audit(audit_db_path)
    if command == "/mutes":
        return format_mutes(db_path)
    if command == "/mute":
        return change_mute(argument, True, db_path)
    if command == "/unmute":
        return change_mute(argument, False, db_path)
    return HELP_TEXT


class TelegramCommandBot:
    """Long-poll Telegram updates and answer authorized commands."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        db_path: Path | None = None,
        poll_timeout: int = 30,
        audit_db_path: Path | None = None,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.db_path = db_path
        self.audit_db_path = audit_db_path
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
        return await self._send_reply(
            client, message, command_response(text, self.db_path, self.audit_db_path)
        )

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
