"""Tests for database layer."""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import get_connection, fetch_prices, fetch_pairs, fetch_favorites, toggle_favorite, db_status


@pytest.mark.asyncio
async def test_fetch_prices(temp_db):
    async with get_connection(temp_db) as conn:
        df = await fetch_prices(conn, market="crypto")
        assert len(df) >= 2
        assert "ticker" in df.columns
        assert "close" in df.columns
        assert "BTC/USD" in df["ticker"].values


@pytest.mark.asyncio
async def test_fetch_pairs(temp_db):
    async with get_connection(temp_db) as conn:
        df = await fetch_pairs(conn, market="crypto", min_corr=0.5)
        assert len(df) >= 1
        assert "ticker_a" in df.columns
        assert "corr" in df.columns


@pytest.mark.asyncio
async def test_toggle_favorite_add_remove(temp_db):
    async with get_connection(temp_db) as conn:
        # Add
        result = await toggle_favorite(
            conn, "BTC.USD_ETH.USD", "BTC/USD", "ETH/USD",
            signal="Шорт BTC / Лонг ETH", signal_type="short_a",
            z_at_entry=2.3, price_a_entry=45000, price_b_entry=2300,
            halflife=30, corr=0.85
        )
        assert result["action"] == "added"
        
        # Verify
        favs = await fetch_favorites(conn)
        assert len(favs) >= 1
        
        # Remove (toggle again)
        result = await toggle_favorite(
            conn, "BTC.USD_ETH.USD", "BTC/USD", "ETH/USD"
        )
        assert result["action"] == "removed"


@pytest.mark.asyncio
async def test_db_status(temp_db):
    async with get_connection(temp_db) as conn:
        status = await db_status(conn)
        assert status["n_tickers"] >= 2
        assert status["n_rows"] >= 30
        assert status["n_pairs"] >= 1
        assert isinstance(status["n_active_signals"], int)
