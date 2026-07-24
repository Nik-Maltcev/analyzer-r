import aiosqlite
import pandas as pd
import pytest

from app.core.scanner_history import (
    annotate_scanner_results,
    auto_close_crypto_positions,
    is_scanner_signal_within_horizon,
    sync_scanner_periods,
)


@pytest.mark.asyncio
async def test_scanner_signal_period_closes_and_restarts(temp_db):
    signal = {
        "signal_key": "SPY",
        "ticker_a": "SPY",
        "ticker_b": "",
        "direction": "long",
        "confidence": "Высокая",
    }

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row

        periods = await sync_scanner_periods(
            conn, "stocks", "momentum", "2026-07-09", [signal]
        )
        assert periods["SPY"]["observation_count"] == 1
        assert periods["SPY"]["confidence"] == "Высокая"

        periods = await sync_scanner_periods(
            conn, "stocks", "momentum", "2026-07-09", [signal]
        )
        assert periods["SPY"]["observation_count"] == 1

        changed_confidence = {**signal, "confidence": "Средняя"}
        periods = await sync_scanner_periods(
            conn, "stocks", "momentum", "2026-07-10", [changed_confidence]
        )
        assert periods["SPY"]["confidence"] == "Высокая"
        annotated = annotate_scanner_results(
            [{"ticker": "SPY"}], "momentum", periods
        )
        assert annotated[0]["signal_age_days"] == 2
        assert annotated[0]["signal_remaining_days"] == 3
        assert annotated[0]["signal_first_seen_date"] == "09.07.2026"
        assert annotated[0]["signal_within_horizon"] is True

        periods = await sync_scanner_periods(
            conn, "stocks", "momentum", "2026-07-11", []
        )
        assert periods == {}

        periods = await sync_scanner_periods(
            conn, "stocks", "momentum", "2026-07-12", [signal]
        )
        assert periods["SPY"]["observation_count"] == 1
        assert periods["SPY"]["first_seen_date"] == "2026-07-12"

        cursor = await conn.execute(
            """
            SELECT status, ended_date
            FROM scanner_signal_periods
            WHERE market = 'stocks' AND scanner = 'momentum'
              AND signal_key = 'SPY'
            ORDER BY first_seen_date
            """
        )
        history = [tuple(row) for row in await cursor.fetchall()]
        assert history == [
            ("closed", "2026-07-11"),
            ("active", None),
        ]


def test_scanner_signal_horizon_expires_without_closing_raw_period():
    assert is_scanner_signal_within_horizon("momentum", 5) is True
    assert is_scanner_signal_within_horizon("momentum", 6) is False
    assert is_scanner_signal_within_horizon("drawdown", 10) is True
    assert is_scanner_signal_within_horizon("drawdown", 11) is False


@pytest.mark.asyncio
async def test_crypto_auto_close_suppresses_signal_until_it_disappears(temp_db):
    signal = {
        "signal_key": "XMR/USD",
        "ticker_a": "XMR/USD",
        "ticker_b": "",
        "direction": "long",
        "confidence": "Высокая",
    }
    prices = pd.DataFrame(
        {"XMR/USD": [100.0, 131.0]},
        index=["2026-07-23", "2026-07-24"],
    )

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row
        await sync_scanner_periods(
            conn, "crypto", "momentum", "2026-07-23", [signal]
        )

        closed = await auto_close_crypto_positions(
            conn,
            prices,
            "2026-07-24",
        )
        assert closed == [{
            "ticker": "XMR/USD",
            "daily_change_pct": 31.0,
            "close_price": 131.0,
        }]

        periods = await sync_scanner_periods(
            conn, "crypto", "momentum", "2026-07-24", [signal]
        )
        assert periods["XMR/USD"]["status"] == "suppressed"
        annotated = annotate_scanner_results(
            [{"ticker": "XMR/USD"}],
            "momentum",
            periods,
        )
        assert annotated[0]["signal_suppressed"] is True

        periods = await sync_scanner_periods(
            conn, "crypto", "momentum", "2026-07-25", []
        )
        assert periods == {}

        periods = await sync_scanner_periods(
            conn, "crypto", "momentum", "2026-07-26", [signal]
        )
        assert periods["XMR/USD"]["status"] == "active"
        assert periods["XMR/USD"]["first_seen_date"] == "2026-07-26"

        cursor = await conn.execute(
            """
            SELECT status, ended_date, close_reason, closed_price
            FROM scanner_signal_periods
            WHERE market = 'crypto' AND scanner = 'momentum'
              AND signal_key = 'XMR/USD'
            ORDER BY first_seen_date
            """
        )
        history = [tuple(row) for row in await cursor.fetchall()]
        assert history == [
            ("closed", "2026-07-24", "auto_30_daily", 131.0),
            ("active", None, None, None),
        ]


@pytest.mark.asyncio
async def test_crypto_auto_close_does_not_rewrite_completed_horizon(temp_db):
    signal = {
        "signal_key": "LTC/USD",
        "ticker_a": "LTC/USD",
        "ticker_b": "",
        "direction": "long",
        "confidence": "Средняя",
    }
    prices = pd.DataFrame(
        {"LTC/USD": [100.0, 131.0]},
        index=["2026-07-23", "2026-07-24"],
    )

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row
        for day in range(19, 24):
            await sync_scanner_periods(
                conn,
                "crypto",
                "momentum",
                f"2026-07-{day:02d}",
                [signal],
            )

        closed = await auto_close_crypto_positions(
            conn,
            prices,
            "2026-07-24",
        )
        assert closed == []

        cursor = await conn.execute(
            """
            SELECT status, observation_count, close_reason
            FROM scanner_signal_periods
            WHERE market = 'crypto' AND scanner = 'momentum'
              AND signal_key = 'LTC/USD'
            """
        )
        row = await cursor.fetchone()
        assert tuple(row) == ("active", 5, None)
