import numpy as np
import pandas as pd

from app.core.scanners import (
    corr_breakdown_scan,
    drawdown_scan,
    momentum_scan,
)


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
