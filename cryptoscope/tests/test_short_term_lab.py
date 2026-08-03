import sqlite3

import numpy as np
import pandas as pd
import pytest

from app.core.short_term_lab import (
    CALCULATION_VERSION,
    ROUND_TRIP_COST_PCT,
    STRATEGIES,
    _advance_forward,
    _aggregate,
    _dual_momentum,
    _directional_return,
    _simulate,
    ensure_short_term_schema,
    generate_candidates,
)
from app.data.mexc_intraday import INTRADAY_TICKERS
from app.data.tickers import CRYPTO_TICKERS


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


def test_eighth_batch_candidate_generation_smoke():
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

    assert CALCULATION_VERSION == "short-term-lab-v8"
    assert set(candidates) == {
        "trend_persistence",
        "dual_momentum",
    }
    assert all(
        event["signal_time"] % (60 * 60_000) == 0
        for events in candidates.values()
        for event in events
    )


def test_preserved_strategy_settings_are_unchanged_in_eighth_batch():
    assert STRATEGIES["trend_persistence"] == {
        "name": "Trend Persistence",
        "short_name": "Устойчивый тренд",
        "timeframe": 60,
        "hold": 24 * 60,
        "stop": 5.0,
        "target": 10.0,
        "description": "Торгует только сильный согласованный тренд цены и объёма на часовом горизонте.",
    }
    assert STRATEGIES["dual_momentum"] == {
        "name": "Dual Momentum",
        "short_name": "Двойной импульс",
        "timeframe": 60,
        "hold": 24 * 60,
        "stop": 5.0,
        "target": 10.0,
        "description": "Совмещает собственный импульс монеты с её силой относительно остального рынка.",
    }


def test_intraday_universe_uses_all_100_configured_crypto_tickers():
    assert len(INTRADAY_TICKERS) == 100
    assert INTRADAY_TICKERS == tuple(CRYPTO_TICKERS)


def test_momentum_strategies_require_directional_and_relative_agreement():
    hour = 60 * 60_000
    rows = []
    growth = {
        "BTC/USD": 1.0002,
        "ETH/USD": 1.0030,
        "SOL/USD": 1.0010,
        "XRP/USD": 0.9970,
    }
    for ticker, rate in growth.items():
        prices = 100 * np.power(rate, np.arange(60))
        for index, close in enumerate(prices):
            previous = prices[max(index - 1, 0)]
            rows.append({
                "ticker": ticker,
                "open_time": index * hour,
                "open": float(previous),
                "high": float(max(previous, close) * 1.001),
                "low": float(min(previous, close) * 0.999),
                "close": float(close),
                "volume": 100.0,
                "quote_volume": float(close * 100),
            })
    hourly = pd.DataFrame(rows)

    dual = _dual_momentum(hourly)

    assert any(item["ticker"] == "ETH/USD" and item["direction"] == "long" for item in dual)
    assert any(item["ticker"] == "XRP/USD" and item["direction"] == "short" for item in dual)


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
