"""Tests for passwordless authentication."""

from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.db.database import set_db_path


@pytest.fixture
def app(temp_db):
    from app.main import app

    set_db_path(temp_db)
    return app


@pytest.mark.asyncio
async def test_magic_link_creates_one_time_session(app, monkeypatch):
    captured = {}
    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", "re_test")
    monkeypatch.setattr(settings, "app_base_url", "http://test")
    monkeypatch.setattr(settings, "auth_legacy_owner_email", "")

    async def fake_send(email: str, magic_link: str, request_id: str):
        captured.update({
            "email": email,
            "magic_link": magic_link,
            "request_id": request_id,
        })

    monkeypatch.setattr("app.api.auth.send_magic_link_email", fake_send)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        request_response = await client.post(
            "/api/auth/magic-link",
            json={"email": "User@Example.com"},
        )
        assert request_response.status_code == 200
        assert captured["email"] == "user@example.com"

        magic_url = urlsplit(captured["magic_link"])
        verify_response = await client.get(
            f"{magic_url.path}?{magic_url.query}",
        )
        assert verify_response.status_code == 303
        assert verify_response.headers["location"] == "/?auth=success"

        me_response = await client.get("/api/auth/me")
        assert me_response.json() == {
            "authenticated": True,
            "auth_available": True,
            "email": "user@example.com",
        }

        reused_response = await client.get(
            f"{magic_url.path}?{magic_url.query}",
        )
        assert reused_response.status_code == 303
        assert reused_response.headers["location"] == "/?auth=invalid"

        logout_response = await client.post("/api/auth/logout")
        assert logout_response.status_code == 303
        assert (await client.get("/api/auth/me")).json()["authenticated"] is False


@pytest.mark.asyncio
async def test_magic_link_rejects_invalid_email(app, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", "re_test")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/auth/magic-link",
            json={"email": "not-an-email"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_magic_link_reports_missing_resend_configuration(app, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", "")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/auth/magic-link",
            json={"email": "user@example.com"},
        )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_configured_owner_claims_legacy_favorites(
    app,
    temp_db,
    monkeypatch,
):
    captured = {}
    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", "re_test")
    monkeypatch.setattr(settings, "app_base_url", "http://test")
    monkeypatch.setattr(
        settings,
        "auth_legacy_owner_email",
        "owner@example.com",
    )

    async def fake_send(email: str, magic_link: str, request_id: str):
        captured["magic_link"] = magic_link

    monkeypatch.setattr("app.api.auth.send_magic_link_email", fake_send)

    import sqlite3

    conn = sqlite3.connect(temp_db)
    conn.execute(
        """
        INSERT INTO favorites (
            pair, market, ticker_a, ticker_b, status, user_id
        )
        VALUES ('BTC_ETH', 'crypto', 'BTC/USD', 'ETH/USD', 'active', 'local')
        """
    )
    conn.commit()
    conn.close()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        await client.post(
            "/api/auth/magic-link",
            json={"email": "owner@example.com"},
        )
        magic_url = urlsplit(captured["magic_link"])
        await client.get(f"{magic_url.path}?{magic_url.query}")

        favorites = await client.get("/api/favorites")

    assert favorites.status_code == 200
    assert favorites.json()["total"] == 1
