import numpy as np
import pandas as pd

from app.core.reversal_lab import ROUND_TRIP_COST_PCT, backtest_reversal


def _synthetic_candles() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    size = 340
    timestamps = np.arange(size, dtype=np.int64) * 300_000 + 1_700_000_000_000
    btc_returns = rng.normal(0, 0.0004, size)
    alt_returns = btc_returns + rng.normal(0, 0.0007, size)
    volumes = np.full(size, 100.0)

    # A large sell impulse, then a confirming bounce and target follow-through.
    alt_returns[305] = -0.05
    alt_returns[306] = 0.01
    alt_returns[307] = 0.02
    volumes[305] = 1000.0

    btc = 100 * np.cumprod(1 + btc_returns)
    alt = 10 * np.cumprod(1 + alt_returns)
    rows = []
    for ticker, prices in (("BTC/USD", btc), ("ALT/USD", alt)):
        for index, timestamp in enumerate(timestamps):
            rows.append({
                "ticker": ticker,
                "open_time": int(timestamp),
                "close": float(prices[index]),
                "volume": float(volumes[index] if ticker == "ALT/USD" else 100.0),
            })
    return pd.DataFrame(rows)


def test_reversal_requires_confirmation_and_deducts_costs():
    trades, metrics = backtest_reversal(_synthetic_candles())
    alt_trades = [trade for trade in trades if trade["ticker"] == "ALT/USD"]

    assert alt_trades
    trade = alt_trades[0]
    assert trade["direction"] == "long"
    assert trade["entry_time"] > trade["shock_time"]
    assert trade["exit_time"] > trade["entry_time"]
    assert trade["net_return_pct"] == (
        trade["gross_return_pct"] - ROUND_TRIP_COST_PCT
    )
    assert metrics["trades"] >= 1
