"""Тесты «Live-сигналы» — кросс-секционный моментум (admin-only, крипта)."""

from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta

import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import SESSION_COOKIE_NAME, hash_auth_token
from app.config import get_settings
from app.core import live_momentum
from app.core.live_momentum import (
    compute_cross_sectional_momentum,
    merge_live_prices,
)
from app.db.database import get_sync_connection, set_db_path


def _synthetic_prices(returns: dict[str, float], days: int = 16) -> pd.DataFrame:
    """Цены, у которых доходность за lookback=14 равна заданной (base=day1, last=day15)."""
    start = date(2026, 1, 1)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    rows = []
    for ticker, ret in returns.items():
        closes = [100.0] * days
        closes[days - 1] = 100.0 * (1 + ret)   # last
        for i, d in enumerate(dates):
            rows.append({"ticker": ticker, "date": d, "close": closes[i]})
    return pd.DataFrame(rows)


# --- Чистая функция ранжирования (без сети/БД) ------------------------------

def test_ranks_long_top_short_bottom():
    prices = _synthetic_prices({
        "AAA/USD": 0.50, "BBB/USD": 0.40, "CCC/USD": 0.30, "DDD/USD": 0.20,
        "EEE/USD": 0.10, "FFF/USD": 0.05, "GGG/USD": -0.05, "HHH/USD": -0.10,
        "III/USD": -0.20, "JJJ/USD": -0.30,
    })
    report = compute_cross_sectional_momentum(prices, lookback=14)

    assert report["status"] == "ok"
    assert report["universe"] == 10
    assert report["legs_per_side"] == 2  # 0.2 * 10
    assert [x["ticker"] for x in report["long_legs"]] == ["AAA/USD", "BBB/USD"]
    assert [x["ticker"] for x in report["short_legs"]] == ["III/USD", "JJJ/USD"]
    assert report["long_legs"][0]["momentum_pct"] == 50.0
    assert report["short_legs"][-1]["momentum_pct"] == -30.0


def test_delta_neutral_equal_weights():
    prices = _synthetic_prices({f"T{i}/USD": (0.4 - i * 0.05) for i in range(10)})
    report = compute_cross_sectional_momentum(prices, lookback=14)
    legs = report["legs_per_side"]
    # половина капитала на сторону, равный вес на ногу
    assert abs(report["long_legs"][0]["weight"] - 0.5 / legs) < 1e-9
    total = sum(x["weight"] for x in report["long_legs"])
    assert abs(total - 0.5) < 1e-9
    assert abs(sum(x["weight"] for x in report["short_legs"]) - 0.5) < 1e-9


def test_insufficient_history_and_no_data():
    empty = compute_cross_sectional_momentum(pd.DataFrame(), lookback=14)
    assert empty["status"] == "no_data"

    short = _synthetic_prices({f"T{i}/USD": 0.1 for i in range(8)}, days=5)
    assert compute_cross_sectional_momentum(short, lookback=14)["status"] == "insufficient_history"


def test_merge_live_prices_marks_legs():
    prices = _synthetic_prices({f"T{i}/USD": (0.4 - i * 0.05) for i in range(10)})
    report = compute_cross_sectional_momentum(prices, lookback=14)
    top = report["long_legs"][0]["ticker"]
    merged = merge_live_prices(report, {top: 160.0})  # была 140 (100*1.4) -> +14.3%
    leg = merged["long_legs"][0]
    assert leg["live_price"] == 160.0
    assert leg["intraday_pct"] == pytest.approx((160 / 140 - 1) * 100, abs=0.01)
    assert merged["live_marked_legs"] == 1


# --- Доступ через HTTP -------------------------------------------------------

@pytest.fixture
def app(temp_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_variant", "global")
    monkeypatch.setattr(settings, "resend_api_key", "re_test")
    monkeypatch.setattr(settings, "auth_legacy_owner_email", "")
    monkeypatch.setattr(settings, "auth_admin_emails", "")

    from app.main import app as fastapi_app

    set_db_path(temp_db)
    live_momentum._BASKET_CACHE.clear()
    return fastapi_app


def _add_user(temp_db, trial_modifier="+3 days"):
    token = "session-ls"
    with closing(get_sync_connection(temp_db)) as conn:
        conn.execute(
            """
            INSERT INTO auth_users (id, email, trial_started_at, trial_ends_at)
            VALUES ('user-1', 'user@example.com', datetime('now'), datetime('now', ?))
            """,
            (trial_modifier,),
        )
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES (?, 'user-1', datetime('now', '+1 day'))
            """,
            (hash_auth_token(token),),
        )
        conn.commit()
    return token


def _seed_crypto(temp_db):
    prices = _synthetic_prices({f"T{i}/USD": (0.4 - i * 0.05) for i in range(10)})
    with closing(get_sync_connection(temp_db)) as conn:
        for row in prices.to_dict(orient="records"):
            conn.execute(
                "INSERT OR REPLACE INTO prices (ticker, date, close, market) VALUES (?,?,?,'crypto')",
                (row["ticker"], row["date"], row["close"]),
            )
        conn.commit()


@pytest.mark.asyncio
async def test_admin_sees_live_signals(app, temp_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_legacy_owner_email", "user@example.com")

    async def _no_net(tickers, ttl_seconds=30):
        return {"prices": {}}

    monkeypatch.setattr("app.data.mexc_market.refresh_crypto_live_prices", _no_net)
    _seed_crypto(temp_db)
    live_momentum._BASKET_CACHE.clear()
    token = _add_user(temp_db)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: token}
    ) as client:
        resp = await client.get("/tab/live-signals")

    assert resp.status_code == 200
    assert "Live-сигналы" in resp.text
    assert "T0/USD" in resp.text          # топ-моментум в LONG
    assert "кросс-секционный" in resp.text.lower()


@pytest.mark.asyncio
async def test_non_admin_gets_404(app, temp_db):
    token = _add_user(temp_db)  # обычный юзер с триалом, не админ
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: token}
    ) as client:
        resp = await client.get("/tab/live-signals")

    assert resp.status_code == 404
