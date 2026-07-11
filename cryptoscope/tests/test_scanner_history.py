import aiosqlite
import pytest

from app.core.scanner_history import (
    annotate_scanner_results,
    sync_scanner_periods,
)


@pytest.mark.asyncio
async def test_scanner_signal_period_closes_and_restarts(temp_db):
    signal = {
        "signal_key": "SPY",
        "ticker_a": "SPY",
        "ticker_b": "",
        "direction": "long",
    }

    async with aiosqlite.connect(temp_db) as conn:
        conn.row_factory = aiosqlite.Row

        periods = await sync_scanner_periods(
            conn, "stocks", "momentum", "2026-07-09", [signal]
        )
        assert periods["SPY"]["observation_count"] == 1

        periods = await sync_scanner_periods(
            conn, "stocks", "momentum", "2026-07-09", [signal]
        )
        assert periods["SPY"]["observation_count"] == 1

        periods = await sync_scanner_periods(
            conn, "stocks", "momentum", "2026-07-10", [signal]
        )
        annotated = annotate_scanner_results(
            [{"ticker": "SPY"}], "momentum", periods
        )
        assert annotated[0]["signal_age_days"] == 2
        assert annotated[0]["signal_remaining_days"] == 3
        assert annotated[0]["signal_first_seen_date"] == "09.07.2026"

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
