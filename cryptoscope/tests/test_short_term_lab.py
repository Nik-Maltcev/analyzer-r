import sqlite3

import numpy as np
import pandas as pd
import pytest

from app.core.short_term_lab import (
    CALCULATION_VERSION,
    ROUND_TRIP_COST_PCT,
    _advance_forward,
    _aggregate,
    _directional_return,
    _simulate,
    ensure_short_term_schema,
    generate_candidates,
)


def _bars(rows):
    return pd.DataFrame(
        rows,
        columns=[
            "ticker", "open_time", "open", "high", "low", "close",
            "volume", "quote_volume",
        ],
    )


def test_aggregate_uses_only_fully_closed_bars():
    candles = _bars([
        ("BTC/USD", 0, 100, 101, 99, 100, 1, 100),
        ("BTC/USD", 300_000, 100, 102, 99, 101, 1, 101),
        ("BTC/USD", 600_000, 101, 103, 100, 102, 1, 102),
        ("BTC/USD", 900_000, 102, 104, 101, 103, 1, 103),
        ("BTC/USD", 1_200_000, 103, 105, 102, 104, 1, 104),
    ])

    result = _aggregate(candles, 15)

    assert len(result) == 1
    assert result.iloc[0]["open_time"] == 0
    assert result.iloc[0]["close"] == 102


def test_short_return_uses_entry_not_exit_as_denominator():
    assert _directional_return("short", 100, 90) == pytest.approx(10.0)
    assert _directional_return("long", 100, 110) == pytest.approx(10.0)


def test_second_batch_candidate_generation_smoke():
    rng = np.random.default_rng(42)
    rows = []
    times = np.arange(2_200, dtype=np.int64) * 300_000
    for offset, ticker in enumerate(("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD")):
        close = (100 + offset * 10) * np.exp(np.cumsum(rng.normal(0, 0.003, len(times))))
        open_price = np.r_[close[0], close[:-1]]
        high = np.maximum(open_price, close) * (1 + rng.uniform(0, 0.002, len(times)))
        low = np.minimum(open_price, close) * (1 - rng.uniform(0, 0.002, len(times)))
        volume = rng.uniform(100, 1_000, len(times))
        rows.extend(
            (ticker, int(timestamp), float(open_value), float(high_value),
             float(low_value), float(close_value), float(volume_value),
             float(volume_value * close_value))
            for timestamp, open_value, high_value, low_value, close_value, volume_value
            in zip(times, open_price, high, low, close, volume, strict=True)
        )

    candidates = generate_candidates(_bars(rows))

    assert CALCULATION_VERSION == "short-term-lab-v2"
    assert set(candidates) == {
        "vwap_reversion",
        "liquidity_sweep",
        "volatility_squeeze",
        "btc_lead_lag",
    }
    assert all(
        event["signal_time"] % (15 * 60_000) == 0
        for events in candidates.values()
        for event in events
    )


def test_simulation_enters_at_decision_time_and_uses_stop_first():
    candidate = {
        "strategy": "volatility_breakout",
        "ticker": "BTC/USD",
        "direction": "long",
        "signal_time": 900_000,
        "signal_price": 99.0,
        "score": 2.5,
        "confidence": "high",
        "timeframe_minutes": 15,
        "hold_minutes": 15,
        "stop_pct": 6.0,
        "target_pct": 10.0,
    }
    candles = _bars([
        ("BTC/USD", 600_000, 98, 100, 97, 99, 1, 99),
        ("BTC/USD", 900_000, 100, 111, 93, 105, 1, 105),
        ("BTC/USD", 1_200_000, 105, 106, 104, 105, 1, 105),
        ("BTC/USD", 1_500_000, 105, 106, 104, 105, 1, 105),
        ("BTC/USD", 1_800_000, 105, 106, 104, 105, 1, 105),
    ])

    trade = _simulate(candidate, candles)

    assert trade is not None
    assert trade["entry_time"] == 900_000
    assert trade["entry_price"] == 100
    assert trade["exit_reason"] == "stop"
    assert trade["exit_price"] == pytest.approx(94)
    assert trade["net_return_pct"] == pytest.approx(-6 - ROUND_TRIP_COST_PCT)


def test_simulation_exits_at_horizon_open_without_extra_bar():
    candidate = {
        "strategy": "cross_momentum",
        "ticker": "BTC/USD",
        "direction": "long",
        "signal_time": 0,
        "signal_price": 100.0,
        "score": 2.5,
        "confidence": "high",
        "timeframe_minutes": 60,
        "hold_minutes": 10,
        "stop_pct": 6.0,
        "target_pct": 10.0,
    }
    candles = _bars([
        ("BTC/USD", 0, 100, 101, 99, 100, 1, 100),
        ("BTC/USD", 300_000, 100, 102, 99, 101, 1, 101),
        ("BTC/USD", 600_000, 103, 109, 102, 108, 1, 108),
    ])

    trade = _simulate(candidate, candles)

    assert trade is not None
    assert trade["exit_reason"] == "time"
    assert trade["exit_time"] == 600_000
    assert trade["exit_price"] == 103
    assert trade["net_return_pct"] == pytest.approx(3 - ROUND_TRIP_COST_PCT)


def test_closed_forward_trade_is_not_rewritten(tmp_path):
    db_path = tmp_path / "short-term.db"
    conn = sqlite3.connect(db_path)
    ensure_short_term_schema(conn)
    conn.execute(
        """
        INSERT INTO short_term_forward_trades (
            calculation_version, strategy, ticker, direction, signal_time,
            signal_price, score, confidence, timeframe_minutes, hold_minutes,
            stop_pct, target_pct, status, entry_time, entry_price,
            planned_exit_time, last_evaluated_time
        ) VALUES (?, 'volatility_breakout', 'BTC/USD',
                  'short', 0, 100, 2.5, 'high', 15, 15, 6, 10,
                  'active', 300000, 100, 1200000, 299999)
        """,
        (CALCULATION_VERSION,),
    )
    conn.commit()
    first = _bars([
        ("BTC/USD", 300_000, 100, 107, 89, 95, 1, 95),
    ])

    _advance_forward(conn, first)
    closed = conn.execute(
        "SELECT status, exit_reason, exit_price, net_return_pct FROM short_term_forward_trades"
    ).fetchone()
    assert closed[0] == "closed"
    assert closed[1] == "stop"
    assert closed[2] == pytest.approx(106)
    assert closed[3] == pytest.approx(-6 - ROUND_TRIP_COST_PCT)

    second = _bars([
        ("BTC/USD", 300_000, 100, 101, 80, 80, 1, 80),
        ("BTC/USD", 600_000, 80, 80, 70, 70, 1, 70),
    ])
    _advance_forward(conn, second)
    unchanged = conn.execute(
        "SELECT status, exit_reason, exit_price, net_return_pct FROM short_term_forward_trades"
    ).fetchone()
    conn.close()

    assert unchanged == closed
