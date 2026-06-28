"""Tests for database layer."""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.cointegration import compute_fixed_zscore
from app.db.database import (
    db_status,
    ensure_favorite_z_model,
    fetch_favorites,
    fetch_pairs,
    fetch_prices,
    get_connection,
    toggle_favorite,
)


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
        favorite = favs.iloc[0]
        assert favorite["hedge_ratio_entry"] is not None
        assert favorite["spread_mean_entry"] is not None
        assert favorite["spread_sd_entry"] > 0
        fixed_z = compute_fixed_zscore(
            favorite["price_a_entry"],
            favorite["price_b_entry"],
            favorite["hedge_ratio_entry"],
            favorite["spread_mean_entry"],
            favorite["spread_sd_entry"],
        )
        assert fixed_z == pytest.approx(2.3)
        
        # Remove (toggle again)
        result = await toggle_favorite(
            conn, "BTC.USD_ETH.USD", "BTC/USD", "ETH/USD"
        )
        assert result["action"] == "removed"


@pytest.mark.asyncio
async def test_legacy_favorite_is_anchored_to_its_entry_prices(temp_db):
    async with get_connection(temp_db) as conn:
        cursor = await conn.execute(
            """
            SELECT ticker, close
            FROM prices
            WHERE market = 'crypto'
              AND ticker IN ('BTC/USD', 'ETH/USD')
            ORDER BY date DESC
            """
        )
        latest = {}
        for row in await cursor.fetchall():
            latest.setdefault(row["ticker"], row["close"])

        await conn.execute(
            """
            INSERT INTO favorites (
                pair, market, ticker_a, ticker_b, signal_type, z_at_entry,
                price_a_entry, price_b_entry, entry_time, status, user_id
            )
            VALUES (
                'BTC.USD_ETH.USD', 'crypto', 'BTC/USD', 'ETH/USD',
                'long_a', -2.0, ?, ?, datetime('now'), 'active', 'local'
            )
            """,
            (latest["BTC/USD"], latest["ETH/USD"]),
        )
        await conn.commit()

        favorites = await fetch_favorites(conn)
        model = await ensure_favorite_z_model(conn, favorites.iloc[0])
        refreshed = (await fetch_favorites(conn)).iloc[0]

    assert model
    fixed_z = compute_fixed_zscore(
        refreshed["price_a_entry"],
        refreshed["price_b_entry"],
        refreshed["hedge_ratio_entry"],
        refreshed["spread_mean_entry"],
        refreshed["spread_sd_entry"],
    )
    assert fixed_z == pytest.approx(-2.0)


@pytest.mark.asyncio
async def test_db_status(temp_db):
    async with get_connection(temp_db) as conn:
        status = await db_status(conn)
        assert status["n_tickers"] >= 2
        assert status["n_rows"] >= 30
        assert status["n_pairs"] >= 1
        assert isinstance(status["n_active_signals"], int)
