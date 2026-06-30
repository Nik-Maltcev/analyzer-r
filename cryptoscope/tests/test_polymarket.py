"""Tests for Polymarket data normalization and UI routes."""

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.data.polymarket import (
    POLYMARKET_ASSETS,
    parse_pyth_history,
    parse_yahoo_history,
    sample_moscow_23_boundaries,
)
from app.db.database import set_db_path


def test_parse_pyth_history_sorts_and_drops_invalid_prices():
    timestamps, prices = parse_pyth_history({
        "s": "ok",
        "t": [300, 100, 200, 400],
        "c": [103.0, 101.0, None, -1],
    })

    assert timestamps == [100, 300]
    assert prices == [101.0, 103.0]


def test_parse_yahoo_history_uses_adjusted_close():
    timestamps, prices = parse_yahoo_history({
        "chart": {
            "result": [{
                "timestamp": [100, 200],
                "indicators": {
                    "adjclose": [{"adjclose": [10.5, 11.25]}],
                    "quote": [{"close": [10.0, 11.0]}],
                },
            }]
        }
    })

    assert timestamps == [100, 200]
    assert prices == [10.5, 11.25]


def test_history_is_sampled_at_completed_23_moscow_boundaries():
    def timestamp(day, hour):
        return int(datetime(
            2026,
            1,
            day,
            hour,
            tzinfo=timezone.utc,
        ).timestamp())

    sampled_timestamps, sampled_prices = sample_moscow_23_boundaries(
        [
            timestamp(1, 19),
            timestamp(1, 20),
            timestamp(2, 19),
            timestamp(2, 20),
        ],
        [100, 999, 101, 999],
        now=datetime(2026, 1, 3, 18, tzinfo=timezone.utc),
    )

    assert sampled_timestamps == [timestamp(1, 20), timestamp(2, 20)]
    assert sampled_prices == [100, 101]


def test_polymarket_catalog_contains_requested_assets():
    assert {asset.key for asset in POLYMARKET_ASSETS} == {
        "SPY",
        "PLTR",
        "GOOGL",
        "NVDA",
        "AMZN",
        "MSFT",
        "META",
        "ABNB",
        "COIN",
        "TSLA",
        "RKLB",
        "AAPL",
        "SPX",
        "HSI",
        "NIK",
        "DJIA",
        "UKX",
        "DAX",
        "RUT",
        "NYA",
        "WTI",
        "XAUUSD",
        "XAGUSD",
        "NG",
    }


@pytest.fixture
def app(temp_db):
    set_db_path(temp_db)
    from app.main import app

    return app


def _forecast_result():
    forecast = {
        "key": "SPY",
        "label": "S&P 500 ETF",
        "category": "equities",
        "category_label": "ETF",
        "source": "Pyth",
        "source_kind": "pyth",
        "available": True,
        "quality": "Умеренный",
        "direction": "up",
        "direction_probability": 58.4,
        "probability_up": 58.4,
        "probability_down": 41.6,
        "edge_pp": 8.4,
        "latest_price": 741.25,
        "latest_timestamp": 1_782_777_600,
        "day_change_pct": 0.42,
        "momentum_5d_pct": 1.7,
        "typical_move_pct": 0.81,
        "volatility_20d_pct": 1.04,
        "backtest_accuracy": 56.8,
        "baseline_accuracy": 52.1,
        "backtest_samples": 44,
        "observations": 171,
        "brier_score": 0.244,
        "pyth_symbol": "Equity.US.SPY/USD",
    }
    return {
        "forecasts": [forecast],
        "leader": forecast,
        "available_count": 1,
        "total_count": 1,
        "updated_at": datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
        "cached": False,
    }


@pytest.mark.asyncio
async def test_polymarket_tab_and_results_render(app, monkeypatch):
    async def fake_forecasts(force=False):
        return _forecast_result()

    monkeypatch.setattr(
        "app.api.polymarket.get_polymarket_forecasts",
        fake_forecasts,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        shell = await client.get("/tab/polymarket")
        results = await client.get("/tab/polymarket/results")
        api_response = await client.get("/api/polymarket")
        app_page = await client.get("/app")

    assert shell.status_code == 200
    assert 'id="polymarket-results"' in shell.text
    assert results.status_code == 200
    assert "S&amp;P 500 ETF" in results.text
    assert "58.4%" in results.text
    assert api_response.status_code == 200
    assert api_response.json()["leader"]["key"] == "SPY"
    assert 'data-tab="polymarket"' in app_page.text
