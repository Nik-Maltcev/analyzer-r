import numpy as np
import pandas as pd

from app.core.scanners import (
    apply_scanner_market_context,
    corr_breakdown_scan,
    drawdown_scan,
    momentum_scan,
    overlay_live_prices_on_wide,
    scanner_market_context,
)


def test_overlay_live_prices_replaces_and_appends_latest_day():
    wide = pd.DataFrame(
        {"BTC/USD": [100.0, 101.0], "ETH/USD": [50.0, 51.0]},
        index=["2026-07-24", "2026-07-25"],
    )

    replaced = overlay_live_prices_on_wide(
        wide,
        {"BTC/USD": 105.0, "ETH/USD": "bad", "NEW/USD": 1.0},
        "2026-07-25",
    )
    assert replaced.loc["2026-07-25", "BTC/USD"] == 105.0
    assert replaced.loc["2026-07-25", "ETH/USD"] == 51.0
    assert "NEW/USD" not in replaced.columns
    assert wide.loc["2026-07-25", "BTC/USD"] == 101.0  # original untouched

    appended = overlay_live_prices_on_wide(
        wide,
        {"BTC/USD": 110.0},
        "2026-07-26",
    )
    assert list(appended.index) == [
        "2026-07-24",
        "2026-07-25",
        "2026-07-26",
    ]
    assert appended.loc["2026-07-26", "BTC/USD"] == 110.0
    assert pd.isna(appended.loc["2026-07-26", "ETH/USD"])

    unchanged = overlay_live_prices_on_wide(wide, {}, "2026-07-26")
    assert list(unchanged.index) == ["2026-07-24", "2026-07-25"]


def test_momentum_recommends_direction_with_confidence():
    prices = np.column_stack([
        np.linspace(100, 160, 30),
        np.linspace(160, 100, 30),
    ])

    result = momentum_scan(prices, ["UP", "DOWN"], [])
    rows = result.set_index("ticker")

    assert rows.loc["UP", "recommendation"] == "Рассмотреть лонг"
    assert rows.loc["UP", "recommendation_class"] == "long"
    assert rows.loc["DOWN", "recommendation"] == "Рассмотреть шорт"
    assert rows.loc["DOWN", "recommendation_class"] == "short"


def test_drawdown_does_not_recommend_catching_an_active_fall():
    falling = np.concatenate([
        np.linspace(60, 100, 90),
        np.linspace(100, 60, 30),
    ])

    result = drawdown_scan(falling.reshape(-1, 1), ["FALLING"])
    row = result.iloc[0]

    assert row["recommendation"] == "Не входить"
    assert row["signal"] == "Ждать"
    assert row["recommendation_reason"] == "Падение продолжается"


def test_drawdown_allows_long_only_after_rebound_confirmation():
    rebounding = np.concatenate([
        np.linspace(60, 100, 90),
        np.linspace(100, 65, 20),
        np.linspace(65, 75, 10),
    ])

    result = drawdown_scan(rebounding.reshape(-1, 1), ["REBOUND"])
    row = result.iloc[0]

    assert row["recommendation"] == "Рассмотреть лонг"
    assert row["signal"] == "Лонг"
    assert row["recommendation_reason"] == "Отскок подтверждается"


def test_correlation_break_recommends_avoiding_unvalidated_pair():
    rng = np.random.default_rng(42)
    returns_a = rng.normal(0, 0.01, 120)
    returns_b = np.concatenate([
        returns_a[:90] + rng.normal(0, 0.001, 90),
        -returns_a[90:] + rng.normal(0, 0.001, 30),
    ])
    prices = pd.DataFrame({
        "A": 100 * np.exp(np.cumsum(returns_a)),
        "B": 80 * np.exp(np.cumsum(returns_b)),
    })

    result = corr_breakdown_scan(prices, ["A", "B"])
    row = result.iloc[0]

    assert row["recommendation"] == "Не открывать пару"
    assert row["recommendation_class"] == "wait"


def test_scanner_market_context_detects_stress_only_for_ru():
    prices = np.full((40, 8), 100.0)
    prices *= np.exp(np.arange(40)[:, None] * 0.001)
    prices[-1] *= 0.95

    ru_context = scanner_market_context(prices, "ru")
    crypto_context = scanner_market_context(prices, "crypto")

    assert ru_context["market_regime"] == "stress"
    assert crypto_context["market_regime"] == "normal"
    assert crypto_context["market_volatility"] is None


def test_ru_stress_lowers_scanner_confidence_and_adds_warning():
    records = [{
        "ticker": "SBER",
        "confidence": "Высокая",
        "risk_note": None,
    }]

    result = apply_scanner_market_context(
        records,
        "ru",
        {"market_regime": "stress"},
    )

    assert result[0]["confidence"] == "Средняя"
    assert "Стрессовый режим RU" in result[0]["risk_note"]


def test_scanner_stress_context_does_not_change_other_markets():
    records = [{
        "ticker": "BTC/USD",
        "confidence": "Высокая",
        "risk_note": None,
    }]

    result = apply_scanner_market_context(
        records,
        "crypto",
        {"market_regime": "stress"},
    )

    assert result[0]["confidence"] == "Высокая"
    assert result[0]["risk_note"] is None
