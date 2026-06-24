"""Signal computation and scoring."""

import numpy as np
from typing import Optional


def determine_signal(z_now: Optional[float], z_forecast: Optional[float], ticker_a: str, ticker_b: str) -> dict:
    """
    Determine trading signal from Z-score and forecast.
    
    Returns:
        dict with signal, signal_type, strength
    """
    signal = "Ждать"
    signal_type = "wait"
    strength = "Нет"
    
    if z_now is None and z_forecast is None:
        return {"signal": signal, "signal_type": signal_type, "strength": strength}
    
    z_cur = z_now if z_now is not None else 0
    z_hat = z_forecast if z_forecast is not None else 0
    
    if z_cur >= 2 or z_hat >= 2:
        signal = f"Шорт {ticker_a} / Лонг {ticker_b}"
        signal_type = "short_a"
    elif z_cur <= -2 or z_hat <= -2:
        signal = f"Лонг {ticker_a} / Шорт {ticker_b}"
        signal_type = "long_a"
    
    return {"signal": signal, "signal_type": signal_type}


def determine_strength(is_coint: bool, z_now: Optional[float], z_forecast: Optional[float]) -> str:
    """Determine signal strength category."""
    z_cur = abs(z_now) if z_now is not None else 0
    z_hat = abs(z_forecast) if z_forecast is not None else 0
    
    if is_coint and z_cur >= 2:
        return "Сильный"
    elif z_hat >= 2:
        return "Прогнозный"
    elif z_cur >= 1.5:
        return "Формируется"
    return "Нет"


def compute_pair_score(corr: float, is_coint: bool, halflife: Optional[int]) -> float:
    """Compute composite pair score for ranking."""
    score = abs(corr)
    if is_coint:
        score += 0.3
    if halflife is not None and 5 <= halflife <= 60:
        score += 0.3
    return round(float(score), 4)


def correlation_matrix(log_returns: np.ndarray) -> np.ndarray:
    """Compute correlation matrix from log return matrix."""
    T, N = log_returns.shape
    corr = np.zeros((N, N))
    
    for i in range(N):
        for j in range(i, N):
            ok = ~np.isnan(log_returns[:, i]) & ~np.isnan(log_returns[:, j])
            if ok.sum() >= 30:
                c = float(np.corrcoef(log_returns[ok, i], log_returns[ok, j])[0, 1])
                corr[i, j] = c if not np.isnan(c) else 0.0
                corr[j, i] = corr[i, j]
    return corr
