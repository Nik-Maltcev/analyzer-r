"""Tests for signal computation and scoring."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.signals import determine_signal, determine_strength, compute_pair_score, correlation_matrix
import numpy as np


class TestDetermineSignal:
    def test_short_a_signal(self):
        result = determine_signal(z_now=2.5, z_forecast=1.5, ticker_a="BTC", ticker_b="ETH")
        assert result["signal_type"] == "short_a"
        assert "Шорт BTC" in result["signal"]
        assert "Лонг ETH" in result["signal"]

    def test_long_a_signal(self):
        result = determine_signal(z_now=-2.3, z_forecast=-1.0, ticker_a="BTC", ticker_b="ETH")
        assert result["signal_type"] == "long_a"
        assert "Лонг BTC" in result["signal"]
        assert "Шорт ETH" in result["signal"]

    def test_wait_signal(self):
        result = determine_signal(z_now=0.5, z_forecast=0.3, ticker_a="BTC", ticker_b="ETH")
        assert result["signal_type"] == "wait"
        assert result["signal"] == "Ждать"

    def test_forecast_triggers_signal(self):
        result = determine_signal(z_now=1.0, z_forecast=2.5, ticker_a="BTC", ticker_b="ETH")
        assert result["signal_type"] == "short_a"

    def test_negative_forecast_triggers_signal(self):
        result = determine_signal(z_now=-1.0, z_forecast=-2.8, ticker_a="BTC", ticker_b="ETH")
        assert result["signal_type"] == "long_a"

    def test_none_inputs(self):
        result = determine_signal(z_now=None, z_forecast=None, ticker_a="BTC", ticker_b="ETH")
        assert result["signal_type"] == "wait"


class TestDetermineStrength:
    def test_strong_signal(self):
        strength = determine_strength(is_coint=True, z_now=2.5, z_forecast=2.1)
        assert strength == "Сильный"

    def test_forecast_signal(self):
        strength = determine_strength(is_coint=False, z_now=1.0, z_forecast=2.5)
        assert strength == "Прогнозный"

    def test_forming_signal(self):
        strength = determine_strength(is_coint=False, z_now=1.8, z_forecast=1.5)
        assert strength == "Формируется"

    def test_no_signal(self):
        strength = determine_strength(is_coint=False, z_now=0.5, z_forecast=0.3)
        assert strength == "Нет"

    def test_none_input_handled(self):
        strength = determine_strength(is_coint=False, z_now=None, z_forecast=None)
        assert strength == "Нет"


class TestPairScore:
    def test_maximum_score(self):
        score = compute_pair_score(corr=0.95, is_coint=True, halflife=30)
        assert score > 1.3
        assert score <= 1.6

    def test_minimum_score(self):
        score = compute_pair_score(corr=0.1, is_coint=False, halflife=None)
        assert score < 0.5

    def test_mid_score(self):
        score = compute_pair_score(corr=0.6, is_coint=True, halflife=None)
        assert 0.8 < score < 1.0

    def test_score_is_float(self):
        score = compute_pair_score(corr=0.5, is_coint=False, halflife=30)
        assert isinstance(score, float)


class TestCorrelationMatrix:
    def test_basic_correlation(self):
        np.random.seed(42)
        n, m = 100, 3
        data = np.random.randn(n, m)
        corr = correlation_matrix(data)
        
        assert corr.shape == (m, m)
        assert np.all(np.diag(corr) == 1.0)
        assert np.allclose(corr, corr.T)

    def test_symmetric(self):
        np.random.seed(42)
        data = np.random.randn(200, 5)
        corr = correlation_matrix(data)
        assert np.allclose(corr, corr.T)

    def test_diagonal_one(self):
        data = np.random.randn(100, 4)
        corr = correlation_matrix(data)
        assert np.allclose(np.diag(corr), 1.0)
