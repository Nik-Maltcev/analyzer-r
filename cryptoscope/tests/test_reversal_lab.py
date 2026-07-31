import sqlite3

import numpy as np
import pandas as pd

from app.core.reversal_lab import (
    ROUND_TRIP_COST_PCT,
    backtest_reversal,
    ensure_reversal_schema,
    process_reversal_forward,
)


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

    process_reversal_forward(conn)
    assert conn.execute("SELECT COUNT(*) FROM reversal_forward_trades").fetchone()[0] == 1
    assert conn.execute(
        "SELECT net_return_pct FROM reversal_forward_trades WHERE ticker='ALT/USD'"
    ).fetchone()[0] == final[3]
