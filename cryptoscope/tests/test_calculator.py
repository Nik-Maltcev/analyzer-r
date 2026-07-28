"""Tests for P&L calculator."""

import sys
import os
import math
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.calculator import (
    calc_pair_performance,
    calc_single_performance,
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
        assert result["spread_move_pp"] == 5.4482
        assert result["pair_move_pct"] == 2.7241
        assert result["unlevered_return_pct"] == 2.7241
        assert result["gross_pnl"] == 27.24
        assert result["commissions"] == 0.4
        assert result["net_pnl"] == 26.84
        assert result["net_return_pct"] == 2.6841
        assert result["leg_a_notional"] + result["leg_b_notional"] == 1000
        assert result["gross_pnl"] == round(
            result["gross_exposure"]
            * result["unlevered_return_pct"]
            / 100,
            2,
        )
        assert result["net_pnl"] == round(
            result["gross_pnl"] - result["total_cost"],
            2,
        )

    def test_incomplete_prices_do_not_invent_pnl(self):
        result = calc_pair_performance(
            "long_a",
            entry_a=100,
            entry_b=50,
            price_a_now=0,
            price_b_now=48,
        )

        assert result["complete"] is False

    def test_negative_hedge_ratio_changes_second_leg_direction(self):
        result = calc_pair_performance(
            "long_a",
            entry_a=100,
            entry_b=100,
            price_a_now=110,
            price_b_now=120,
            capital=1000,
            leverage=1,
            taker_fee_pct=0,
            funding_rate_8h_pct=0,
            hedge_ratio=-2,
        )

        assert result["leg_a_side"] == "Лонг"
        assert result["leg_b_side"] == "Лонг"
        assert result["hedge_ratio"] == -2
        assert result["leg_a_notional"] == 333.33
        assert result["leg_b_notional"] == 666.67
        assert result["unlevered_return_pct"] == 16.6667
        assert result["gross_pnl"] == 166.67

    def test_short_negative_hedge_ratio_shorts_both_legs(self):
        result = calc_pair_performance(
            "short_a",
            entry_a=100,
            entry_b=100,
            price_a_now=90,
            price_b_now=80,
            taker_fee_pct=0,
            funding_rate_8h_pct=0,
            hedge_ratio=-1,
        )

        assert result["leg_a_side"] == "Шорт"
        assert result["leg_b_side"] == "Шорт"
        assert result["unlevered_return_pct"] == 15

    def test_unknown_signal_is_not_turned_into_fee_only_loss(self):
        result = calc_pair_performance(
            "wait",
            entry_a=100,
            entry_b=100,
            price_a_now=110,
            price_b_now=90,
        )

        assert result == {
            "complete": False,
            "error": "unsupported_signal_type",
        }

    def test_zero_hedge_ratio_allocates_nothing_to_second_leg(self):
        result = calc_pair_performance(
            "long_a",
            entry_a=100,
            entry_b=100,
            price_a_now=110,
            price_b_now=50,
            capital=1000,
            leverage=1,
            taker_fee_pct=0,
            funding_rate_8h_pct=0,
            hedge_ratio=0,
        )

        assert result["leg_a_notional"] == 1000
        assert result["leg_b_notional"] == 0
        assert result["unlevered_return_pct"] == 10
        assert result["gross_pnl"] == 100

    def test_randomized_pair_cash_and_percentage_reconcile(self):
        rng = random.Random(20260728)
        for _ in range(500):
            entry_a = rng.uniform(0.001, 5000)
            entry_b = rng.uniform(0.001, 5000)
            price_a = entry_a * rng.uniform(0.5, 1.5)
            price_b = entry_b * rng.uniform(0.5, 1.5)
            capital = rng.uniform(10, 10000)
            leverage = rng.uniform(0.1, 10)
            fee = rng.uniform(0, 0.2)
            funding = rng.uniform(0, 0.1)
            hold_days = rng.uniform(0, 30)
            hedge_ratio = rng.uniform(-5, 5)
            signal_type = rng.choice(("long_a", "short_a"))

            result = calc_pair_performance(
                signal_type,
                entry_a=entry_a,
                entry_b=entry_b,
                price_a_now=price_a,
                price_b_now=price_b,
                capital=capital,
                leverage=leverage,
                taker_fee_pct=fee,
                funding_rate_8h_pct=funding,
                hold_days=hold_days,
                hedge_ratio=hedge_ratio,
            )

            expected_leg_pnl = (
                result["leg_a_notional"] * result["leg_a_pnl_pct"] / 100
                + result["leg_b_notional"] * result["leg_b_pnl_pct"] / 100
            )
            assert math.isclose(
                result["gross_pnl"],
                expected_leg_pnl,
                abs_tol=0.06,
            )
            assert math.isclose(
                result["net_pnl"],
                result["gross_pnl"] - result["total_cost"],
                abs_tol=0.011,
            )
            assert math.isclose(
                result["net_return_pct"],
                result["net_pnl"] / result["capital"] * 100,
                abs_tol=0.011,
            )


class TestSinglePerformance:
    def test_long_net_pnl_uses_one_position_and_round_trip_fees(self):
        result = calc_single_performance(
            "long_a",
            entry_price=100,
            price_now=110,
            capital=1000,
            leverage=1,
            taker_fee_pct=0.02,
            funding_rate_8h_pct=0,
        )

        assert result["pair_move_pct"] == 10
        assert result["gross_pnl"] == 100
        assert result["commissions"] == 0.4
        assert result["net_pnl"] == 99.6

    def test_short_profits_when_price_falls(self):
        result = calc_single_performance(
            "short_a",
            entry_price=100,
            price_now=90,
            taker_fee_pct=0,
            funding_rate_8h_pct=0,
        )

        assert result["pair_move_pct"] == 10
        assert result["leg_a_side"] == "Шорт"

    def test_non_finite_execution_settings_do_not_poison_result(self):
        result = calc_single_performance(
            "long_a",
            entry_price=100,
            price_now=110,
            capital=math.nan,
            leverage=math.inf,
            taker_fee_pct=math.nan,
            funding_rate_8h_pct=math.inf,
            hold_days=math.nan,
        )

        assert result["complete"] is True
        assert result["capital"] == 0
        assert result["gross_pnl"] == 0
        assert result["net_pnl"] == 0


class TestCalcSignalPnl:
    signal_info = {"spread_sd_pct": 0.05, "z_now": 2.5, "hedge_ratio": 1.0}

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
        r1 = calc_signal_pnl(self.signal_info, capital=1000, leverage=1)
        r2 = calc_signal_pnl(self.signal_info, capital=1000, leverage=10)
        assert r2["position_size"] == 10 * r1["position_size"]

    def test_commissions_scale_with_fee(self):
        r1 = calc_signal_pnl(self.signal_info, capital=1000, leverage=3, taker_fee_pct=0.02)
        r2 = calc_signal_pnl(self.signal_info, capital=1000, leverage=3, taker_fee_pct=0.04)
        assert r2["commissions"] == 2 * r1["commissions"]
        assert r1["commissions"] == 1.2

    def test_funding_scales_with_days(self):
        r1 = calc_signal_pnl(self.signal_info, capital=1000, leverage=3, hold_days=5)
        r2 = calc_signal_pnl(self.signal_info, capital=1000, leverage=3, hold_days=10)
        assert r2["funding_cost"] == 2 * r1["funding_cost"]

    def test_output_types(self):
        result = calc_signal_pnl(self.signal_info)
        assert isinstance(result["capital"], float)
        assert isinstance(result["position_size"], float)
        assert isinstance(result["commissions"], float)
        assert isinstance(result["hold_days"], int)
        assert isinstance(result["risk_reward"], float)

    def test_zero_fee_no_commission(self):
        result = calc_signal_pnl(self.signal_info, taker_fee_pct=0, funding_rate_8h_pct=0)
        assert result["commissions"] == 0
        assert result["funding_cost"] == 0

    def test_risk_reward_is_positive(self):
        result = calc_signal_pnl(self.signal_info, avg_pnl_z=3.0)
        assert result["risk_reward"] > 0

    def test_higher_z_move_gives_higher_gross_pnl(self):
        r1 = calc_signal_pnl(self.signal_info, avg_pnl_z=1.0)
        r2 = calc_signal_pnl(self.signal_info, avg_pnl_z=3.0)
        assert r2["gross_pnl"] > r1["gross_pnl"]

    def test_missing_spread_volatility_is_not_invented(self):
        result = calc_signal_pnl({"z_now": 2.5})

        assert result["complete"] is False

    def test_negative_backtest_expectancy_stays_negative(self):
        result = calc_signal_pnl(
            {
                "spread_sd_pct": 0.05,
                "hedge_ratio": 1.0,
                "backtest_avg_pnl_sigma": -0.5,
                "backtest_validated": 1,
            },
            taker_fee_pct=0,
            funding_rate_8h_pct=0,
        )

        assert result["gross_pnl"] < 0


def test_pair_performance_uses_hedge_ratio_for_leg_notionals():
    result = calc_pair_performance(
        "long_a",
        entry_a=100,
        entry_b=100,
        price_a_now=110,
        price_b_now=100,
        capital=1000,
        leverage=1,
        taker_fee_pct=0,
        funding_rate_8h_pct=0,
        hedge_ratio=3,
    )

    assert result["leg_a_notional"] == 250
    assert result["leg_b_notional"] == 750
    assert result["spread_move_pp"] == 10
    assert result["unlevered_return_pct"] == 2.5
    assert result["gross_pnl"] == 25


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

    def test_invalid_inputs_cannot_create_nan_or_negative_costs(self):
        result = compute_position_details(
            {"halflife": math.nan},
            capital=math.nan,
            leverage=-3,
            taker_fee=math.inf,
            funding_rate=-1,
        )

        assert result["capital"] == 0
        assert result["leverage"] == 0
        assert result["position_size"] == 0
        assert result["commission_total"] == 0
        assert result["funding_total"] == 0
        assert result["estimated_days"] == 30
