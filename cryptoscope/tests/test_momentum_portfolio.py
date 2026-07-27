import asyncio
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from app.core.momentum_portfolio import (
    apply_momentum_live_prices,
    fetch_momentum_portfolio_report,
    sync_momentum_portfolio_journal,
)


SIGNALS = [
    {
        "ticker": "ETH/USD",
        "recommendation_class": "long",
        "confidence": "Высокая",
        "momentum_score": 14.0,
    },
    {
        "ticker": "SOL/USD",
        "recommendation_class": "long",
        "confidence": "Высокая",
        "momentum_score": 12.0,
    },
    {
        "ticker": "LTC/USD",
        "recommendation_class": "long",
        "confidence": "Средняя",
        "momentum_score": 9.0,
    },
]


async def _fake_candidates(_conn, _wide):
    return SIGNALS


def _seed_prices(db_path: Path, days: int = 61) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE prices (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            market TEXT,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    start = date(2026, 1, 1)
    tickers = {
        "BTC/USD": 100.0,
        "ETH/USD": 50.0,
        "SOL/USD": 40.0,
        "LTC/USD": 30.0,
        "XRP/USD": 20.0,
    }
    for day in range(days):
        current_date = (start + timedelta(days=day)).isoformat()
        for index, (ticker, initial) in enumerate(tickers.items()):
            close = initial * (1 + 0.008 + index * 0.001) ** day
            conn.execute(
                """
                INSERT INTO prices(ticker, date, close, market)
                VALUES (?, ?, ?, 'crypto')
                """,
                (ticker, current_date, close),
            )
    conn.commit()
    conn.close()


def test_momentum_portfolio_journal_freezes_completed_daily_run():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "market.db"
            _seed_prices(db_path)
            with patch(
                "app.core.momentum_portfolio._momentum_candidates",
                _fake_candidates,
            ):
                first = await sync_momentum_portfolio_journal(str(db_path))
                assert first["created"] is True

                conn = sqlite3.connect(db_path)
                previous_date = date(2026, 3, 2)
                next_date = previous_date + timedelta(days=1)
                for ticker, previous_close in conn.execute(
                    """
                    SELECT ticker, close
                    FROM prices
                    WHERE market = 'crypto' AND date = ?
                    """,
                    (previous_date.isoformat(),),
                ):
                    conn.execute(
                        """
                        INSERT INTO prices(ticker, date, close, market)
                        VALUES (?, ?, ?, 'crypto')
                        """,
                        (ticker, next_date.isoformat(), previous_close * 1.02),
                    )
                conn.commit()
                conn.close()

                second = await sync_momentum_portfolio_journal(str(db_path))
                assert second["finalized"] == 1

                async with aiosqlite.connect(db_path) as report_conn:
                    report_conn.row_factory = aiosqlite.Row
                    report = await fetch_momentum_portfolio_report(report_conn)
                assert report["completed_total"] == 1
                assert report["positive_total"] == 1
                frozen_cash = report["cumulative_cash"]

                third = await sync_momentum_portfolio_journal(str(db_path))
                assert third["created"] is False
                async with aiosqlite.connect(db_path) as report_conn:
                    report_conn.row_factory = aiosqlite.Row
                    repeated = await fetch_momentum_portfolio_report(
                        report_conn
                    )
                assert repeated["cumulative_cash"] == frozen_cash

    asyncio.run(scenario())


def test_momentum_portfolio_report_includes_active_mark_and_total():
    report = {
        "current": {
            "capital": 300.0,
            "finalized_on": None,
            "allocations": [
                {
                    "ticker": "ETH/USD",
                    "allocation": 180.0,
                    "entry_price": 100.0,
                },
                {
                    "ticker": "SOL/USD",
                    "allocation": 120.0,
                    "entry_price": 50.0,
                },
            ],
        },
        "cumulative_cash": 12.0,
        "compounded_return_pct": 4.0,
    }

    marked = apply_momentum_live_prices(
        report,
        {
            "ETH/USD": 110.0,
            "SOL/USD": 45.0,
        },
    )

    assert marked["active_positions_total"] == 2
    assert marked["active_cash"] == 6.0
    assert marked["total_cash"] == 18.0
    assert marked["active_return_pct"] == 2.0
    assert marked["total_return_pct"] == 6.08
