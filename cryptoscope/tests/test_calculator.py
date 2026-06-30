"""Tests for P&L calculator."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.calculator import (
    calc_pair_performance,
    calc_signal_pnl,
    compute_position_details,
)


class TestPairPerformance:
    def test_equal_notional_net_pnl_includes_round_trip_fees(self):
        result = calc_pair_performance(
            "long_a",
            entry_a=0.574,
            entry_b=0.0616,
            price_a_now=0.568,
            price_b_now=0.0576,
            capital=1000,
            leverage=1,
            taker_fee_pct=0.02,
            funding_rate_8h_pct=0.01,
            hold_days=0,
        )

        assert result["leg_a_pnl_pct"] == -1.0453
        assert result["leg_b_pnl_pct"] == 6.4935
        assert result["pair_move_pct"] == 5.4482
        assert result["gross_pnl"] == 27.24
        assert result["commissions"] == 0.4
        assert result["net_pnl"] == 26.84
        assert result["net_return_pct"] == 2.6841

    def test_incomplete_prices_do_not_invent_pnl(self):
        result = calc_pair_performance(
            "long_a",
            entry_a=100,
            entry_b=50,
            price_a_now=0,
            price_b_now=48,
        )

        assert result == {"complete": False}


class TestCalcSignalPnl:
    def test_basic_calculation(self):
        result = calc_signal_pnl(
            signal_info={"spread_sd_pct": 0.05},
            capital=1000, leverage=3, taker_fee_pct=0.02, funding_rate_8h_pct=0.01,
            hold_days=5, avg_pnl_z=2.0
        )
        assert result["capital"] == 1000
        assert result["leverage"] == 3
        assert result["position_size"] == 3000
        assert result["commissions"] > 0
        assert result["funding_cost"] > 0
        assert result["total_cost"] > 0
        assert "gross_pnl" in result
        assert "net_pnl" in result

    def test_position_size_scales_with_leverage(self):
        r1 = calc_signal_pnl({"spread_sd_pct": 0.05}, capital=1000, leverage=1)
        r2 = calc_signal_pnl({"spread_sd_pct": 0.05}, capital=1000, leverage=10)
        assert r2["position_size"] == 10 * r1["position_size"]

    def test_commissions_scale_with_fee(self):
        r1 = calc_signal_pnl({"spread_sd_pct": 0.05}, capital=1000, leverage=3, taker_fee_pct=0.02)
        r2 = calc_signal_pnl({"spread_sd_pct": 0.05}, capital=1000, leverage=3, taker_fee_pct=0.04)
        assert r2["commissions"] == 2 * r1["commissions"]

    def test_funding_scales_with_days(self):
        r1 = calc_signal_pnl({"spread_sd_pct": 0.05}, capital=1000, leverage=3, hold_days=5)
        r2 = calc_signal_pnl({"spread_sd_pct": 0.05}, capital=1000, leverage=3, hold_days=10)
        assert r2["funding_cost"] == 2 * r1["funding_cost"]

    def test_output_types(self):
        result = calc_signal_pnl({"spread_sd_pct": 0.05})
        assert isinstance(result["capital"], float)
        assert isinstance(result["position_size"], float)
        assert isinstance(result["commissions"], float)
        assert isinstance(result["hold_days"], int)
        assert isinstance(result["risk_reward"], float)

    def test_zero_fee_no_commission(self):
        result = calc_signal_pnl({"spread_sd_pct": 0.05}, taker_fee_pct=0, funding_rate_8h_pct=0)
        assert result["commissions"] == 0
        assert result["funding_cost"] == 0

    def test_risk_reward_is_positive(self):
        result = calc_signal_pnl({"spread_sd_pct": 0.05}, avg_pnl_z=3.0)
        assert result["risk_reward"] > 0

    def test_higher_z_move_gives_higher_gross_pnl(self):
        r1 = calc_signal_pnl({"spread_sd_pct": 0.05}, avg_pnl_z=1.0)
        r2 = calc_signal_pnl({"spread_sd_pct": 0.05}, avg_pnl_z=3.0)
        assert r2["gross_pnl"] > r1["gross_pnl"]


class TestComputePositionDetails:
    def test_basic_computation(self):
        pair = {"halflife": 30}
        result = compute_position_details(pair, capital=1000, leverage=3)
        assert result["position_size"] == 3000
        assert result["capital"] == 1000
        assert result["leverage"] == 3
        assert result["commission_total"] > 0
        assert result["funding_total"] > 0

    def test_no_halflife_defaults(self):
        pair = {}
        result = compute_position_details(pair)
        assert result["estimated_days"] == 30

    def test_output_types(self):
        result = compute_position_details({})
        assert isinstance(result["position_size"], float)
        assert isinstance(result["total_cost"], float)
        assert isinstance(result["estimated_days"], int)
