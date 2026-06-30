"""Tests for the single-asset direction probability model."""

import numpy as np
import pytest

from app.core.direction import forecast_next_close


def test_direction_forecast_is_bounded_and_backtested():
    rng = np.random.default_rng(42)
    returns = np.zeros(260)
    for index in range(1, len(returns)):
        returns[index] = (
            0.35 * returns[index - 1]
            + rng.normal(0.0004, 0.012)
        )
    prices = 100 * np.exp(np.cumsum(returns))
    timestamps = list(range(1_700_000_000, 1_700_000_000 + len(prices)))

    result = forecast_next_close(prices, timestamps)

    assert result["direction"] in {"up", "down"}
    assert 38 <= result["probability_up"] <= 62
    assert result["probability_up"] + result["probability_down"] == pytest.approx(100)
    assert 0 <= result["backtest_accuracy"] <= 100
    assert result["backtest_samples"] >= 20
    assert result["latest_timestamp"] == timestamps[-1]
    assert result["typical_move_pct"] > 0


def test_direction_forecast_rejects_short_history():
    with pytest.raises(ValueError, match="at least"):
        forecast_next_close([100 + index for index in range(30)])
