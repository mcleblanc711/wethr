"""Offline tests for the read-only Telegram command bot."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trader import get_db, init_db
from src.telegram_bot import (
    HELP_TEXT,
    MAX_MESSAGE_LENGTH,
    TelegramCommandBot,
    command_response,
    format_pnl,
    format_positions,
    format_status,
    format_today,
)


class FakeResponse:
    def __init__(self, payload: dict, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> dict:
        return self.payload


class FakeClient:
    def __init__(self, updates: list[dict], send_error: Exception | None = None) -> None:
        self.updates = updates
        self.send_error = send_error
        self.get_calls: list[tuple[str, dict, int]] = []
        self.post_calls: list[tuple[str, dict, int]] = []

    async def get(self, url: str, *, params: dict, timeout: int) -> FakeResponse:
        self.get_calls.append((url, params, timeout))
        return FakeResponse({"ok": True, "result": self.updates})

    async def post(self, url: str, *, json: dict, timeout: int) -> FakeResponse:
        self.post_calls.append((url, json, timeout))
        return FakeResponse({"ok": True}, self.send_error)


def insert_trade(
    db_path: Path,
    *,
    trade_id: int,
    settled: int = 0,
    pnl: float | None = None,
    label: str = "72°F - 74°F",
) -> None:
    with get_db(db_path) as conn:
        _insert_trade(conn, trade_id, settled, pnl, label)


def _insert_trade(
    conn: sqlite3.Connection,
    trade_id: int,
    settled: int,
    pnl: float | None,
    label: str,
) -> None:
    conn.execute(
        """
        INSERT INTO trades (
            id, created_at, city, target_date, bracket_label, bracket_unit,
            side, entry_price, size_usd, model_prob, market_prob, edge,
            member_count, total_members, confidence, kelly_full, kelly_frac,
            settled, settled_at, pnl
        ) VALUES (?, '2026-08-06 12:00:00', 'nyc', '2026-08-07', ?, 'F',
            'YES', 0.40, 25.0, 0.55, 0.40, 0.15, 10, 20, 0.55, 0.10, 0.05,
            ?, CASE WHEN ? = 1 THEN '2026-08-08 00:00:00' END, ?)
        """,
        (trade_id, label, settled, settled, pnl),
    )


def make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "wethr.db"
    init_db(db_path)
    return db_path


def test_command_responses_read_from_the_paper_ledger(tmp_path: Path):
    db_path = make_db(tmp_path)
    insert_trade(db_path, trade_id=1, settled=1, pnl=12.5)
    insert_trade(db_path, trade_id=2)

    positions = command_response("/positions", db_path)
    pnl = format_pnl(db_path)
    status = format_status(db_path)

    assert "Open paper positions: 1" in positions
    assert "#2 nyc 2026-08-07" in positions
    assert "Realized P/L: $+12.50" in pnl
    assert "Realized ROI: +50.00%" in pnl
    assert "Settled: 1 (1W / 0L" in pnl
    assert "Mode: paper trading only" in status
    assert "do not prove collector-service health" in status


def test_positions_are_limited_to_a_telegram_message(tmp_path: Path):
    db_path = make_db(tmp_path)
    with get_db(db_path) as conn:
        for trade_id in range(1, 23):
            _insert_trade(conn, trade_id, 0, None, "x" * 5000)

    message = format_positions(db_path)

    assert len(message) <= MAX_MESSAGE_LENGTH
    assert "additional position(s) not shown" in message or message.endswith("… truncated")


def test_unknown_and_group_commands_return_help(tmp_path: Path):
    db_path = make_db(tmp_path)

    assert command_response("/unknown", db_path) == HELP_TEXT
    assert command_response("/help@wethr_bot", db_path) == HELP_TEXT


def test_polling_authorizes_chat_replies_in_thread_and_persists_offset(tmp_path: Path):
    db_path = make_db(tmp_path)
    bot = TelegramCommandBot("token", "-10042", db_path=db_path, poll_timeout=15)
    client = FakeClient(
        [
            {
                "update_id": 41,
                "message": {
                    "chat": {"id": -10042},
                    "text": "/help",
                    "message_thread_id": 7,
                },
            }
        ]
    )

    acknowledged = asyncio.run(bot.poll_once(client))

    assert acknowledged == 1
    assert bot.get_offset() == 42
    assert client.get_calls[0][1]["timeout"] == 15
    assert client.get_calls[0][1]["allowed_updates"] == '["message"]'
    assert client.post_calls[0][1]["chat_id"] == "-10042"
    assert client.post_calls[0][1]["message_thread_id"] == 7
    assert client.post_calls[0][1]["text"] == HELP_TEXT


def test_unauthorized_messages_are_acknowledged_without_a_reply(tmp_path: Path):
    db_path = make_db(tmp_path)
    bot = TelegramCommandBot("token", "123", db_path=db_path)
    client = FakeClient(
        [{"update_id": 10, "message": {"chat": {"id": 456}, "text": "/pnl"}}]
    )

    assert asyncio.run(bot.poll_once(client)) == 1
    assert bot.get_offset() == 11
    assert client.post_calls == []


def test_failed_reply_leaves_update_unacknowledged_for_retry(tmp_path: Path):
    db_path = make_db(tmp_path)
    bot = TelegramCommandBot("token", "123", db_path=db_path)
    client = FakeClient(
        [{"update_id": 10, "message": {"chat": {"id": 123}, "text": "/pnl"}}],
        send_error=RuntimeError("offline"),
    )

    assert asyncio.run(bot.poll_once(client)) == 0
    assert bot.get_offset() is None


def test_failed_reply_does_not_log_bot_token(tmp_path: Path, caplog):
    db_path = make_db(tmp_path)
    token = "12345:SECRETTOKEN"
    request = httpx.Request(
        "POST", f"https://api.telegram.org/bot{token}/sendMessage"
    )
    response = httpx.Response(404, request=request)
    error = httpx.HTTPStatusError(
        "not found", request=request, response=response
    )
    bot = TelegramCommandBot(token, "123", db_path=db_path)
    client = FakeClient(
        [{"update_id": 10, "message": {"chat": {"id": 123}, "text": "/pnl"}}],
        send_error=error,
    )

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(bot.poll_once(client)) == 0

    assert token not in caplog.text
    assert "HTTP 404" in caplog.text


def make_audit_db(tmp_path: Path) -> Path:
    audit_path = tmp_path / "wethr_audit.db"
    conn = sqlite3.connect(audit_path)
    conn.executescript(
        """
        CREATE TABLE audit_runs (
            run_id TEXT, started_at TEXT, finished_at TEXT, status TEXT,
            trigger_type TEXT, workflow_id TEXT, daily_rows_upserted INTEGER,
            trade_rows_upserted INTEGER, n_trades_input INTEGER,
            error_message TEXT, notes TEXT
        );
        CREATE TABLE divergences_trades (
            trade_id TEXT, wu_temp REAL, om_temp REAL, iem_temp REAL,
            om_flipped INTEGER, iem_flipped INTEGER
        );
        INSERT INTO audit_runs VALUES
            ('1', '2026-09-13T15:00:00Z', '2026-09-13T15:01:00Z', 'completed',
             'production', 'wf', 5, 5, 5, NULL, NULL),
            ('2', '2026-09-14T15:00:00Z', NULL, 'running', 'production', 'wf',
             0, 0, 12, NULL, NULL);
        INSERT INTO divergences_trades VALUES ('1', 73, 75.2, 74, 1, 0);
        """
    )
    conn.commit()
    conn.close()
    return audit_path


def test_settled_lists_recent_trades_with_window_total(tmp_path: Path):
    db_path = make_db(tmp_path)
    insert_trade(db_path, trade_id=1, settled=1, pnl=12.5)
    insert_trade(db_path, trade_id=2, settled=1, pnl=-25.0)
    insert_trade(db_path, trade_id=3)

    message = command_response("/settled", db_path)

    assert "Last 2 settled trade(s)" in message
    assert "✅ #1 nyc 2026-08-07" in message
    assert "❌ #2 nyc 2026-08-07" in message
    assert "#3" not in message
    assert "Window P/L: $-12.50" in message
    assert "Last 1 settled" in command_response("/settled 1", db_path)
    assert command_response("/settled lots", db_path).startswith("Usage:")


def test_trade_detail_includes_audit_divergence(tmp_path: Path):
    db_path = make_db(tmp_path)
    audit_path = make_audit_db(tmp_path)
    insert_trade(db_path, trade_id=1, settled=1, pnl=12.5)
    insert_trade(db_path, trade_id=2)

    audited = command_response("/trade 1", db_path, audit_path)
    open_trade = command_response("/trade #2", db_path, tmp_path / "missing.db")

    assert "Trade #1 nyc 2026-08-07" in audited
    assert "Result: ✅ P/L $+12.50" in audited
    assert "Audit: WU 73.0° · OM 75.2° · IEM 74.0°" in audited
    assert "Bracket flipped under: OM" in audited
    assert "Status: open" in open_trade
    assert "Audit" not in open_trade
    assert command_response("/trade 99", db_path, audit_path) == "Trade #99 not found."
    assert command_response("/trade abc", db_path) == "Usage: /trade <id>"


def test_today_counts_opened_and_settled(tmp_path: Path):
    db_path = make_db(tmp_path)
    insert_trade(db_path, trade_id=1, settled=1, pnl=12.5)
    insert_trade(db_path, trade_id=2)

    opened_day = format_today(db_path, today="2026-08-06")
    settled_day = format_today(db_path, today="2026-08-08")

    assert "Opened: 2 ($50.00 staked)" in opened_day
    assert "Settled: 0 (0W / 0L)" in opened_day
    assert "Settled: 1 (1W / 0L)" in settled_day
    assert "Settled P/L: $+12.50" in settled_day


def test_audit_reports_latest_run_or_missing_ledger(tmp_path: Path):
    db_path = make_db(tmp_path)
    audit_path = make_audit_db(tmp_path)

    latest = command_response("/audit", db_path, audit_path)

    assert "Status: running (production)" in latest
    assert "Finished: not finished" in latest
    assert "Trades audited: 12" in latest
    assert command_response("/audit", db_path, tmp_path / "none.db") == (
        "No n8n audit ledger found."
    )


def test_mute_commands_toggle_ntfy_categories(tmp_path: Path):
    db_path = make_db(tmp_path)

    muted = command_response("/mute settlements", db_path)
    all_muted = command_response("/mute all", db_path)
    unmuted = command_response("/unmute positions", db_path)

    assert "settlements: muted" in muted and "positions: on" in muted
    assert "positions: muted" in all_muted and "calibration: muted" in all_muted
    assert "positions: on" in unmuted and "settlements: muted" in unmuted
    assert "settlements: muted" in command_response("/mutes", db_path)
    assert command_response("/mute bogus", db_path).startswith("Usage: /mute")
    assert command_response("/unmute", db_path).startswith("Usage: /unmute")
