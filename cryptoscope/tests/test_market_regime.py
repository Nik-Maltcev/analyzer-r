import numpy as np
import pandas as pd

from app.core.market_regime import (
    REGIME_ORDER,
    build_market_regime_history,
)


def _wide_prices(
    btc_returns: np.ndarray,
    *,
    tickers: int = 8,
    dispersion: float = 0.001,
) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=len(btc_returns), freq="D")
    btc = 100.0 * np.exp(np.cumsum(btc_returns))
    data = {"BTC/USD": btc}
    for index in range(tickers - 1):
        phase = np.sin(np.arange(len(dates)) / (5.0 + index))
        asset_returns = btc_returns * (0.85 + index * 0.03)
        asset_returns = asset_returns + phase * dispersion
        data[f"ASSET{index}/USD"] = (10 + index) * np.exp(
            np.cumsum(asset_returns)
        )
    return pd.DataFrame(data, index=dates)


def test_probabilities_are_normalized_and_finite():
    returns = np.full(120, 0.004)
    history = build_market_regime_history(_wide_prices(returns))

    assert history
    probabilities = history[-1]["probabilities"]
    assert set(probabilities) == set(REGIME_ORDER)
    assert abs(sum(probabilities.values()) - 1.0) < 1e-5
    assert all(np.isfinite(value) for value in probabilities.values())
    assert history[-1]["dominant_regime"] == "trend"
    assert history[-1]["trend_direction"] == "up"


def test_sharp_correlated_selloff_enables_protective_risk():
    returns = np.concatenate([
        np.full(90, 0.001),
        np.full(8, -0.045),
    ])
    history = build_market_regime_history(
        _wide_prices(returns, dispersion=0.0001)
    )

    latest = history[-1]
    assert latest["dominant_regime"] == "panic"
    assert latest["risk_state"] == "panic"
    assert latest["risk_multiplier"] <= 0.15


def test_future_prices_do_not_rewrite_past_snapshot():
    base = _wide_prices(np.full(110, 0.002))
    baseline = build_market_regime_history(base, sessions=90)
    comparison_date = baseline[-6]["data_date"]
    expected = next(
        item for item in baseline if item["data_date"] == comparison_date
    )

    changed = base.copy()
    changed.iloc[-5:, :] *= np.array(
        [0.90, 0.82, 0.87, 0.84, 0.89]
    )[:, None]
    recalculated = build_market_regime_history(changed, sessions=90)
    actual = next(
        item for item in recalculated if item["data_date"] == comparison_date
    )

    assert actual["probabilities"] == expected["probabilities"]
    assert actual["metrics"] == expected["metrics"]
