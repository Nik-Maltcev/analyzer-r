"""Tests for backtest engine."""

import sys
import os
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.backtest import (
    attach_pair_returns,
    backtest_stats,
    compute_spread_sd_pct,
    run_backtest,
)


class TestRunBacktest:
    def test_generates_trades_when_z_crosses_threshold(self):
        np.random.seed(42)
        n = 500
        z = np.zeros(n)
        z[0] = 0.0
        for i in range(1, n):
            z[i] = 0.95 * z[i-1] + np.random.randn() * 0.1
        z[100] = 2.5
        z[120] = 0.3
        
        trades = run_backtest(z)
        assert len(trades) >= 1
        assert "entry_z" in trades.columns
        assert "exit_z" in trades.columns
        assert "pnl_sigma" in trades.columns
        assert "days" in trades.columns

    def test_no_trades_when_within_band(self):
        z = np.random.randn(200) * 0.5  # All within ±1
        trades = run_backtest(z)
        assert len(trades) == 0

    def test_stop_loss_triggers(self):
        z = np.array([0.0, 0.5, 2.5, 3.0, 3.8, 4.0])
        trades = run_backtest(z, stop_threshold=3.5)
        assert len(trades) >= 1
        assert trades.iloc[0]["days"] <= 3

    def test_long_entry_works(self):
        z = np.array([0.0, -1.0, -2.5, -2.3, -0.5, -0.2])
        trades = run_backtest(z)
        assert len(trades) >= 1
        assert trades.iloc[0]["type"] == "long"

    def test_short_entry_works(self):
        z = np.array([0.0, 1.0, 2.5, 2.3, 0.5, 0.2])
        trades = run_backtest(z)
        assert len(trades) >= 1
        assert trades.iloc[0]["type"] == "short"

    def test_short_mean_reversion_is_profitable(self):
        trades = run_backtest(np.array([0.0, 2.5, 2.4, 0.3, 0.2]))

        assert np.isclose(trades.iloc[0]["pnl_sigma"], 2.2)

    def test_long_mean_reversion_is_profitable(self):
        trades = run_backtest(np.array([0.0, -2.5, -2.4, -0.3, -0.2]))

        assert np.isclose(trades.iloc[0]["pnl_sigma"], 2.2)

    def test_crossing_zero_keeps_directional_profit(self):
        short = run_backtest(np.array([0.0, 2.5, 2.4, -0.4, -0.5]))
        long = run_backtest(np.array([0.0, -2.5, -2.4, 0.4, 0.5]))

        assert short.iloc[0]["pnl_sigma"] == 2.9
        assert long.iloc[0]["pnl_sigma"] == 2.9

    def test_trades_dataframe_has_correct_columns(self):
        z = np.array([0.0, 2.5, 2.4, 0.3, 0.2])
        trades = run_backtest(z)
        expected_cols = ["entry_idx", "exit_idx", "entry_z", "exit_z", "pnl_sigma", "days", "type"]
        for col in expected_cols:
            assert col in trades.columns

    def test_handles_nan_in_series(self):
        z = np.array([0.0, np.nan, 2.5, 2.4, 0.3, np.nan, 0.2])
        trades = run_backtest(z)
        assert len(trades) >= 1  # Should skip NaN and still find trade

    def test_never_executes_at_the_signal_close(self):
        trades = run_backtest(
            np.array([0.0, 2.5, 2.2, 0.4, 0.1]),
        )

        trade = trades.iloc[0]
        assert trade["entry_signal_idx"] == 1
        assert trade["entry_idx"] == 2
        assert trade["exit_signal_idx"] == 3
        assert trade["exit_idx"] == 4
        assert trade["entry_z"] == 2.2
        assert trade["exit_z"] == 0.1


class TestBacktestStats:
    def test_empty_trades(self):
        trades = pd.DataFrame(columns=["entry_z", "exit_z", "pnl_sigma", "days", "type"])
        stats = backtest_stats(trades)
        assert stats["n_trades"] == 0
        assert stats["has_history"] is False

    def test_stats_for_trades(self):
        trades = pd.DataFrame([
            {"entry_z": 2.5, "exit_z": 0.3, "pnl_sigma": 2.2, "days": 5, "type": "short"},
            {"entry_z": -2.3, "exit_z": -0.5, "pnl_sigma": 1.8, "days": 7, "type": "long"},
            {"entry_z": 2.1, "exit_z": 2.8, "pnl_sigma": -0.7, "days": 3, "type": "short"},
        ])
        stats = backtest_stats(trades, spread_sd_pct=0.05)
        assert stats["n_trades"] == 3
        assert stats["has_history"] is True
        assert 0 <= stats["win_rate"] <= 100
        assert stats["avg_hold"] > 0

    def test_all_wins_gives_100_win_rate(self):
        trades = pd.DataFrame([
            {"entry_z": 2.5, "exit_z": 0.3, "pnl_sigma": 2.2, "days": 5, "type": "short"},
            {"entry_z": -2.3, "exit_z": -0.5, "pnl_sigma": 1.8, "days": 7, "type": "long"},
        ])
        stats = backtest_stats(trades)
        assert stats["win_rate"] == 100.0

    def test_exact_pair_returns_override_sigma_approximation(self):
        trades = pd.DataFrame(
            [{
                "entry_idx": 0,
                "exit_idx": 1,
                "pnl_sigma": 9.0,
                "days": 1,
                "type": "long",
            }]
        )
        priced = attach_pair_returns(
            trades,
            np.array([100.0, 110.0]),
            np.array([200.0, 190.0]),
            hedge_ratio=1.0,
        )

        stats = backtest_stats(priced, spread_sd_pct=0.50)

        assert np.isclose(priced.iloc[0]["return_pct"], 7.5)
        assert np.isclose(stats["avg_pnl_pct"], 7.5)
        assert stats["win_rate"] == 100.0

    def test_exact_pair_returns_support_negative_hedge_ratio(self):
        trades = pd.DataFrame(
            [{
                "entry_idx": 0,
                "exit_idx": 1,
                "pnl_sigma": 1.0,
                "days": 1,
                "type": "long",
            }]
        )
        priced = attach_pair_returns(
            trades,
            np.array([100.0, 110.0]),
            np.array([200.0, 220.0]),
            hedge_ratio=-1.0,
        )

        assert np.isclose(priced.iloc[0]["return_pct"], 10.0)


class TestSpreadSD:
    def test_basic_computation(self, sample_prices):
        pa, pb = sample_prices
        sd = compute_spread_sd_pct(pa, pb, hedge_ratio=0.5)
        assert sd > 0
        assert isinstance(sd, float)

    def test_short_series_has_no_invented_default(self):
        pa = np.array([100.0] * 10)
        pb = np.array([50.0] * 10)
        sd = compute_spread_sd_pct(pa, pb, hedge_ratio=1.0)
        assert sd is None
