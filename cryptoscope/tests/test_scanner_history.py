from datetime import UTC, datetime

import aiosqlite
import pandas as pd
import pytest

from app.core.scanner_history import (
    annotate_scanner_results,
    auto_close_crypto_positions,
    ensure_scanner_history_schema,
    is_scanner_signal_within_horizon,
    snapshot_crypto_strategy_positions,
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


@pytest.mark.asyncio
async def test_crypto_strategy_admission_is_persisted_on_confidence_upgrade(
    temp_db,
):
    low_signal = {
        "signal_key": "TEST/USD",
        "ticker_a": "TEST/USD",
        "ticker_b": "",
        "direction": "long",
        "confidence": "Низкая",
    }

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row
        periods = await sync_scanner_periods(
            conn,
            "crypto",
            "momentum",
            "2026-07-20",
            [low_signal],
        )
        assert periods["TEST/USD"]["strategy_admitted_date"] is None

        medium_signal = {**low_signal, "confidence": "Средняя"}
        periods = await sync_scanner_periods(
            conn,
            "crypto",
            "momentum",
            "2026-07-21",
            [medium_signal],
        )
        assert (
            periods["TEST/USD"]["strategy_admitted_date"]
            == "2026-07-21"
        )
        assert periods["TEST/USD"]["strategy_confidence"] == "Средняя"

        high_signal = {**low_signal, "confidence": "Высокая"}
        periods = await sync_scanner_periods(
            conn,
            "crypto",
            "momentum",
            "2026-07-22",
            [high_signal],
        )
        assert (
            periods["TEST/USD"]["strategy_admitted_date"]
            == "2026-07-21"
        )
        assert periods["TEST/USD"]["strategy_confidence"] == "Средняя"


@pytest.mark.asyncio
async def test_schema_reopens_legacy_same_day_auto_close(temp_db):
    signal = {
        "signal_key": "SHIB/USD",
        "ticker_a": "SHIB/USD",
        "ticker_b": "",
        "direction": "long",
        "confidence": "Высокая",
    }

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row
        await sync_scanner_periods(
            conn,
            "crypto",
            "momentum",
            "2026-07-26",
            [signal],
        )
        await conn.execute(
            """
            UPDATE scanner_signal_periods
            SET status = 'suppressed',
                ended_date = '2026-07-26',
                close_reason = 'auto_30_daily',
                closed_price = 0.00000565,
                closed_at = datetime('now')
            WHERE signal_key = 'SHIB/USD'
            """
        )
        await conn.commit()

        await ensure_scanner_history_schema(conn)

        cursor = await conn.execute(
            """
            SELECT status, ended_date, close_reason, closed_price, closed_at
            FROM scanner_signal_periods
            WHERE signal_key = 'SHIB/USD'
            """
        )
        row = await cursor.fetchone()
        assert tuple(row) == ("active", None, None, None, None)


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
async def test_crypto_auto_close_keeps_signal_born_on_pump_day(temp_db):
    """A signal created by the +30% day itself survives its first day."""
    signal = {
        "signal_key": "SHIB/USD",
        "ticker_a": "SHIB/USD",
        "ticker_b": "",
        "direction": "long",
        "confidence": "Высокая",
    }
    prices = pd.DataFrame(
        {"SHIB/USD": [100.0, 131.0]},
        index=["2026-07-25", "2026-07-26"],
    )

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row
        await sync_scanner_periods(
            conn, "crypto", "momentum", "2026-07-26", [signal]
        )

        closed = await auto_close_crypto_positions(
            conn,
            prices,
            "2026-07-26",
        )
        assert closed == []

        cursor = await conn.execute(
            """
            SELECT status, close_reason
            FROM scanner_signal_periods
            WHERE market = 'crypto' AND scanner = 'momentum'
              AND signal_key = 'SHIB/USD'
            """
        )
        assert tuple(await cursor.fetchone()) == ("active", None)

        # The next +30% day closes the position: it has lived since yesterday.
        prices_next = pd.DataFrame(
            {"SHIB/USD": [100.0, 131.0, 175.0]},
            index=["2026-07-25", "2026-07-26", "2026-07-27"],
        )
        await sync_scanner_periods(
            conn, "crypto", "momentum", "2026-07-27", [signal]
        )
        closed = await auto_close_crypto_positions(
            conn,
            prices_next,
            "2026-07-27",
            evaluated_at=datetime(2026, 7, 28, tzinfo=UTC),
        )
        assert len(closed) == 1
        assert closed[0]["ticker"] == "SHIB/USD"


@pytest.mark.asyncio
async def test_crypto_auto_close_ignores_incomplete_current_day(temp_db):
    signal = {
        "signal_key": "PEPE/USD",
        "ticker_a": "PEPE/USD",
        "ticker_b": "",
        "direction": "long",
        "confidence": "Средняя",
    }
    prices = pd.DataFrame(
        {"PEPE/USD": [100.0, 110.0, 150.0]},
        index=["2026-07-25", "2026-07-26", "2026-07-27"],
    )

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row
        await sync_scanner_periods(
            conn,
            "crypto",
            "momentum",
            "2026-07-25",
            [signal],
            current_prices={"PEPE/USD": 100.0},
        )
        closed = await auto_close_crypto_positions(
            conn,
            prices,
            "2026-07-27",
            evaluated_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        )
        assert closed == []

        closed = await auto_close_crypto_positions(
            conn,
            prices,
            "2026-07-27",
            evaluated_at=datetime(2026, 7, 28, tzinfo=UTC),
        )
        assert len(closed) == 1
        assert closed[0]["close_price"] == 150.0


@pytest.mark.asyncio
async def test_completed_crypto_trade_journal_is_immutable(temp_db):
    signal = {
        "signal_key": "BAL/USD",
        "ticker_a": "BAL/USD",
        "ticker_b": "",
        "direction": "long",
        "confidence": "Высокая",
    }
    dates = [f"2026-07-{day:02d}" for day in range(20, 25)]
    values = [100.0, 102.0, 104.0, 106.0, 120.0]
    wide = pd.DataFrame({"BAL/USD": values}, index=dates)

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row
        for data_date, price in zip(dates, values):
            await conn.execute(
                """
                INSERT OR REPLACE INTO prices (ticker, date, close, market)
                VALUES ('BAL/USD', ?, ?, 'crypto')
                """,
                (data_date, price),
            )
            await sync_scanner_periods(
                conn,
                "crypto",
                "momentum",
                data_date,
                [signal],
                current_prices={"BAL/USD": price},
            )
        await snapshot_crypto_strategy_positions(conn, wide)

        cursor = await conn.execute(
            """
            SELECT opened_on, entry_price, closed_on, exit_price,
                   return_pct, cash_result
            FROM crypto_strategy_trades
            WHERE ticker = 'BAL/USD'
            """
        )
        original = tuple(await cursor.fetchone())

        await conn.execute(
            """
            UPDATE prices
            SET close = close * 10
            WHERE ticker = 'BAL/USD'
            """
        )
        mutated = wide * 10
        await snapshot_crypto_strategy_positions(conn, mutated)
        cursor = await conn.execute(
            """
            SELECT opened_on, entry_price, closed_on, exit_price,
                   return_pct, cash_result
            FROM crypto_strategy_trades
            WHERE ticker = 'BAL/USD'
            """
        )
        assert tuple(await cursor.fetchone()) == original
    assert original[:4] == (
        "2026-07-20",
        100.0,
        "2026-07-24",
        120.0,
    )
    assert original[4] == pytest.approx(20.0)
    assert original[5] == pytest.approx(20.0)


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
