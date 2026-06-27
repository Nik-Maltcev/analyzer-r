"""Tests for health endpoint."""

import pytest
from httpx import AsyncClient, ASGITransport
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.db.database import set_db_path
from app.config import get_settings


@pytest.fixture
def app(temp_db):
    """Create test FastAPI app with temp database."""
    set_db_path(temp_db)
    
    from app.main import app
    return app


@pytest.mark.asyncio
async def test_health_endpoint(app, temp_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["ok", "degraded"]
        assert "version" in data
        assert "uptime_seconds" in data


@pytest.mark.asyncio
async def test_liveness_endpoint(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"


@pytest.mark.asyncio
async def test_metrics_endpoint(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/metrics")
        assert response.status_code == 200
        text = response.text
        assert "cryptoscope_uptime_seconds" in text


@pytest.mark.asyncio
async def test_signals_endpoint(app, temp_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/signals?market=crypto")
        assert response.status_code == 200
        data = response.json()
        assert "total" in data
        assert "active" in data
        assert "signals" in data


@pytest.mark.asyncio
async def test_brazil_signals_endpoint(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/signals?market=br")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["active"] == 0


@pytest.mark.asyncio
async def test_dashboard_endpoint(app, temp_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/signals/dashboard?market=crypto")
        assert response.status_code == 200
        data = response.json()
        assert "n_active" in data
        assert "n_total" in data


@pytest.mark.asyncio
async def test_index_page(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert "CryptoScope" in response.text
        assert 'data-market="br"' in response.text


@pytest.mark.asyncio
async def test_onboarding_page(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/onboarding")
        assert response.status_code == 200
