import sqlite3

import numpy as np
import pandas as pd

from app.core.reversal_lab import (
    ROUND_TRIP_COST_PCT,
    _forward_credibility,
    backtest_reversal,
    ensure_reversal_schema,
    process_reversal_forward,
)
from app.config import get_settings
from app.content.reversal_notifications import dispatch_reversal_notifications
from app.content.telegram import TelegramPublisher


def _synthetic_candles() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    size = 340
    timestamps = np.arange(size, dtype=np.int64) * 300_000 + 1_700_000_000_000
    btc_returns = rng.normal(0, 0.0004, size)
    alt_returns = btc_returns + rng.normal(0, 0.0007, size)
    volumes = np.full(size, 100.0)

    # A large sell impulse, then a confirming bounce and target follow-through.
    alt_returns[305] = -0.05
    alt_returns[306] = 0.01
    alt_returns[307] = 0.02
    volumes[305] = 1000.0

    btc = 100 * np.cumprod(1 + btc_returns)
    alt = 10 * np.cumprod(1 + alt_returns)
    rows = []
    for ticker, prices in (("BTC/USD", btc), ("ALT/USD", alt)):
        for index, timestamp in enumerate(timestamps):
            rows.append({
                "ticker": ticker,
                "open_time": int(timestamp),
                "close": float(prices[index]),
                "volume": float(volumes[index] if ticker == "ALT/USD" else 100.0),
            })
    return pd.DataFrame(rows)


def test_reversal_requires_confirmation_and_deducts_costs():
    trades, metrics = backtest_reversal(_synthetic_candles())
    alt_trades = [trade for trade in trades if trade["ticker"] == "ALT/USD"]

    assert alt_trades
    trade = alt_trades[0]
    assert trade["direction"] == "long"
    assert trade["entry_time"] > trade["shock_time"]
    assert trade["exit_time"] > trade["entry_time"]
    assert trade["net_return_pct"] == (
        trade["gross_return_pct"] - ROUND_TRIP_COST_PCT
    )
    assert metrics["trades"] >= 1


def _insert_candles(conn: sqlite3.Connection, frame: pd.DataFrame) -> None:
    conn.executemany(
        """
        INSERT INTO reversal_candles (
            ticker, open_time, open, high, low, close, volume, quote_volume
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.ticker, int(row.open_time), float(row.close), float(row.close),
                float(row.close), float(row.close), float(row.volume), 0.0,
            )
            for row in frame.itertuples(index=False)
        ],
    )
    conn.commit()


def test_forward_journal_starts_now_and_never_duplicates_closed_trade():
    candles = _synthetic_candles()
    timestamps = sorted(candles["open_time"].unique())
    conn = sqlite3.connect(":memory:")
    ensure_reversal_schema(conn)

    _insert_candles(conn, candles[candles["open_time"] <= timestamps[305]])
    initialized = process_reversal_forward(conn)
    assert initialized["status"] == "initialized"
    assert conn.execute("SELECT COUNT(*) FROM reversal_forward_trades").fetchone()[0] == 0

    _insert_candles(conn, candles[candles["open_time"] == timestamps[306]])
    opened = process_reversal_forward(conn)
    trade = conn.execute(
        "SELECT * FROM reversal_forward_trades WHERE ticker='ALT/USD'"
    ).fetchone()
    assert opened["opened"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM reversal_forward_notifications WHERE event_type='opened'"
    ).fetchone()[0] == 1
    assert trade["direction"] == "long"
    assert trade["status"] == "active"

    _insert_candles(conn, candles[candles["open_time"] == timestamps[307]])
    closed = process_reversal_forward(conn)
    final = conn.execute(
        """
        SELECT status, exit_reason, gross_return_pct, net_return_pct, cash_result
        FROM reversal_forward_trades WHERE ticker='ALT/USD'
        """
    ).fetchone()
    assert closed["closed"] == 1
    assert final[0] == "closed"
    assert final[1] == "target"
    assert final[3] == final[2] - ROUND_TRIP_COST_PCT
    assert final[4] == final[3]
    assert conn.execute(
        "SELECT COUNT(*) FROM reversal_forward_notifications WHERE event_type='closed'"
    ).fetchone()[0] == 1

    process_reversal_forward(conn)
    assert conn.execute("SELECT COUNT(*) FROM reversal_forward_trades").fetchone()[0] == 1
    assert conn.execute(
        "SELECT net_return_pct FROM reversal_forward_trades WHERE ticker='ALT/USD'"
    ).fetchone()[0] == final[3]
    assert conn.execute(
        "SELECT COUNT(*) FROM reversal_forward_notifications"
    ).fetchone()[0] == 2


def test_forward_credibility_requires_a_real_sample():
    small = _forward_credibility([
        {"cash_result": 1.0, "net_return_pct": 1.0, "direction": "long"},
        {"cash_result": -1.0, "net_return_pct": -1.0, "direction": "short"},
    ])
    assert small["verdict"] == "Данных пока мало"
    assert small["sample_progress"] < 10
    assert 0 <= small["win_rate_low"] <= small["win_rate_high"] <= 100

    confirmed = _forward_credibility([
        {"cash_result": 1.0, "net_return_pct": 1.0, "direction": "long"}
        for _ in range(20)
    ] + [
        {"cash_result": -0.5, "net_return_pct": -0.5, "direction": "short"}
        for _ in range(10)
    ])
    assert confirmed["sample_progress"] == 100
    assert confirmed["profit_factor"] == 4.0
    assert confirmed["verdict"] == "Преимущество подтверждается"


def test_forward_telegram_event_is_delivered_once(tmp_path, monkeypatch):
    db_path = str(tmp_path / "forward.db")
    candles = _synthetic_candles()
    timestamps = sorted(candles["open_time"].unique())
    conn = sqlite3.connect(db_path)
    ensure_reversal_schema(conn)
    _insert_candles(conn, candles[candles["open_time"] <= timestamps[305]])
    process_reversal_forward(conn)
    _insert_candles(conn, candles[candles["open_time"] == timestamps[306]])
    process_reversal_forward(conn)
    conn.close()

    settings = get_settings()
    monkeypatch.setattr(settings, "reversal_telegram_notifications_enabled", True)
    monkeypatch.setattr(settings, "content_telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "content_telegram_chat_id", "test-chat")
    sent_messages = []

    def fake_send(self, text, reply_to_message_id=None):
        sent_messages.append(text)
        return 42

    monkeypatch.setattr(TelegramPublisher, "send_message", fake_send)
    first = dispatch_reversal_notifications(db_path)
    second = dispatch_reversal_notifications(db_path)

    assert first == {"status": "processed", "sent": 1, "failed": 0}
    assert second == {"status": "processed", "sent": 0, "failed": 0}
    assert len(sent_messages) == 1
    assert "Открыта тестовая позиция" in sent_messages[0]
