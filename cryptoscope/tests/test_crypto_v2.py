import numpy as np
import pandas as pd

from app.core.crypto_v2 import (
    MAX_HOLDING_SESSIONS,
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
    assert all(trade["allocation"] <= 150.0001 for trade in result["trades"])
    entry_counts = {}
    for trade in result["trades"]:
        entry_counts[trade["entry_date"]] = (
            entry_counts.get(trade["entry_date"], 0) + 1
        )
    assert max(entry_counts.values()) <= 3


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
