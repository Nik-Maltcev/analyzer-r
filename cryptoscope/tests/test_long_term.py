from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.core.long_term import (
    BOLLINGER_LIVE_VERSION,
    build_bollinger_recovery_report,
    build_long_term_investment_report,
    evaluate_long_term_strategies,
    wilson_interval,
)


def _growth_prices(days: int = 1300, tickers: int = 10) -> pd.DataFrame:
    dates = pd.date_range("2022-01-01", periods=days, freq="D")
    rows = []
    for index in range(tickers):
        daily_growth = 0.0008 + index * 0.00002
        closes = 10 * np.power(1 + daily_growth, np.arange(days))
        rows.extend(
            {"ticker": f"C{index}/USD", "date": date, "close": close}
            for date, close in zip(dates, closes)
        )
    return pd.DataFrame(rows)


def test_wilson_interval_is_conservative_for_small_samples():
    lower, upper = wilson_interval(10, 10)

    assert 0.70 < lower < 0.80
    assert upper == 1.0


def test_long_term_research_uses_independent_profitable_events():
    events, report = evaluate_long_term_strategies(
        _growth_prices(),
        minimum_independent_events=1,
    )

    trend = next(row for row in report["strategies"] if row["strategy"] == "trend")
    benchmark = next(
        row for row in report["strategies"] if row["strategy"] == "buy_hold"
    )
    assert trend["asset_events"] >= 10
    assert trend["independent_market_episodes"] >= 1
    assert trend["probability_pct"] == 100.0
    assert benchmark["probability_pct"] == 100.0
    assert report["strategy_count"] == 30
    assert (events.loc[events["strategy"] == "trend", "net_return_pct"] > 15).all()


def test_long_term_research_does_not_qualify_small_samples_at_80_percent():
    _events, report = evaluate_long_term_strategies(
        _growth_prices(tickers=2),
        minimum_independent_events=20,
    )

    assert report["qualified_strategies"] == []


def test_bollinger_live_report_uses_last_completed_month_and_next_day_entry():
    dates = pd.date_range(end="2024-06-15", periods=900, freq="D")
    closes = np.full(len(dates), 100.0)
    closes += np.sin(np.arange(len(dates)) / 7) * 0.5
    prices = pd.DataFrame({"ticker": "TEST/USD", "date": dates, "close": closes})
    prices.loc[prices["date"] == "2024-05-01", "close"] = 95.0
    prices.loc[prices["date"] == "2024-05-10", "close"] = 70.0
    prices.loc[prices["date"] == "2024-05-31", "close"] = 110.0
    prices.loc[prices["date"] >= "2024-06-01", "close"] = 111.0
    research = {
        "research": {
            "strategies": [
                {
                    "strategy": "bollinger_recovery",
                    "asset_events": 15,
                    "asset_successes": 12,
                    "asset_probability_pct": 80.0,
                    "independent_market_episodes": 4,
                    "median_net_return_pct": 92.26,
                    "worst_drawdown_pct": -71.78,
                    "qualifies": False,
                }
            ]
        }
    }

    report = build_bollinger_recovery_report(prices, research)

    assert report["is_ready"] is True
    assert report["strategy_version"] == BOLLINGER_LIVE_VERSION
    assert report["evaluation_date"] == "2024-05-31"
    assert report["next_evaluation_date"] == "2024-06-30"
    assert report["historical"]["asset_probability_pct"] == 80.0
    assert report["is_validated"] is False
    assert report["scan"][0]["signal"] is True
    assert report["active"][0]["entry_date"] == "2024-06-01"
    assert report["active"][0]["planned_exit"] == "2025-06-01"


def _deposit_research(*, qualifies: bool = True) -> dict:
    return {
        "deposit_research": {
            "required_probability_pct": 80,
            "round_trip_cost_pct": 0.4,
            "plans": [
                {
                    "key": "12m-15",
                    "strategies": [
                        {
                            "strategy": "trend_price_sma100",
                            "label": "Price above SMA 100",
                            "asset_events": 40,
                            "asset_probability_pct": 85.0,
                            "independent_market_episodes": 24,
                            "familywise_confidence_low_pct": 81.0,
                            "median_net_return_pct": 22.0,
                            "quartile_25_net_return_pct": -6.0,
                            "qualifies": qualifies,
                        }
                    ],
                }
            ],
        }
    }


def test_long_term_investment_report_prices_a_new_purchase_today():
    report = build_long_term_investment_report(
        _growth_prices(days=500, tickers=1),
        _deposit_research(),
    )

    plan = next(row for row in report["plans"] if row["key"] == "12m-15")
    candidate = plan["best_candidate"]
    assert report["is_ready"] is True
    assert report["qualified_count"] == 1
    assert plan["decision"] == "buy"
    assert candidate["ticker"] == "C0/USD"
    assert candidate["target_price"] > candidate["current_price"]
    assert candidate["historical_probability_pct"] == 85.0


@pytest.mark.asyncio
async def test_long_term_routes_return_json_and_render_html(tmp_path, monkeypatch):
    prices = _growth_prices(days=500, tickers=1)
    data_path = tmp_path / "daily.csv"
    report_path = tmp_path / "report.json"
    prices.to_csv(data_path, index=False)
    report_path.write_text(
        json.dumps(_deposit_research()),
        encoding="utf-8",
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "long_term_data_path", str(data_path))
    monkeypatch.setattr(settings, "long_term_report_path", str(report_path))

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        api_response = await client.get("/api/long-term")
        html_response = await client.get("/tab/long-term")

    assert api_response.status_code == 200
    assert api_response.json()["default_plan_key"] == "12m-15"
    assert html_response.status_code == 200
    assert "Что можно купить сегодня" in html_response.text
