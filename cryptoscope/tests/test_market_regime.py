import json

import aiosqlite
import numpy as np
import pandas as pd
import pytest

from app.core.market_regime import (
    CALCULATION_VERSION,
    REGIME_ORDER,
    build_market_regime_history,
    build_regime_trade_plan,
    fetch_market_regime_report,
)
from app.db.schema import (
    CREATE_MARKET_REGIME_SNAPSHOTS,
    CREATE_SCANNER_SIGNAL_PERIODS,
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


def _latest_for_plan(
    regime: str,
    *,
    trend_direction: str = "mixed",
    risk_multiplier: float = 1.0,
    momentum_status: str = "limited",
) -> dict:
    return {
        "dominant_regime": regime,
        "trend_direction": trend_direction,
        "risk_state": "panic" if regime == "panic" else "normal",
        "risk_multiplier": risk_multiplier,
        "strategies": [
            {
                "key": "momentum",
                "status": momentum_status,
                "weight": 0.3,
            },
            {
                "key": "drawdown",
                "status": "limited",
                "weight": 0.2,
            },
        ],
    }


def _period(
    ticker: str,
    *,
    scanner: str = "momentum",
    direction: str = "long",
    confidence: str = "Высокая",
    age: int = 1,
) -> dict:
    return {
        "scanner": scanner,
        "ticker_a": ticker,
        "direction": direction,
        "confidence": confidence,
        "first_seen_date": "2026-07-28",
        "last_seen_date": "2026-07-28",
        "observation_count": age,
        "current_price": 123.45,
    }


def test_range_trade_plan_returns_specific_long_and_short_candidates():
    report = build_regime_trade_plan(
        _latest_for_plan("range"),
        [
            _period("ETH/USD", scanner="drawdown", direction="long"),
            _period("SOL/USD", direction="short", confidence="Средняя"),
            _period("DOGE/USD", confidence="Низкая"),
        ],
    )

    assert [item["ticker"] for item in report["candidates"]] == [
        "ETH/USD",
        "SOL/USD",
    ]
    assert report["candidates"][0]["action_label"] == "Купить"
    assert report["candidates"][1]["action_label"] == "Шорт"
    assert report["rejected_count"] == 1


def test_trade_plan_does_not_hide_valid_candidates_after_fifth_item():
    report = build_regime_trade_plan(
        _latest_for_plan("range"),
        [
            _period(f"ASSET{index}/USD", direction="short")
            for index in range(7)
        ],
    )

    assert report["count"] == 7


def test_trend_trade_plan_rejects_signals_against_btc_direction():
    report = build_regime_trade_plan(
        _latest_for_plan("trend", trend_direction="up"),
        [
            _period("ETH/USD", direction="long"),
            _period("SOL/USD", direction="short"),
        ],
    )

    assert [item["ticker"] for item in report["candidates"]] == ["ETH/USD"]
    assert report["rejected_count"] == 1


def test_trade_plan_removes_conflicting_direction_for_same_coin():
    report = build_regime_trade_plan(
        _latest_for_plan("range"),
        [
            _period("ETH/USD", direction="long"),
            _period("ETH/USD", direction="short"),
        ],
    )

    assert report["candidates"] == []
    assert report["conflict_count"] == 1
    assert "не прошли фильтр" in report["empty_reason"]


def test_panic_trade_plan_keeps_only_high_confidence_short():
    report = build_regime_trade_plan(
        _latest_for_plan(
            "panic",
            trend_direction="down",
            risk_multiplier=0.15,
            momentum_status="off",
        ),
        [
            _period("ETH/USD", direction="long"),
            _period("SOL/USD", direction="short", confidence="Средняя"),
            _period("BTC/USD", direction="short"),
        ],
    )

    assert [item["ticker"] for item in report["candidates"]] == ["BTC/USD"]
    assert report["position_size_label"] == "не более 15% обычного размера"


@pytest.mark.asyncio
async def test_report_loads_fresh_trade_plan_from_persisted_scanner_periods():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(CREATE_MARKET_REGIME_SNAPSHOTS)
        await conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)
        await conn.execute(
            """
            CREATE TABLE prices (
                market TEXT,
                ticker TEXT,
                date TEXT,
                close REAL
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO prices (market, ticker, date, close)
            VALUES ('crypto', 'ETH/USD', '2026-07-28', 3200.0)
            """
        )
        await conn.execute(
            """
            INSERT INTO scanner_signal_periods (
                market, scanner, signal_key, ticker_a, ticker_b,
                direction, confidence, first_seen_date, last_seen_date,
                observation_count, status
            ) VALUES (
                'crypto', 'drawdown', 'ETH/USD', 'ETH/USD', '',
                'long', 'Высокая', '2026-07-27', '2026-07-28',
                2, 'active'
            )
            """
        )
        metrics = {
            "btc_price": 100000,
            "btc_return_7": 1.0,
            "btc_return_20": 1.5,
            "btc_sma_spread": 0.4,
            "btc_volatility": 35.0,
            "volatility_percentile": 50.0,
            "drawdown_60": -4.0,
            "breadth_20": 45.0,
            "breadth_change_5": 2.0,
            "average_correlation": 0.4,
            "dispersion_7": 6.0,
            "universe_size": 87,
        }
        strategies = _latest_for_plan("range")["strategies"]
        await conn.execute(
            """
            INSERT INTO market_regime_snapshots (
                calculation_version, market, data_date, dominant_regime,
                trend_direction, risk_state, risk_multiplier, confidence,
                probabilities_json, metrics_json, strategies_json,
                warnings_json
            ) VALUES (?, 'crypto', '2026-07-28', 'range', 'mixed',
                      'normal', 1.0, 0.2, ?, ?, ?, '[]')
            """,
            (
                CALCULATION_VERSION,
                json.dumps({
                    "trend": 0.2,
                    "range": 0.5,
                    "panic": 0.1,
                    "recovery": 0.2,
                }),
                json.dumps(metrics),
                json.dumps(strategies, ensure_ascii=False),
            ),
        )
        await conn.commit()

        report = await fetch_market_regime_report(conn)

    assert report["trade_plan"]["count"] == 1
    assert report["trade_plan"]["candidates"][0]["ticker"] == "ETH/USD"
    assert report["trade_plan"]["candidates"][0]["current_price_label"] == "$3 200.00"
