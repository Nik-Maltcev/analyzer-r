import sqlite3

import numpy as np
import pandas as pd
import pytest

from app.core.short_term_lab import (
    BACKTEST_WINDOWS_DAYS,
    CALCULATION_VERSION,
    ROUND_TRIP_COST_PCT,
    STRATEGIES,
    _advance_forward,
    _aggregate,
    _dual_momentum,
    _directional_return,
    _select_non_overlapping_candidates,
    _simulate,
    backtest,
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


@pytest.fixture(autouse=True)
def _register_legacy_strategies(monkeypatch):
    """Retired strategies stay importable for their point-in-time tests."""
    for key in ("dual_momentum", "volatility_breakout"):
        monkeypatch.setitem(STRATEGIES, key, {
            "name": key, "short_name": key, "timeframe": 60,
            "hold": 24 * 60, "stop": 0.0, "target": 0.0,
        })


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


def test_dual_momentum_uses_closed_hourly_history_and_market_tails():
    rows = []
    hour = 60 * 60_000
    returns = np.linspace(-0.15, 0.15, 30)
    for ticker_index, total_return in enumerate(returns):
        ticker = f"C{ticker_index:02d}/USD"
        for index in range(30):
            price = 100.0 if index < 29 else 100.0 * (1 + total_return)
            rows.append((ticker, index * hour, price, price, price, price, 1, price))

    events = _dual_momentum(_bars(rows))
    final = [event for event in events if event["signal_time"] == 30 * hour]

    assert CALCULATION_VERSION == "short-term-lab-v34"
    assert len(final) == 6
    assert {event["direction"] for event in final} == {"long", "short"}
    assert all(event["signal_time"] % (6 * hour) == 0 for event in events)
    assert {event["ticker"] for event in final if event["direction"] == "short"} == {
        "C00/USD", "C01/USD", "C02/USD",
    }
    assert {event["ticker"] for event in final if event["direction"] == "long"} == {
        "C27/USD", "C28/USD", "C29/USD",
    }
    strongest = next(event for event in final if event["ticker"] == "C29/USD")
    assert strongest["signal_price"] == pytest.approx(115.0)
    assert strongest["momentum_24h_pct"] == pytest.approx(15.0)
    assert strongest["market_size"] == 30
    assert strongest["tail_size"] == 3


def test_dual_momentum_rejects_a_gap_in_lookback_history():
    hour = 60 * 60_000
    rows = []
    for ticker_index in range(30):
        ticker = f"C{ticker_index:02d}/USD"
        for index in range(30):
            if ticker == "C29/USD" and index == 20:
                continue
            total_return = -0.15 + ticker_index * 0.30 / 29
            price = 100.0 if index < 29 else 100.0 * (1 + total_return)
            rows.append((ticker, index * hour, price, price, price, price, 1, price))

    events = _dual_momentum(_bars(rows))

    assert not any(
        event["signal_time"] == 30 * hour and event["ticker"] == "C29/USD"
        for event in events
    )


def test_dual_momentum_decision_is_not_changed_by_a_future_candle():
    hour = 60 * 60_000
    rows = []
    future_rows = []
    for ticker_index, total_return in enumerate(np.linspace(-0.15, 0.15, 30)):
        ticker = f"C{ticker_index:02d}/USD"
        for index in range(30):
            price = 100.0 if index < 29 else 100.0 * (1 + total_return)
            rows.append((ticker, index * hour, price, price, price, price, 1, price))
        future_price = 1.0 if ticker_index >= 27 else 10_000.0
        future_rows.append(
            (ticker, 30 * hour, future_price, future_price, future_price,
             future_price, 1, future_price)
        )

    before = [
        event for event in _dual_momentum(_bars(rows))
        if event["signal_time"] == 30 * hour
    ]
    after = [
        event for event in _dual_momentum(_bars(rows + future_rows))
        if event["signal_time"] == 30 * hour
    ]

    assert after == before


def test_strategies_share_the_same_hourly_backtest_parameters():
    assert len(STRATEGIES) >= 1
    for key, settings in STRATEGIES.items():
        assert settings["timeframe"] == 60, f"{key}: timeframe"
        assert settings["hold"] == 24 * 60, f"{key}: hold"
        assert settings["stop"] == 0.0, f"{key}: stop"
        assert settings["target"] == 0.0, f"{key}: target"


def test_dual_momentum_backtest_has_only_90_day_window():
    candles = _bars([
        ("BTC/USD", 0, 100, 101, 99, 100, 1, 100),
        ("BTC/USD", 300_000, 100, 101, 99, 100, 1, 100),
    ])

    _, metrics = backtest(candles, {"dual_momentum": []})

    assert BACKTEST_WINDOWS_DAYS == (30, 90, 180, 365)
    assert set(metrics) == {
        "dual_momentum_30d", "dual_momentum_90d",
        "dual_momentum_180d", "dual_momentum_365d",
    }
    assert metrics["dual_momentum_90d"]["window_days"] == 90
    assert all(not metrics[key]["is_complete"] for key in metrics)


def test_intraday_universe_uses_all_100_configured_crypto_tickers():
    assert len(INTRADAY_TICKERS) == 100
    assert INTRADAY_TICKERS == tuple(CRYPTO_TICKERS)


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
        "strategy": "dual_momentum",
        "ticker": "BTC/USD",
        "direction": "long",
        "signal_time": 0,
        "signal_price": 100.0,
        "score": 2.5,
        "confidence": "high",
        "timeframe_minutes": 60,
        "hold_minutes": 10,
        "stop_pct": 0.0,
        "target_pct": 0.0,
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


def test_simulation_rejects_missing_entry_or_interior_five_minute_bar():
    candidate = {
        "strategy": "dual_momentum", "ticker": "BTC/USD", "direction": "long",
        "signal_time": 0, "signal_price": 100.0, "score": 2.5,
        "confidence": "high", "timeframe_minutes": 60, "hold_minutes": 15,
        "stop_pct": 0.0, "target_pct": 0.0,
    }
    missing_entry = _bars([
        ("BTC/USD", 300_000, 100, 100, 100, 100, 1, 100),
        ("BTC/USD", 600_000, 100, 100, 100, 100, 1, 100),
        ("BTC/USD", 900_000, 100, 100, 100, 100, 1, 100),
    ])
    missing_middle = _bars([
        ("BTC/USD", 0, 100, 100, 100, 100, 1, 100),
        ("BTC/USD", 300_000, 100, 100, 100, 100, 1, 100),
        ("BTC/USD", 900_000, 100, 100, 100, 100, 1, 100),
    ])

    assert _simulate(candidate, missing_entry) is None
    assert _simulate(candidate, missing_middle) is None


def test_non_overlapping_selection_uses_full_24_hour_holding_period():
    base = {
        "strategy": "dual_momentum", "ticker": "BTC/USD", "direction": "long",
        "signal_price": 100.0, "score": 2.0, "confidence": "high",
        "timeframe_minutes": 60, "hold_minutes": 24 * 60,
        "stop_pct": 0.0, "target_pct": 0.0,
    }
    events = [
        {**base, "signal_time": 0},
        {**base, "signal_time": 6 * 60 * 60_000},
        {**base, "signal_time": 24 * 60 * 60_000},
    ]

    selected = _select_non_overlapping_candidates(events)

    assert [event["signal_time"] for event in selected] == [0, 24 * 60 * 60_000]


def test_forward_trade_with_disabled_levels_closes_only_at_horizon(tmp_path):
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
        ) VALUES (?, 'dual_momentum', 'BTC/USD',
                  'long', 0, 100, 2.5, 'high', 60, 10, 0, 0,
                  'active', 0, 100, 600000, -1)
        """,
        (CALCULATION_VERSION,),
    )
    conn.commit()

    first = _bars([
        ("BTC/USD", 0, 100, 110, 90, 105, 1, 105),
        ("BTC/USD", 300_000, 105, 111, 95, 108, 1, 108),
    ])
    _advance_forward(conn, first)
    active = conn.execute(
        "SELECT status, exit_time, current_net_return_pct "
        "FROM short_term_forward_trades"
    ).fetchone()

    assert active[0] == "active"
    assert active[1] is None
    assert active[2] == pytest.approx(8 - ROUND_TRIP_COST_PCT)

    complete = pd.concat([
        first,
        _bars([("BTC/USD", 600_000, 103, 104, 102, 103, 1, 103)]),
    ], ignore_index=True)
    _advance_forward(conn, complete)
    closed = conn.execute(
        "SELECT status, exit_reason, exit_time, exit_price, net_return_pct "
        "FROM short_term_forward_trades"
    ).fetchone()
    conn.close()

    assert closed[0] == "closed"
    assert closed[1] == "time"
    assert closed[2] == 600_000
    assert closed[3] == pytest.approx(103)
    assert closed[4] == pytest.approx(3 - ROUND_TRIP_COST_PCT)


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
