"""Tests for Engle-Granger cointegration, Z-score, AR(1) forecast."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.cointegration import (
    compute_fixed_zscore,
    compute_zscore,
    engle_granger,
    fit_fixed_zscore_model,
    forecast_zscore,
)


class TestEngleGranger:
    def test_cointegrated_pair(self, sample_prices):
        pa, pb = sample_prices
        result = engle_granger(pa, pb)
        
        assert "halflife" in result
        assert "t_stat" in result
        assert "p_value" in result
        assert "critical_5pct" in result
        assert "is_coint" in result
        assert "hedge_ratio" in result
        assert isinstance(result["is_coint"], bool)

    def test_cointegrated_pair_returns_positive_halflife(self, sample_prices):
        pa, pb = sample_prices
        result = engle_granger(pa, pb)
        
        if result["is_coint"] and result["halflife"] is not None:
            assert result["halflife"] > 0

    def test_t_stat_is_negative_when_cointegrated(self, sample_prices):
        pa, pb = sample_prices
        result = engle_granger(pa, pb)
        
        if result["is_coint"] and result["t_stat"] is not None:
            assert result["t_stat"] <= result["critical_5pct"]
            assert result["p_value"] <= 0.05

    def test_non_cointegrated_pair(self, non_cointegrated_prices):
        pa, pb = non_cointegrated_prices
        result = engle_granger(pa, pb)
        # Non-cointegrated pairs should have is_coint=False or t_stat > -2.9
        assert isinstance(result["is_coint"], bool)
        if result["halflife"] is not None:
            assert result["halflife"] > 0

    def test_min_obs_threshold(self):
        """Should return non-cointegrated when too few observations."""
        pa = np.array([100.0, 101.0, 102.0])
        pb = np.array([50.0, 51.0, 52.0])
        result = engle_granger(pa, pb, min_obs=60)
        assert result["is_coint"] is False
        assert result["halflife"] is None

    def test_max_pvalue_controls_significance(self, sample_prices):
        pa, pb = sample_prices

        strict = engle_granger(pa, pb, max_pvalue=0.0)
        assert strict["is_coint"] is False  # p <= 0 is impossible

        default = engle_granger(pa, pb)
        if default["p_value"] is not None and default["p_value"] <= 0.05:
            assert default["is_coint"] is True

    def test_hedge_ratio_is_finite(self, sample_prices):
        pa, pb = sample_prices
        result = engle_granger(pa, pb)
        if result["hedge_ratio"] is not None:
            assert np.isfinite(result["hedge_ratio"])

    def test_with_nans(self, sample_prices):
        pa, pb = sample_prices
        pa[:5] = np.nan
        pb[-5:] = np.nan
        result = engle_granger(pa, pb)
        # Should not crash and should return sensible results
        assert isinstance(result["is_coint"], bool)

    def test_output_types(self, sample_prices):
        pa, pb = sample_prices
        result = engle_granger(pa, pb)
        assert isinstance(result["is_coint"], bool)
        if result["t_stat"] is not None:
            assert isinstance(result["t_stat"], float)
        if result["hedge_ratio"] is not None:
            assert isinstance(result["hedge_ratio"], float)
        if result["halflife"] is not None:
            assert isinstance(result["halflife"], (int, float))

    def test_negative_ar_phi_has_no_half_life(self):
        rng = np.random.default_rng(20260728)
        n = 500
        log_b = np.cumsum(rng.normal(0, 0.01, n)) + 4.0
        residual = np.zeros(n)
        for idx in range(1, n):
            residual[idx] = -0.65 * residual[idx - 1] + rng.normal(0, 0.01)
        pa = np.exp(0.3 + 1.1 * log_b + residual)
        pb = np.exp(log_b)

        result = engle_granger(pa, pb)

        assert result["ar_phi"] is not None
        assert result["ar_phi"] < 0
        assert result["halflife"] is None


class TestFixedZScoreModel:
    def test_entry_prices_are_anchored_to_signal_z(self, sample_prices):
        pa, pb = sample_prices
        model = fit_fixed_zscore_model(
            pa,
            pb,
            z_at_entry=-2.0,
            price_a_entry=pa[-1],
            price_b_entry=pb[-1],
        )

        z_now = compute_fixed_zscore(
            pa[-1],
            pb[-1],
            model["hedge_ratio_entry"],
            model["spread_mean_entry"],
            model["spread_sd_entry"],
        )

        assert z_now == pytest.approx(-2.0)

    def test_saved_model_stays_anchored_after_history_changes(
        self,
        sample_prices,
    ):
        pa, pb = sample_prices
        model = fit_fixed_zscore_model(
            pa,
            pb,
            z_at_entry=2.4,
            price_a_entry=pa[-1],
            price_b_entry=pb[-1],
        )

        # A later analysis can refit independently without changing this model.
        fit_fixed_zscore_model(
            np.append(pa, pa[-1]),
            np.append(pb, pb[-1]),
        )
        fixed_z = compute_fixed_zscore(
            pa[-1],
            pb[-1],
            model["hedge_ratio_entry"],
            model["spread_mean_entry"],
            model["spread_sd_entry"],
        )

        assert fixed_z == pytest.approx(2.4)


class TestZScore:
    def test_zscore_computation(self, sample_prices):
        pa, pb = sample_prices
        result = compute_zscore(pa, pb)
        
        assert "zscores" in result
        assert "z_now" in result
        assert "sd" in result
        assert "mean" in result
        
        if result["zscores"] is not None:
            assert len(result["zscores"]) == result["n"]
            # Z-scores should have mean ~0 (approximately)
            assert abs(np.mean(result["zscores"])) < 0.1

    def test_zscore_with_hedge_ratio(self, sample_prices):
        pa, pb = sample_prices
        cg = engle_granger(pa, pb)
        result = compute_zscore(pa, pb, cg["hedge_ratio"])
        assert result["z_now"] is not None
        assert isinstance(result["z_now"], float)

    def test_zscore_min_obs(self):
        pa = np.array([100.0] * 5)
        pb = np.array([50.0] * 5)
        result = compute_zscore(pa, pb, min_obs=30)
        assert result["zscores"] is None

    def test_zscore_sd_positive(self, sample_prices):
        pa, pb = sample_prices
        result = compute_zscore(pa, pb)
        if result["sd"] is not None:
            assert result["sd"] > 0

    def test_zscore_ignores_non_finite_and_non_positive_prices(
        self,
        sample_prices,
    ):
        pa, pb = sample_prices
        pa = pa.copy()
        pb = pb.copy()
        pa[0] = np.inf
        pa[1] = 0
        pb[2] = -1

        result = compute_zscore(pa, pb)

        assert result["zscores"] is not None
        assert result["n"] == len(pa) - 3
        assert np.isfinite(result["z_now"])

    def _regime_shifted_prices(self):
        """Deterministic pair whose spread shifts level 120 sessions ago."""
        n = 300
        t = np.arange(n, dtype=float)
        la = np.log(100.0) + 0.01 * np.sin(t)
        lb = np.log(50.0) + 0.02 * np.cos(t)
        la[-120:] += 1.0
        return np.exp(la), np.exp(lb)

    def test_zscore_window_normalizes_on_trailing_window(self):
        pa, pb = self._regime_shifted_prices()

        result = compute_zscore(pa, pb, 1.0, window=120)

        trailing = result["zscores"][-120:]
        assert abs(float(np.mean(trailing))) < 1e-9
        assert float(np.std(trailing)) == pytest.approx(1.0)
        assert result["z_prev"] == pytest.approx(float(result["zscores"][-2]))

    def test_zscore_window_reacts_to_regime_shift(self):
        pa, pb = self._regime_shifted_prices()

        full = compute_zscore(pa, pb, 1.0)
        windowed = compute_zscore(pa, pb, 1.0, window=120)

        # Full-history normalization treats the whole recent regime as abnormal:
        assert float(np.mean(full["zscores"][-120:])) > 1.0
        # Windowed normalization is centered on the current regime instead:
        assert abs(float(np.mean(windowed["zscores"][-120:]))) < 1e-9
        assert abs(windowed["z_now"]) < 2.0

    def test_zscore_without_window_keeps_full_history(self):
        pa, pb = self._regime_shifted_prices()

        result = compute_zscore(pa, pb, 1.0)

        assert abs(float(np.mean(result["zscores"]))) < 1e-9


class TestForecast:
    def test_forecast_output(self, sample_zscore_series):
        result = forecast_zscore(sample_zscore_series)
        
        assert "z_forecast" in result
        assert result["z_forecast"] is not None
        assert "phi" in result
        
        # AR(1) phi should be between 0 and 1 for mean-reverting series
        if result["phi"] is not None:
            assert 0 < result["phi"] < 1

    def test_forecast_close_to_current(self, sample_zscore_series):
        result = forecast_zscore(sample_zscore_series)
        z_now = sample_zscore_series[-1]
        z_forecast = result["z_forecast"]
        
        # Forecast should not be wildly different from current
        assert abs(z_now - z_forecast) < 2.0

    def test_forecast_min_obs(self):
        z = np.array([1.0, 1.5, 2.0])
        result = forecast_zscore(z, min_obs=20)
        assert result["z_forecast"] is not None
        assert result["phi"] is None  # Not enough data for reliable phi

    def test_forecast_output_types(self, sample_zscore_series):
        result = forecast_zscore(sample_zscore_series)
        assert isinstance(result["z_forecast"], float)
        if result["phi"] is not None:
            assert isinstance(result["phi"], float)
        if result["intercept"] is not None:
            assert isinstance(result["intercept"], float)

    def test_explosive_ar_fit_is_not_used_as_forecast(self):
        z = np.array([1.2**idx for idx in range(30)], dtype=float)

        result = forecast_zscore(z)

        assert result["phi"] >= 1
        assert result["z_forecast"] is None
