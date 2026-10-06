"""Тесты forward-журнала TS-моментума (относительная сила к BTC)."""

from __future__ import annotations

import asyncio
import sqlite3

import pandas as pd
import pytest

from app.core.ts_momentum import (
    CALCULATION_VERSION,
    HOLD_DAYS,
    LOOKBACK,
    MIN_UNIVERSE,
    compute_relative_strength_basket,
    ensure_ts_momentum_schema,
    sync_ts_momentum_forward,
)
from app.db.schema import CREATE_PRICES


def _synthetic_prices(days: int) -> pd.DataFrame:
    """BTC + 4 растущих + 4 падающих монеты, ежедневные цены."""
    rows = []
    coins = {
        "BTC/USD": 1.01,
        "UP1/USD": 1.02, "UP2/USD": 1.02, "UP3/USD": 1.02, "UP4/USD": 1.02,
        "DN1/USD": 0.98, "DN2/USD": 0.98, "DN3/USD": 0.98, "DN4/USD": 0.98,
    }
    for t in range(days):
        d = f"2026-01-{t + 1:02d}" if t < 31 else f"2026-02-{t - 30:02d}"
        for ticker, growth in coins.items():
            rows.append({"ticker": ticker, "date": d,
                         "close": 100.0 * (growth ** t)})
    return pd.DataFrame(rows, columns=["ticker", "date", "close"])


# --- чистая функция ----------------------------------------------------------

def test_basket_long_up_short_down():
    prices = _synthetic_prices(LOOKBACK + 1)
    report = compute_relative_strength_basket(prices)
    assert report["status"] == "ok"
    assert report["universe"] == 9
    by_ticker = {leg["ticker"]: leg for leg in report["legs"]}
    assert by_ticker["UP1/USD"]["direction"] == "long"
    assert by_ticker["DN1/USD"]["direction"] == "short"
    # равный вес, сумма = 1
    assert sum(leg["weight"] for leg in report["legs"]) == pytest.approx(1.0)
    assert report["long_legs"] == 5  # BTC + 4 up
    assert report["short_legs"] == 4


def test_basket_insufficient_history():
    prices = _synthetic_prices(LOOKBACK)  # на одну строку меньше минимума
    assert compute_relative_strength_basket(prices)["status"] == "insufficient_history"


def test_basket_no_btc():
    prices = _synthetic_prices(LOOKBACK + 1)
    prices = prices[prices["ticker"] != "BTC/USD"]
    assert compute_relative_strength_basket(prices)["status"] == "no_btc"


def test_basket_insufficient_universe():
    prices = _synthetic_prices(LOOKBACK + 1)
    keep = {"BTC/USD", "UP1/USD", "DN1/USD"}
    prices = prices[prices["ticker"].isin(keep)]
    report = compute_relative_strength_basket(prices)
    assert report["status"] == "insufficient_universe"
    assert report["universe"] < MIN_UNIVERSE


# --- async sync на временной БД ---------------------------------------------

def _seed_db(path: str, prices: pd.DataFrame) -> None:
    conn = sqlite3.connect(path)
    conn.execute(CREATE_PRICES)
    for r in prices.itertuples(index=False):
        conn.execute(
            "INSERT OR REPLACE INTO prices (ticker, date, close, market) "
            "VALUES (?, ?, ?, 'crypto')",
            (r.ticker, r.date, r.close),
        )
    conn.commit()
    conn.close()


def _journal_rows(path: str, status: str | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if status:
        rows = conn.execute(
            "SELECT * FROM ts_momentum_forward_journal "
            "WHERE calculation_version = ? AND status = ? "
            "ORDER BY ticker",
            (CALCULATION_VERSION, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ts_momentum_forward_journal "
            "WHERE calculation_version = ? ORDER BY rebalance_date, ticker",
            (CALCULATION_VERSION,),
        ).fetchall()
    conn.close()
    return list(rows)


def test_sync_opens_then_closes_and_is_idempotent(tmp_path):
    db = str(tmp_path / "test.db")
    # День 0..30 (31 строка) — достаточно для открытия корзины на дне 30.
    _seed_db(db, _synthetic_prices(LOOKBACK + 1))

    first = asyncio.run(sync_ts_momentum_forward(db))
    assert first["status"] == "ok"
    assert first["opened"] == 9
    assert first["closed"] == 0
    active = _journal_rows(db, "active")
    assert len(active) == 9
    open_date = active[0]["rebalance_date"]

    # Идемпотентность: повторный вызов в тот же день не открывает вторую корзину.
    again = asyncio.run(sync_ts_momentum_forward(db))
    assert again["opened"] == 0
    assert len(_journal_rows(db, "active")) == 9

    # Добавляем цены через HOLD_DAYS дней и закрываем корзину.
    full = _synthetic_prices(LOOKBACK + 1 + HOLD_DAYS)
    _seed_db(db, full)
    second = asyncio.run(sync_ts_momentum_forward(db))
    assert second["closed"] == 9
    assert second["opened"] == 9  # открылась новая корзина на свежую дату

    closed = _journal_rows(db, "closed")
    assert len(closed) == 9
    by_ticker = {row["ticker"]: row for row in closed}

    # UP1 (лонг) вырос сильнее BTC -> положительный excess.
    assert by_ticker["UP1/USD"]["return_pct"] > 0
    # DN1 (шорт) упал, BTC вырос -> шорт против BTC в плюсе.
    assert by_ticker["DN1/USD"]["return_pct"] > 0
    # Нога BTC: excess против самой себя = 0.
    assert by_ticker["BTC/USD"]["return_pct"] == pytest.approx(0.0, abs=1e-9)
    # cash_result = weight * STAKE * signed_excess; сумма весов закрытой корзины = 1
    assert sum(row["weight"] for row in closed) == pytest.approx(1.0)
    for row in closed:
        assert row["closed_on"] == second["as_of"]
        assert row["exit_price"] > 0
        assert row["btc_exit_price"] > 0

    # Новая активная корзина открыта на дату, отличную от первой.
    new_active = _journal_rows(db, "active")
    assert len(new_active) == 9
    assert new_active[0]["rebalance_date"] != open_date


def test_sync_no_data(tmp_path):
    db = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db)
    conn.execute(CREATE_PRICES)
    conn.commit()
    conn.close()
    result = asyncio.run(sync_ts_momentum_forward(db))
    assert result["status"] == "no_data"
    assert result["opened"] == 0
