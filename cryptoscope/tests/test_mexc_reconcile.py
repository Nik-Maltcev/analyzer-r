"""Тесты свёрки MEXC-цен с Binance (крипто)."""

from __future__ import annotations

import asyncio

import pandas as pd

import app.data.binance_history as binance_history
from app.data.mexc import reconcile_mexc_with_binance


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"ticker": "BTC/USD", "date": "2026-10-06", "close": 100.0,
             "volume": 1.0, "market": "crypto", "provider": "mexc"},
            {"ticker": "EGLD/USD", "date": "2026-10-06", "close": 50.0,
             "volume": 1.0, "market": "crypto", "provider": "mexc"},
            {"ticker": "ONE/USD", "date": "2026-10-06", "close": 0.002,
             "volume": 1.0, "market": "crypto", "provider": "mexc"},
            {"ticker": "NOTLISTED/USD", "date": "2026-10-06", "close": 7.0,
             "volume": 1.0, "market": "crypto", "provider": "mexc"},
        ]
    )


def _patch_binance(mapping: dict[str, dict[str, float]], *, fail: bool = False):
    async def fake(client, symbol, start_date, end_date):
        if fail:
            raise RuntimeError("binance down")
        closes = mapping.get(symbol)
        if closes is None:
            raise RuntimeError(f"invalid symbol {symbol}")
        rows = [{"date": d, "close": c} for d, c in sorted(closes.items())]
        return pd.DataFrame(rows)

    return fake


def test_reconcile_overrides_only_divergent_rows(monkeypatch):
    mapping = {
        "BTCUSDT": {"2026-10-06": 100.0},        # identical -> untouched
        "EGLDUSDT": {"2026-10-06": 40.0},         # -20% -> override
        "ONEUSDT": {"2026-10-06": 0.00201},       # +0.5% -> untouched
        # NOTLISTEDUSD absent -> fetch raises -> skipped
    }
    monkeypatch.setattr(
        binance_history, "fetch_binance_daily_klines", _patch_binance(mapping)
    )

    frame, report = asyncio.run(reconcile_mexc_with_binance(_frame()))

    by_ticker = {r.ticker: r.close for r in frame.itertuples(index=False)}
    assert by_ticker["EGLD/USD"] == 40.0
    assert by_ticker["BTC/USD"] == 100.0
    assert by_ticker["ONE/USD"] == 0.002
    assert by_ticker["NOTLISTED/USD"] == 7.0
    assert report["corrected"] == 1
    assert report["skipped_tickers"] == 1


def test_reconcile_fail_safe_when_binance_down(monkeypatch):
    monkeypatch.setattr(
        binance_history,
        "fetch_binance_daily_klines",
        _patch_binance({}, fail=True),
    )

    frame, report = asyncio.run(reconcile_mexc_with_binance(_frame()))

    by_ticker = {r.ticker: r.close for r in frame.itertuples(index=False)}
    assert by_ticker["EGLD/USD"] == 50.0
    assert by_ticker["BTC/USD"] == 100.0
    assert report["corrected"] == 0


def test_reconcile_empty_frame_is_noop():
    frame, report = asyncio.run(reconcile_mexc_with_binance(pd.DataFrame()))
    assert frame.empty
    assert report["corrected"] == 0
