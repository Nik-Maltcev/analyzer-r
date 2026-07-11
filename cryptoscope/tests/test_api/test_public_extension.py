"""Tests for the limited public browser-extension feed."""

import os
import sqlite3
import sys

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.config import get_settings
from app.db.database import set_db_path


@pytest.fixture
def app(temp_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_variant", "br")
    monkeypatch.setattr(settings, "resend_api_key", "re_test")
    set_db_path(temp_db)

    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            """
            INSERT INTO pairs (
                market, ticker_a, ticker_b, corr, halflife, t_stat,
                is_coint, score, z_now, signal, signal_type, strength,
                signal_started_at, computed_at
            ) VALUES (
                'br', 'PETR4.SA', 'VALE3.SA', 0.82, 12, -3.4,
                1, 1.4, -2.6, 'Long A', 'long_a', 'Strong',
                datetime('now', '-2 days'), datetime('now')
            )
            """
        )
        conn.commit()

    from app.main import app
    return app


@pytest.mark.asyncio
async def test_public_feed_does_not_require_authentication(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/public/extension/feed?market=br")

    assert response.status_code == 200
    payload = response.json()
    assert payload["market"] == "br"
    assert payload["total"] == 1
    assert payload["items"][0]["recommendation"] == "Comprar PETR4.SA / Vender VALE3.SA"
    assert payload["items"][0]["signal_days"] == 2
    assert response.headers["cache-control"] == "public, max-age=300"


@pytest.mark.asyncio
async def test_public_feed_rejects_market_outside_edition(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/public/extension/feed?market=ru")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_extension_privacy_page_is_public(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/privacy/extension")

    assert response.status_code == 200
    assert "Política de privacidade da extensão" in response.text
