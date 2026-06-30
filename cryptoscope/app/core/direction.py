"""Calibrated next-close direction forecast for single assets."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

MIN_PRICE_POINTS = 80
FEATURE_WINDOW = 20


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _fit_logistic(
    features: np.ndarray,
    targets: np.ndarray,
    iterations: int = 600,
    learning_rate: float = 0.08,
    regularization: float = 0.2,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = features.mean(axis=0)
    scale = features.std(axis=0)
    scale[scale < 1e-8] = 1.0
    normalized = (features - mean) / scale
    design = np.column_stack([np.ones(len(normalized)), normalized])
    weights = np.zeros(design.shape[1], dtype=float)

    for _ in range(iterations):
        probabilities = _sigmoid(design @ weights)
        gradient = design.T @ (probabilities - targets) / len(targets)
        gradient[1:] += regularization * weights[1:] / len(targets)
        weights -= learning_rate * gradient

    return weights, mean, scale


def _predict_probability(
    features: np.ndarray,
    model: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> np.ndarray:
    weights, mean, scale = model
    normalized = (features - mean) / scale
    design = np.column_stack([np.ones(len(normalized)), normalized])
    return _sigmoid(design @ weights)


def _feature_at(prices: np.ndarray, returns: np.ndarray, index: int) -> list[float]:
    recent_5 = returns[index - 5:index]
    recent_20 = returns[index - 20:index]
    volatility_20 = max(float(np.std(recent_20)), 1e-8)
    mean_5 = float(np.mean(prices[index - 4:index + 1]))
    mean_20 = float(np.mean(prices[index - 19:index + 1]))
    return [
        float(returns[index - 1]) / volatility_20,
        float(np.sum(returns[index - 3:index])) / volatility_20,
        float(np.sum(recent_5)) / volatility_20,
        float(np.sum(returns[index - 10:index])) / volatility_20,
        math.log(float(prices[index]) / mean_5) / volatility_20,
        math.log(float(prices[index]) / mean_20) / volatility_20,
        float(np.std(recent_5)) / volatility_20 - 1.0,
        float(np.mean(recent_5 > 0)) - 0.5,
    ]


def _build_dataset(
    prices: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    returns = np.diff(np.log(prices))
    features = []
    targets = []
    for index in range(FEATURE_WINDOW, len(prices) - 1):
        if math.isclose(
            float(prices[index + 1]),
            float(prices[index]),
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            continue
        features.append(_feature_at(prices, returns, index))
        targets.append(1.0 if prices[index + 1] > prices[index] else 0.0)
    current = np.asarray(
        [_feature_at(prices, returns, len(prices) - 1)],
        dtype=float,
    )
    return np.asarray(features, dtype=float), np.asarray(targets), current


def forecast_next_close(
    price_values: Sequence[float],
    timestamps: Sequence[int] | None = None,
) -> dict:
    """Estimate the probability that the next close is above the latest close."""
    prices = np.asarray(price_values, dtype=float)
    valid = np.isfinite(prices) & (prices > 0)
    prices = prices[valid]
    if len(prices) < MIN_PRICE_POINTS:
        raise ValueError(
            f"Need at least {MIN_PRICE_POINTS} valid prices, got {len(prices)}"
        )

    clean_timestamps = None
    if timestamps is not None:
        timestamp_values = np.asarray(timestamps)
        if len(timestamp_values) == len(valid):
            clean_timestamps = timestamp_values[valid]

    features, targets, current = _build_dataset(prices)
    if len(targets) < 50:
        raise ValueError("Not enough observations for direction backtest")

    split = max(40, int(len(targets) * 0.75))
    split = min(split, len(targets) - 20)
    holdout_model = _fit_logistic(features[:split], targets[:split])
    holdout_probabilities = _predict_probability(
        features[split:],
        holdout_model,
    )
    holdout_targets = targets[split:]
    holdout_predictions = holdout_probabilities >= 0.5
    accuracy = float(np.mean(holdout_predictions == holdout_targets))
    positive_rate = float(np.mean(holdout_targets))
    baseline_accuracy = max(positive_rate, 1.0 - positive_rate)
    brier_score = float(
        np.mean((holdout_probabilities - holdout_targets) ** 2)
    )

    final_model = _fit_logistic(features, targets)
    raw_probability = float(_predict_probability(current, final_model)[0])
    outperformance = max(0.0, accuracy - baseline_accuracy)
    reliability = float(np.clip(0.45 + outperformance * 4.0, 0.45, 1.0))
    probability_up = 0.5 + (raw_probability - 0.5) * reliability
    probability_up = float(np.clip(probability_up, 0.38, 0.62))
    probability_down = 1.0 - probability_up

    log_returns = np.diff(np.log(prices))
    latest_timestamp = (
        int(clean_timestamps[-1])
        if clean_timestamps is not None and len(clean_timestamps)
        else None
    )
    direction = "up" if probability_up >= 0.5 else "down"
    direction_probability = max(probability_up, probability_down)

    return {
        "direction": direction,
        "probability_up": round(probability_up * 100, 1),
        "probability_down": round(probability_down * 100, 1),
        "direction_probability": round(direction_probability * 100, 1),
        "edge_pp": round(abs(probability_up - 0.5) * 100, 1),
        "latest_price": round(float(prices[-1]), 6),
        "latest_timestamp": latest_timestamp,
        "day_change_pct": round(
            (float(prices[-1] / prices[-2]) - 1.0) * 100,
            2,
        ),
        "momentum_5d_pct": round(
            (float(prices[-1] / prices[-6]) - 1.0) * 100,
            2,
        ),
        "typical_move_pct": round(
            float(np.median(np.abs(log_returns[-60:]))) * 100,
            2,
        ),
        "volatility_20d_pct": round(
            float(np.std(log_returns[-20:])) * 100,
            2,
        ),
        "backtest_accuracy": round(accuracy * 100, 1),
        "baseline_accuracy": round(baseline_accuracy * 100, 1),
        "brier_score": round(brier_score, 4),
        "observations": int(len(targets)),
        "backtest_samples": int(len(holdout_targets)),
    }
