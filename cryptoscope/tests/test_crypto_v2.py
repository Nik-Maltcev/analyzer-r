import numpy as np
import pandas as pd

from app.core.crypto_v2 import (
    MAX_HOLDING_SESSIONS,
    MAX_POSITIONS,
    MAX_POSITION_WEIGHT,
    MODEL_CAPITAL,
    apply_crypto_v2_live_prices,
    build_crypto_v2_features,
    simulate_crypto_v2,
)


def _rising_market(periods: int = 90) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=periods)
    step = np.arange(periods, dtype=float)
    data = {"BTC/USD": 100 + step * 1.2}
    data.update({
        f"C{index}/USD": 20 + index + step * (0.30 + index * 0.03)
        for index in range(1, 7)
    })
    return pd.DataFrame(data, index=dates)


def test_crypto_v2_requires_two_consecutive_long_days():
    wide = _rising_market()
    features = build_crypto_v2_features(wide)
    first_eligible = features["eligible"].any(axis=1).idxmax()
    first_confirmed = features["confirmed"].any(axis=1).idxmax()

    assert first_confirmed > first_eligible


def test_crypto_v2_limits_positions_and_position_size():
    result = simulate_crypto_v2(_rising_market(), "2025-03-20")

    assert result["trades"]
    assert all(
        trade["allocation"]
        <= MODEL_CAPITAL * MAX_POSITION_WEIGHT + 0.0001
        for trade in result["trades"]
    )
    entry_counts = {}
    entry_allocations = {}
    for trade in result["trades"]:
        entry_counts[trade["entry_date"]] = (
            entry_counts.get(trade["entry_date"], 0) + 1
        )
        entry_allocations[trade["entry_date"]] = (
            entry_allocations.get(trade["entry_date"], 0.0)
            + trade["allocation"]
        )
    assert max(entry_counts.values()) == MAX_POSITIONS
    assert max(entry_allocations.values()) <= MODEL_CAPITAL + 0.0001
    assert any(
        abs(total - MODEL_CAPITAL) < 0.0001
        for total in entry_allocations.values()
    )


def test_crypto_v2_closes_at_fixed_horizon():
    result = simulate_crypto_v2(_rising_market(), "2025-03-20")
    horizon_exits = [
        trade
        for trade in result["trades"]
        if trade["exit_reason"] == "max_horizon_5d"
    ]

    assert horizon_exits
    assert all(
        trade["held_sessions"] == MAX_HOLDING_SESSIONS
        for trade in horizon_exits
    )


def test_crypto_v2_marks_history_and_forward_separately():
    result = simulate_crypto_v2(_rising_market(), "2025-03-20")
    modes = {trade["mode"] for trade in result["trades"]}

    assert modes == {"backtest", "forward"}


def test_crypto_v2_executes_confirmation_on_next_daily_close():
    wide = _rising_market()
    features = build_crypto_v2_features(wide)
    result = simulate_crypto_v2(wide, "2025-03-20")
    first_trade = result["trades"][0]
    entry_date = pd.Timestamp(first_trade["entry_date"])
    entry_position = wide.index.get_loc(entry_date)

    assert entry_position > 0
    decision_date = wide.index[entry_position - 1]
    ticker = first_trade["ticker"]
    assert bool(features["confirmed"].at[decision_date, ticker])
    assert first_trade["entry_price"] == wide.at[entry_date, ticker]


def test_crypto_v2_live_price_updates_order_quantity():
    report = {
        "active": [{
            "ticker": "BTC/USD",
            "entry_price": 100.0,
            "allocation": 50.0,
            "current_price": 100.0,
        }],
        "forward": {"cash_result": 0.0},
    }

    apply_crypto_v2_live_prices(report, {"BTC/USD": 125.0})

    position = report["active"][0]
    assert position["current_price"] == 125.0
    assert position["order_quantity"] == 0.4
    assert position["order_quantity_display"] == "≈ 0.4"
