from datetime import UTC, datetime, timedelta
import sqlite3

import pandas as pd
import pytest

from app.data.mexc import (
    LEGACY_PROVIDER,
    activate_mexc_prices,
    archive_legacy_crypto_prices,
    normalize_mexc_symbol,
    rollback_crypto_provider,
    upsert_mexc_price_versions,
)
from app.db.schema import CREATE_PRICES


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(CREATE_PRICES)
    conn.executemany(
        """
        INSERT INTO prices (ticker, date, close, volume, market)
        VALUES (?, ?, ?, ?, 'crypto')
        """,
        [
            ("BTC/USD", "2026-01-01", 100.0, 1.0),
            ("ETH/USD", "2026-01-01", 10.0, 1.0),
        ],
    )
    conn.commit()
    return conn


def _mexc_frame(tickers: list[str], days: int = 4) -> pd.DataFrame:
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    rows = []
    for index, ticker in enumerate(tickers):
        for offset in range(days):
            rows.append({
                "ticker": ticker,
                "date": (yesterday - timedelta(days=offset)).isoformat(),
                "close": 200.0 + index + offset,
                "volume": 2.0,
                "market": "crypto",
                "provider": "mexc",
            })
    return pd.DataFrame(rows)


def test_normalize_mexc_symbol_uses_usdt_spot_quote():
    assert normalize_mexc_symbol("BTC/USD") == "BTCUSDT"
    assert normalize_mexc_symbol("eth/usdt") == "ETHUSDT"
    assert normalize_mexc_symbol("BTC") is None


def test_mexc_activation_is_atomic_and_legacy_can_be_restored():
    conn = _connection()
    try:
        assert archive_legacy_crypto_prices(conn) == 2
        upsert_mexc_price_versions(
            conn,
            _mexc_frame(["BTC/USD", "ETH/USD"]),
        )

        result = activate_mexc_prices(
            conn,
            ["BTC/USD", "ETH/USD"],
            minimum_history_rows=3,
        )

        assert result["provider"] == "mexc"
        assert result["tickers"] == 2
        providers = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT provider FROM prices"
            ).fetchall()
        }
        assert providers == {"mexc"}

        rollback = rollback_crypto_provider(conn, LEGACY_PROVIDER)
        assert rollback["provider"] == LEGACY_PROVIDER
        restored = conn.execute(
            """
            SELECT ticker, close
            FROM prices
            ORDER BY ticker
            """
        ).fetchall()
        assert restored == [("BTC/USD", 100.0), ("ETH/USD", 10.0)]
    finally:
        conn.close()


def test_rejected_mexc_cutover_does_not_touch_active_prices():
    conn = _connection()
    try:
        archive_legacy_crypto_prices(conn)
        upsert_mexc_price_versions(conn, _mexc_frame(["BTC/USD"]))

        with pytest.raises(RuntimeError, match="coverage"):
            activate_mexc_prices(
                conn,
                ["BTC/USD", "ETH/USD"],
                minimum_history_rows=3,
            )

        active = conn.execute(
            """
            SELECT ticker, close
            FROM prices
            ORDER BY ticker
            """
        ).fetchall()
        assert active == [("BTC/USD", 100.0), ("ETH/USD", 10.0)]
    finally:
        conn.close()
