import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from app.core.short_term_lab import (
    BACKTEST_WINDOWS_DAYS,
    CALCULATION_VERSION,
    REPORT_WINDOW_DAYS,
    ROUND_TRIP_COST_PCT,
    STRATEGIES,
    _advance_forward,
    _aggregate,
    _barrier_exit,
    _build_annual_stability,
    _dual_momentum,
    _directional_return,
    _effective_round_trip_cost,
    _finalize_scan_sample,
    _latest_full_backtest_run,
    _insert_latest_candidates,
    _momentum_signals,
    _record_scan_result,
    _scan_split_time,
    _candidate_identity,
    _select_candidates_by_window,
    _simulate,
    backtest,
    build_strategy_cards_for_report,
    build_scan_cards,
    ensure_short_term_schema,
    generate_candidates,
    get_short_term_report,
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


def test_momentum_is_added_without_removing_active_rs_strategies():
    legacy_test_strategies = {"dual_momentum", "volatility_breakout"}

    assert set(STRATEGIES) - legacy_test_strategies == {
        "rs_low_cost",
        "rs_regime_filter",
        "momentum",
    }


def test_active_baselines_use_timed_exit_without_hidden_barriers():
    for key in ("rs_low_cost", "rs_regime_filter", "momentum"):
        assert STRATEGIES[key]["hold"] == 24 * 60
        assert STRATEGIES[key]["stop"] == 0.0
        assert STRATEGIES[key]["target"] == 0.0
        assert STRATEGIES[key]["cost_pct"] == ROUND_TRIP_COST_PCT


def test_momentum_uses_closed_hourly_candle_and_next_five_minute_entry():
    hour = 60 * 60_000
    rows = []
    for index in range(337):
        btc_price = 100.0
        alt_price = 100.0 * (1.0 + 0.0005 * index)
        rows.extend([
            ("BTC/USD", index * hour, btc_price, btc_price, btc_price, btc_price, 1, btc_price),
            ("ALT/USD", index * hour, alt_price, alt_price, alt_price, alt_price, 1, alt_price),
        ])

    hourly = _bars(rows)
    raw = _momentum_signals(hourly)
    assert raw
    assert raw[-1]["signal_time"] == 336 * hour

    candidates = generate_candidates(hourly, already_hourly=True)["momentum"]
    candidate = candidates[-1]
    assert candidate["signal_time"] == 337 * hour
    assert candidate["signal_price"] == pytest.approx(
        hourly.loc[
            (hourly["ticker"] == "ALT/USD")
            & (hourly["open_time"] == 336 * hour),
            "close",
        ].iloc[0]
    )


def test_adding_momentum_candidate_keeps_existing_rs_forward_trade(tmp_path):
    conn = sqlite3.connect(tmp_path / "short-term.db")
    ensure_short_term_schema(conn)
    conn.execute(
        """
        INSERT INTO short_term_forward_trades (
            calculation_version, strategy, ticker, direction, signal_time,
            signal_price, score, confidence, timeframe_minutes, hold_minutes,
            stop_pct, target_pct
        ) VALUES (?, 'rs_low_cost', 'ALT/USD', 'long', 0,
                  100, 2.5, 'high', 60, 1440, 0, 0)
        """,
        (CALCULATION_VERSION,),
    )
    momentum = {
        "momentum": [{
            "strategy": "momentum", "ticker": "MOM/USD", "direction": "long",
            "signal_time": 60 * 60_000, "signal_price": 10.0, "score": 4.0,
            "confidence": "high", "timeframe_minutes": 60,
            "hold_minutes": 1440, "stop_pct": 0.0, "target_pct": 0.0,
        }],
    }

    assert _insert_latest_candidates(conn, momentum, 60 * 60_000) == 1
    saved = conn.execute(
        "SELECT strategy, ticker FROM short_term_forward_trades ORDER BY strategy"
    ).fetchall()
    conn.close()

    assert saved == [("momentum", "MOM/USD"), ("rs_low_cost", "ALT/USD")]


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

    assert CALCULATION_VERSION == "short-term-lab-v61"
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
        assert settings["hold"] >= 24 * 60, f"{key}: hold"


def test_backtest_builds_all_reporting_windows():
    candles = _bars([
        ("BTC/USD", 0, 100, 101, 99, 100, 1, 100),
        ("BTC/USD", 300_000, 100, 101, 99, 100, 1, 100),
    ])

    _, metrics = backtest(candles, {"dual_momentum": []})

    assert BACKTEST_WINDOWS_DAYS == (30, 90, 180, 365, 1095)
    assert REPORT_WINDOW_DAYS == (1095, 30, 90, 180, 365)
    assert set(metrics) == {
        "dual_momentum_30d", "dual_momentum_90d",
        "dual_momentum_180d", "dual_momentum_365d",
        "dual_momentum_1095d",
    }
    assert metrics["dual_momentum_90d"]["window_days"] == 90
    assert all(not metrics[key]["is_complete"] for key in metrics)


def test_reporting_window_selection_is_invariant_to_older_history():
    day = 24 * 60 * 60 * 1000
    latest_time = 2_000 * day
    cutoff_365 = latest_time - 365 * day

    def event(signal_time, hold_days=2):
        return {
            "strategy": "rs_low_cost",
            "ticker": "BTC/USD",
            "direction": "long",
            "signal_time": signal_time,
            "hold_minutes": hold_days * 24 * 60,
        }

    recent = [
        event(cutoff_365 + day),
        event(cutoff_365 + 4 * day),
    ]
    # This older trade overlaps the first recent signal. A global greedy
    # selection would therefore rewrite the trailing 365-day result.
    older = event(cutoff_365 - day, hold_days=4)

    recent_only = _select_candidates_by_window(
        {"rs_low_cost": recent}, latest_time=latest_time
    )[("rs_low_cost", 365)]
    with_older_history = _select_candidates_by_window(
        {"rs_low_cost": [older, *recent]}, latest_time=latest_time
    )[("rs_low_cost", 365)]

    assert [_candidate_identity(item) for item in with_older_history] == [
        _candidate_identity(item) for item in recent_only
    ]


def test_annual_stability_uses_three_adjacent_non_overlapping_years():
    day = 24 * 60 * 60 * 1000
    latest_time = 1_800 * day
    trades = [
        {
            "strategy": "rs_low_cost",
            "entry_time": latest_time - age_days * day,
            "cash_result": cash_result,
        }
        for age_days, cash_result in ((900, 5.0), (500, -2.0), (100, 3.0))
    ]

    result = _build_annual_stability(
        trades,
        strategy="rs_low_cost",
        latest_time=latest_time,
        coverage_days=1095,
    )

    assert result["complete_years"] == 3
    assert result["profitable_years"] == 2
    assert result["label"] == "Неоднородна"
    assert [period["trades"] for period in result["periods"]] == [1, 1, 1]
    assert [period["net_cash"] for period in result["periods"]] == [5.0, -2.0, 3.0]


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


def test_round_trip_cost_cannot_drop_below_realistic_floor():
    assert _effective_round_trip_cost(None) == pytest.approx(ROUND_TRIP_COST_PCT)
    assert _effective_round_trip_cost(0.10) == pytest.approx(ROUND_TRIP_COST_PCT)
    assert _effective_round_trip_cost(0.50) == pytest.approx(0.50)


def test_long_stop_gap_is_filled_at_actual_five_minute_open():
    barrier = _barrier_exit(
        direction="long",
        entry_price=100.0,
        bar_open=90.0,
        bar_high=95.0,
        bar_low=89.0,
        stop_pct=5.0,
        target_pct=10.0,
    )

    assert barrier == ("stop", 90.0)


def test_same_five_minute_candle_uses_conservative_stop_first():
    barrier = _barrier_exit(
        direction="long",
        entry_price=100.0,
        bar_open=100.0,
        bar_high=112.0,
        bar_low=94.0,
        stop_pct=5.0,
        target_pct=10.0,
    )

    assert barrier == ("stop", 95.0)


def test_reporting_windows_keep_overlapping_raw_signals_before_execution_filter():
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

    selected = _select_candidates_by_window(
        {"dual_momentum": events},
        latest_time=24 * 60 * 60_000,
    )[("dual_momentum", 30)]

    assert [event["signal_time"] for event in selected] == [
        0,
        6 * 60 * 60_000,
        24 * 60 * 60_000,
    ]


def test_backtest_builds_one_executable_ledger_and_reconciles_every_signal():
    five_minutes = 5 * 60_000
    hour = 60 * 60_000
    latest_time = 48 * hour
    rows = []
    for open_time in range(0, latest_time + five_minutes, five_minutes):
        price = 100.0 + 10.0 * open_time / latest_time
        rows.append(
            ("BTC/USD", open_time, price, price, price, price, 1, price)
        )
    base = {
        "strategy": "dual_momentum", "ticker": "BTC/USD", "direction": "long",
        "signal_price": 100.0, "score": 2.0, "confidence": "high",
        "timeframe_minutes": 60, "hold_minutes": 24 * 60,
        "stop_pct": 0.0, "target_pct": 0.0,
        "cost_pct": ROUND_TRIP_COST_PCT,
    }
    candidates = {
        "dual_momentum": [
            {**base, "signal_time": 0},
            {**base, "signal_time": 6 * hour},
            {**base, "signal_time": 24 * hour},
        ]
    }

    trades, metrics = backtest(
        _bars(rows), candidates, latest_time=latest_time
    )

    assert [trade["signal_time"] for trade in trades] == [0, 24 * hour]
    assert all(trade["exit_reason"] == "time" for trade in trades)
    assert all(trade["exit_time"] - trade["entry_time"] == 24 * hour for trade in trades)
    metric = metrics["dual_momentum_30d"]
    assert metric["eligible_candidates"] == 3
    assert metric["trades"] == 2
    assert metric["suppressed_overlaps"] == 1
    assert metric["missing_executions"] == 0
    assert metric["accounted_candidates"] == 3
    assert metric["reconciliation"]["ok"] is True
    assert metric["net_cash"] == pytest.approx(
        sum(trade["cash_result"] for trade in trades)
    )


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


def test_latest_full_backtest_run_skips_newer_lightweight_run(tmp_path):
    db_path = tmp_path / "short-term.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_short_term_schema(conn)
    full_run_id = conn.execute(
        """
        INSERT INTO short_term_runs(calculation_version, status, metrics_json)
        VALUES (?, 'completed', '{}')
        """,
        (CALCULATION_VERSION,),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO short_term_backtest_trades (
            run_id, strategy, ticker, direction, signal_time, entry_time,
            entry_price, exit_time, exit_price, exit_reason, score, confidence,
            gross_return_pct, cost_pct, net_return_pct, cash_result
        ) VALUES (?, 'momentum', 'BTC/USD', 'long', 0, 300000,
                  100, 600000, 101, 'time', 2, 'high', 1, 0.3, 0.7, 0.7)
        """,
        (full_run_id,),
    )
    lightweight_run_id = conn.execute(
        """
        INSERT INTO short_term_runs(calculation_version, status, metrics_json)
        VALUES (?, 'completed', '{}')
        """,
        (CALCULATION_VERSION,),
    ).lastrowid
    conn.commit()

    selected = _latest_full_backtest_run(conn)
    conn.close()

    assert selected is not None
    assert selected["id"] == full_run_id
    assert selected["id"] != lightweight_run_id


def test_report_uses_full_backtest_metrics_after_lightweight_run(tmp_path):
    db_path = tmp_path / "short-term.db"
    conn = sqlite3.connect(db_path)
    ensure_short_term_schema(conn)
    strategies = {
        f"{strategy}_1095d": {
            "window_days": 1095,
            "coverage_days": 1126,
            "is_complete": True,
            "trades": 10,
        }
        for strategy in STRATEGIES
    }
    full_run_id = conn.execute(
        """
        INSERT INTO short_term_runs(calculation_version, status, metrics_json)
        VALUES (?, 'completed', ?)
        """,
        (CALCULATION_VERSION, json.dumps({
            "coverage_days": 1126,
            "strategies": strategies,
        })),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO short_term_backtest_trades (
            run_id, strategy, ticker, direction, signal_time, entry_time,
            entry_price, exit_time, exit_price, exit_reason, score, confidence,
            gross_return_pct, cost_pct, net_return_pct, cash_result
        ) VALUES (?, 'rs_low_cost', 'BTC/USD', 'long', 0, 300000,
                  100, 600000, 101, 'time', 2, 'high', 1, 0.3, 0.7, 0.7)
        """,
        (full_run_id,),
    )
    conn.execute(
        """
        INSERT INTO short_term_runs(calculation_version, status, metrics_json)
        VALUES (?, 'completed', ?)
        """,
        (CALCULATION_VERSION, json.dumps({"strategies": {}})),
    )
    conn.commit()
    conn.close()

    report = get_short_term_report(str(db_path))

    assert report["metrics_run_id"] == full_run_id
    assert report["three_year_calculated"] is True
    assert report["missing_three_year_keys"] == []
    assert any(
        card["window_days"] == 1095 and card["is_complete"]
        for card in report["strategies"]
    )


def test_strategy_cards_keep_three_year_placeholder_when_metric_is_missing():
    cards = build_strategy_cards_for_report({})
    three_year = next(card for card in cards if card["key"].endswith("_1095d"))

    assert three_year["window_days"] == 1095
    assert three_year["coverage_days"] == 0
    assert three_year["is_complete"] is False


def test_strategy_cards_preserve_canonical_three_year_identity():
    cards = build_strategy_cards_for_report(
        {
            "rs_low_cost_1095d": {
                "key": "legacy-cache-key",
                "strategy_key": "legacy_strategy",
                "window_days": 365,
                "coverage_days": 1095,
                "is_complete": True,
            }
        }
    )
    three_year = next(card for card in cards if card["key"] == "rs_low_cost_1095d")

    assert three_year["strategy_key"] == "rs_low_cost"
    assert three_year["window_days"] == 1095
    assert three_year["is_three_year"] is True
    assert three_year["coverage_days"] == 1095
    assert three_year["is_complete"] is True


def test_legacy_completed_report_requests_three_year_migration(tmp_path):
    db_path = tmp_path / "short-term.db"
    conn = sqlite3.connect(db_path)
    ensure_short_term_schema(conn)
    conn.execute(
        """
        INSERT INTO short_term_runs(calculation_version, status, metrics_json)
        VALUES (?, 'completed', ?)
        """,
        (
            CALCULATION_VERSION,
            json.dumps({
                "strategies": {
                    "rs_low_cost_365d": {
                        "window_days": 365,
                        "coverage_days": 365,
                        "is_complete": True,
                    },
                },
            }),
        ),
    )
    conn.commit()
    conn.close()

    report = get_short_term_report(str(db_path))

    assert report["is_ready"] is True
    assert report["needs_history_refresh"] is True
    assert report["three_year_calculated"] is False
    assert any(key.endswith("_1095d") for key in report["missing_three_year_keys"])


def test_existing_rs_report_is_preserved_while_momentum_requests_refresh(tmp_path):
    db_path = tmp_path / "short-term.db"
    conn = sqlite3.connect(db_path)
    ensure_short_term_schema(conn)
    existing_rs = {
        f"{strategy}_1095d": {
            "window_days": 1095,
            "coverage_days": 1095,
            "is_complete": True,
            "trades": 42,
            "net_cash": 12.34,
        }
        for strategy in ("rs_low_cost", "rs_regime_filter")
    }
    run_id = conn.execute(
        """
        INSERT INTO short_term_runs(calculation_version, status, metrics_json)
        VALUES (?, 'completed', ?)
        """,
        (
            CALCULATION_VERSION,
            json.dumps({"coverage_days": 1095, "strategies": existing_rs}),
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO short_term_backtest_trades (
            run_id, strategy, ticker, direction, signal_time, entry_time,
            entry_price, exit_time, exit_price, exit_reason, score, confidence,
            gross_return_pct, cost_pct, net_return_pct, cash_result
        ) VALUES (?, 'rs_low_cost', 'BTC/USD', 'long', 0, 300000,
                  100, 600000, 101, 'time', 2, 'high', 1, 0.3, 0.7, 0.7)
        """,
        (run_id,),
    )
    conn.commit()
    conn.close()

    report = get_short_term_report(str(db_path))
    cards = {card["key"]: card for card in report["strategies"]}

    assert cards["rs_low_cost_1095d"]["net_cash"] == pytest.approx(12.34)
    assert cards["rs_regime_filter_1095d"]["net_cash"] == pytest.approx(12.34)
    assert "momentum_1095d" in report["missing_three_year_keys"]
    assert report["needs_history_refresh"] is True


def test_scan_split_is_chronological_and_does_not_use_returns():
    rows = [{"entry_time": index * 1_000, "result": (-1) ** index} for index in range(10)]

    assert _scan_split_time(rows) == 7_000


def test_scan_metrics_keep_selection_and_holdout_separate():
    cell = {
        "selection_trades": 0, "selection_wins": 0,
        "selection_net_cash": 0.0, "selection_sum_wins": 0.0,
        "selection_sum_losses": 0.0,
        "trades": 0, "wins": 0, "net_cash": 0.0,
        "sum_wins": 0.0, "sum_losses": 0.0,
    }
    _record_scan_result(cell, "selection", 4.0)
    _record_scan_result(cell, "selection", -2.0)
    _record_scan_result(cell, "test", -3.0)
    _record_scan_result(cell, "test", 1.0)
    _finalize_scan_sample(cell, "selection_")
    _finalize_scan_sample(cell)

    assert cell["selection_profit_factor"] == 2.0
    assert cell["selection_net_cash"] == 2.0
    assert cell["profit_factor"] == pytest.approx(1 / 3, abs=0.001)
    assert cell["net_cash"] == -2.0


def test_scan_ranking_uses_selection_metrics_not_holdout_result():
    preferred_on_selection = {
        "eligible": True, "stop_pct": 1.0, "target_pct": 2.0,
        "selection_profit_factor": 2.0, "selection_net_cash": 10.0,
        "selection_trades": 20, "profit_factor": 0.5, "net_cash": -5.0,
        "trades": 10,
    }
    preferred_only_on_holdout = {
        "eligible": True, "stop_pct": 2.0, "target_pct": 4.0,
        "selection_profit_factor": 1.5, "selection_net_cash": 8.0,
        "selection_trades": 20, "profit_factor": 3.0, "net_cash": 20.0,
        "trades": 10,
    }

    cards = build_scan_cards(
        {"momentum": [preferred_only_on_holdout, preferred_on_selection]}, top_n=2
    )

    assert cards[0]["top"][0]["stop_pct"] == 1.0
    assert cards[0]["top"][0]["selection_rank"] == 1
