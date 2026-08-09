import pandas as pd
import pytest

from app.core.equity_short_term_lab import (
    HOLD_SESSIONS,
    MARKETS,
    generate_candidates,
    simulate,
)


def _daily_frame(days=36, tickers=6):
    dates = pd.date_range("2026-01-05", periods=days, freq="B", tz="UTC")
    rows = []
    for ticker_index in range(tickers):
        ticker = f"T{ticker_index:02d}"
        drift = (ticker_index - (tickers - 1) / 2) * 0.002
        price = 100.0
        for day_index, date in enumerate(dates):
            price *= 1 + drift + day_index * 0.00001
            rows.append({
                "ticker": ticker,
                "date": date.strftime("%Y-%m-%d"),
                "close": price,
                "volume": 1_000_000 + ticker_index * 100_000,
                "time": date,
                "time_ms": int(date.timestamp() * 1000),
            })
    return pd.DataFrame(rows)


def test_equity_simulation_enters_next_session_and_holds_five_more_sessions():
    frame = _daily_frame(days=9, tickers=1)
    ticker_frame = frame[frame["ticker"] == "T00"].reset_index(drop=True)
    candidate = {
        "strategy": "daily_momentum",
        "ticker": "T00",
        "direction": "long",
        "signal_time": int(ticker_frame.iloc[0]["time_ms"]),
        "signal_price": float(ticker_frame.iloc[0]["close"]),
        "score": 2.0,
        "confidence": "high",
        "timeframe_minutes": 1440,
        "hold_minutes": HOLD_SESSIONS * 1440,
        "stop_pct": 0.0,
        "target_pct": 0.0,
    }

    trade = simulate([candidate], frame, "ru")[0]

    assert trade["entry_time"] == int(ticker_frame.iloc[1]["time_ms"])
    assert trade["entry_price"] == pytest.approx(ticker_frame.iloc[1]["close"])
    assert trade["exit_time"] == int(ticker_frame.iloc[6]["time_ms"])
    assert trade["exit_price"] == pytest.approx(ticker_frame.iloc[6]["close"])
    assert trade["cost_pct"] == MARKETS["ru"]["cost_pct"]


def test_future_daily_close_does_not_change_past_candidates():
    base = _daily_frame()
    base_end = int(base["time_ms"].max())
    before = [
        item for item in generate_candidates(base, "ru")
        if item["signal_time"] <= base_end
    ]
    future_date = pd.Timestamp("2026-03-31", tz="UTC")
    future = []
    for ticker_index, ticker in enumerate(sorted(base["ticker"].unique())):
        future.append({
            "ticker": ticker,
            "date": future_date.strftime("%Y-%m-%d"),
            "close": 1.0 if ticker_index % 2 else 10_000.0,
            "volume": 50_000_000,
            "time": future_date,
            "time_ms": int(future_date.timestamp() * 1000),
        })
    after = [
        item for item in generate_candidates(
            pd.concat([base, pd.DataFrame(future)], ignore_index=True),
            "ru",
        )
        if item["signal_time"] <= base_end
    ]

    assert after == before


def test_ru_and_stocks_use_separate_forward_journal_versions():
    assert MARKETS["ru"]["version"] != MARKETS["stocks"]["version"]
    assert MARKETS["ru"]["source"] == "MOEX ISS"
    assert MARKETS["stocks"]["source"] == "Yahoo Finance"
