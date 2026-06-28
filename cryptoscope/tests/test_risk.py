"""Tests for market-regime and pair-stability safeguards."""

import json

import numpy as np

from app.core.risk import (
    assess_cointegration_stability,
    assess_market_regime,
    detect_recent_event_gap,
    forecast_scenario,
    guard_signal,
)


def _stable_pair(seed=42, n=500):
    rng = np.random.default_rng(seed)
    common = np.cumsum(rng.normal(0, 0.01, n))
    residual = np.zeros(n)
    for index in range(1, n):
        residual[index] = 0.25 * residual[index - 1] + rng.normal(0, 0.003)
    pb = 100 * np.exp(common)
    pa = 60 * np.exp(0.8 * common + residual)
    return pa, pb


def test_cointegration_stability_passes_multiple_windows():
    pa, pb = _stable_pair()

    result = assess_cointegration_stability(pa, pb)

    assert result["is_coint_stable"] is True
    assert result["coint_windows_passed"] >= 2
    assert result["coint_stability"] >= 67
    assert json.loads(result["coint_windows"])["60"] is True


def test_cointegration_stability_rejects_recent_break():
    pa, pb = _stable_pair()
    pa[-60:] *= np.exp(np.linspace(0, 0.6, 60))

    result = assess_cointegration_stability(pa, pb)

    assert result["is_coint_stable"] is False
    assert json.loads(result["coint_windows"])["60"] is False


def test_market_regime_detects_broad_stress_move():
    prices = np.full((40, 8), 100.0)
    prices *= np.exp(np.arange(40)[:, None] * 0.001)
    prices[-1] *= 0.95

    result = assess_market_regime(prices)

    assert result["market_regime"] == "stress"
    assert result["market_max_5d_move"] > 3


def test_event_gap_ignores_broad_market_move_but_flags_one_leg():
    pa = np.linspace(100, 105, 80)
    pb = np.linspace(80, 84, 80)
    market_returns = np.zeros(79)

    broad_a = pa.copy()
    broad_b = pb.copy()
    broad_a[-1] *= 0.95
    broad_b[-1] *= 0.95
    broad = detect_recent_event_gap(broad_a, broad_b, market_returns)
    assert broad["event_risk"] is False

    pa[-1] *= 0.90
    isolated = detect_recent_event_gap(
        pa,
        pb,
        market_returns,
        ticker_a="AAA",
        ticker_b="BBB",
    )
    assert isolated["event_risk"] is True
    assert "AAA" in isolated["event_risk_reason"]


def test_ru_signal_guard_blocks_unstable_pair():
    signal = {
        "signal": "Лонг AAA / Шорт BBB",
        "signal_type": "long_a",
    }
    stability = {
        "is_coint_stable": False,
        "coint_stability_reason": "Связь сломалась",
    }

    result = guard_signal(
        "ru",
        signal,
        "Прогнозный",
        stability,
        {"event_risk": False, "event_risk_reason": None},
        "stress",
    )

    assert result["signal_type"] == "wait"
    assert result["signal_eligible"] is False
    assert result["strength"] == "Наблюдение"


def test_stress_forecast_range_is_wider():
    normal = forecast_scenario(2.0, 0.4, "normal")
    stress = forecast_scenario(2.0, 0.4, "stress")

    normal_width = normal["z_forecast_high"] - normal["z_forecast_low"]
    stress_width = stress["z_forecast_high"] - stress["z_forecast_low"]
    assert stress_width > normal_width
