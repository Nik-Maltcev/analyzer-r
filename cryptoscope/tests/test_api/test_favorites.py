"""Tests for market-aware favorites pricing."""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import SESSION_COOKIE_NAME, hash_auth_token
from app.config import get_settings
from app.db.database import (
    fetch_favorites,
    get_connection,
    init_db,
    set_db_path,
    toggle_favorite,
)
from app.db.schema import CREATE_PAIRS

TEST_USER_ID = "test-user"
TEST_EMAIL = "user@example.com"
TEST_SESSION = "test-session-token"


def _add_auth_session(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO auth_users (id, email)
        VALUES (?, ?)
        """,
        (TEST_USER_ID, TEST_EMAIL),
    )
    conn.execute(
        """
        INSERT INTO auth_sessions (token_hash, user_id, expires_at)
        VALUES (?, ?, '2099-01-01 00:00:00')
        """,
        (hash_auth_token(TEST_SESSION), TEST_USER_ID),
    )


@pytest.fixture
def app(temp_db):
    from app.main import app

    set_db_path(temp_db)
    return app


@pytest.mark.asyncio
async def test_ru_favorite_uses_moex_market_prices(app, temp_db):
    conn = sqlite3.connect(temp_db)
    _add_auth_session(conn)
    conn.executemany(
        "INSERT INTO prices (ticker, date, close, volume, market) VALUES (?, ?, ?, ?, ?)",
        [
            ("SBER", "2026-06-25", 100, 1, "ru"),
            ("SBER", "2026-06-26", 110, 1, "ru"),
            ("GAZP", "2026-06-25", 200, 1, "ru"),
            ("GAZP", "2026-06-26", 190, 1, "ru"),
        ],
    )
    conn.execute(
        """
        INSERT INTO favorites (
            pair, market, ticker_a, ticker_b, signal, signal_type,
            price_a_entry, price_b_entry, entry_time, status, user_id
        )
        VALUES (
            'SBER_GAZP', 'ru', 'SBER', 'GAZP', 'Test', 'long_a',
            100, 200, '2026-06-25 12:00:00', 'active', ?
        )
        """,
        (TEST_USER_ID,),
    )
    conn.commit()
    conn.close()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(SESSION_COOKIE_NAME, TEST_SESSION)
        response = await client.get("/api/favorites")
        assert response.status_code == 200
        favorite = response.json()["favorites"][0]
        assert favorite["market"] == "ru"
        assert favorite["price_a_now"] == 110
        assert favorite["price_b_now"] == 190
        assert favorite["pnl_total_pct"] == 15.0

        tab_response = await client.get("/tab/favorites")
        assert tab_response.status_code == 200
        assert "Обновить котировки RU" in tab_response.text

        close_response = await client.post("/api/favorites/close/1")
        assert close_response.status_code == 200

    conn = sqlite3.connect(temp_db)
    closed = conn.execute(
        """
        SELECT exit_price_a, exit_price_b, exit_pnl_pct, status
        FROM favorites WHERE id = 1
        """
    ).fetchone()
    conn.close()
    assert closed == (110, 190, 15.0, "closed")


@pytest.mark.asyncio
async def test_refresh_ru_favorites_fetches_only_user_tickers(
    app,
    temp_db,
    monkeypatch,
):
    conn = sqlite3.connect(temp_db)
    _add_auth_session(conn)
    conn.execute(
        """
        INSERT INTO favorites (
            pair, market, ticker_a, ticker_b, signal_type,
            price_a_entry, price_b_entry, entry_time, status, user_id
        )
        VALUES (
            'SBER_GAZP', 'ru', 'SBER', 'GAZP', 'long_a',
            300, 125, datetime('now'), 'active', ?
        )
        """,
        (TEST_USER_ID,),
    )
    conn.commit()
    conn.close()
    captured = []

    async def fake_refresh(tickers):
        captured.extend(tickers)
        return {
            "prices": {"SBER": 301.25, "GAZP": 126.4},
            "updated_at": datetime(2026, 6, 29, 9, 30, tzinfo=timezone.utc),
            "cached": False,
        }

    monkeypatch.setattr(
        "app.api.favorites.refresh_ru_live_prices",
        fake_refresh,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(SESSION_COOKIE_NAME, TEST_SESSION)
        response = await client.post("/api/favorites/refresh-ru")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "updated": 2,
        "cached": False,
        "updated_at": "2026-06-29T09:30:00+00:00",
    }
    assert captured == ["GAZP", "SBER"]
    conn = sqlite3.connect(temp_db)
    ru_price_count = conn.execute(
        "SELECT COUNT(*) FROM prices WHERE market = 'ru'"
    ).fetchone()[0]
    conn.close()
    assert ru_price_count == 0


@pytest.mark.asyncio
async def test_favorites_require_authentication(app, monkeypatch):
    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/favorites")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_legacy_favorites_work_until_resend_is_configured(
    app,
    temp_db,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "resend_api_key", "")
    conn = sqlite3.connect(temp_db)
    conn.execute(
        """
        INSERT INTO favorites (
            pair, market, ticker_a, ticker_b, signal_type,
            price_a_entry, price_b_entry, entry_time, status, user_id
        )
        VALUES (
            'BTC_ETH', 'crypto', 'BTC/USD', 'ETH/USD', 'long_a',
            40000, 2000, datetime('now'), 'active', 'local'
        )
        """
    )
    conn.commit()
    conn.close()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/favorites")

    assert response.status_code == 200
    assert response.json()["total"] == 1


@pytest.mark.asyncio
async def test_user_cannot_close_another_users_favorite(app, temp_db):
    conn = sqlite3.connect(temp_db)
    _add_auth_session(conn)
    conn.execute(
        """
        INSERT INTO auth_users (id, email)
        VALUES ('other-user', 'other@example.com')
        """
    )
    conn.execute(
        """
        INSERT INTO favorites (
            pair, market, ticker_a, ticker_b, signal_type,
            price_a_entry, price_b_entry, entry_time, status, user_id
        )
        VALUES (
            'BTC_ETH', 'crypto', 'BTC/USD', 'ETH/USD', 'long_a',
            40000, 2000, datetime('now'), 'active', 'other-user'
        )
        """
    )
    favorite_id = conn.execute(
        "SELECT id FROM favorites WHERE user_id = 'other-user'"
    ).fetchone()[0]
    conn.commit()
    conn.close()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(SESSION_COOKIE_NAME, TEST_SESSION)
        response = await client.post(f"/api/favorites/close/{favorite_id}")

    assert response.status_code == 404
    conn = sqlite3.connect(temp_db)
    status = conn.execute(
        "SELECT status FROM favorites WHERE id = ?",
        (favorite_id,),
    ).fetchone()[0]
    conn.close()
    assert status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "ticker_a", "ticker_b", "price_a", "price_b"),
    [
        ("stocks", "AAPL", "MSFT", 200, 500),
        ("br", "PETR4", "VALE3", 32, 56),
        ("id", "BBCA", "TLKM", 6175, 2480),
    ],
)
async def test_favorite_records_non_crypto_market_and_entry_prices(
    temp_db,
    market,
    ticker_a,
    ticker_b,
    price_a,
    price_b,
):
    conn = sqlite3.connect(temp_db)
    conn.executemany(
        "INSERT INTO prices (ticker, date, close, volume, market) VALUES (?, ?, ?, ?, ?)",
        [
            (ticker_a, "2026-06-26", price_a, 1, market),
            (ticker_b, "2026-06-26", price_b, 1, market),
        ],
    )
    conn.commit()
    conn.close()

    async with get_connection(temp_db) as db:
        result = await toggle_favorite(
            db,
            f"{ticker_a}_{ticker_b}",
            ticker_a,
            ticker_b,
            market=market,
            signal_type="long_a",
        )
        favorites = await fetch_favorites(db)

    assert result["entry_a"] == price_a
    assert result["entry_b"] == price_b
    assert favorites.iloc[0]["market"] == market


@pytest.mark.asyncio
async def test_existing_favorite_market_is_inferred_from_pair():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(CREATE_PAIRS)
    conn.execute(
        """
        CREATE TABLE favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            ticker_a TEXT NOT NULL,
            ticker_b TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            user_id TEXT DEFAULT 'local'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO pairs (market, ticker_a, ticker_b)
        VALUES ('ru', 'SBER', 'GAZP')
        """
    )
    conn.execute(
        """
        INSERT INTO favorites (pair, ticker_a, ticker_b)
        VALUES ('SBER_GAZP', 'SBER', 'GAZP')
        """
    )
    conn.commit()
    conn.close()

    try:
        await init_db(path)
        conn = sqlite3.connect(path)
        market = conn.execute(
            "SELECT market FROM favorites WHERE pair = 'SBER_GAZP'"
        ).fetchone()[0]
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(favorites)").fetchall()
        }
        conn.close()
        assert market == "ru"
        assert {
            "hedge_ratio_entry",
            "spread_mean_entry",
            "spread_sd_entry",
        }.issubset(columns)
    finally:
        os.unlink(path)
