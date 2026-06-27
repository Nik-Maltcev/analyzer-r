"""Signal computation and scoring."""

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np


def resolve_signal_started_at(
    current_signal_type: str,
    previous_signal_type: Optional[str],
    previous_started_at: Optional[str],
    previous_computed_at: Optional[str],
    now: str,
) -> Optional[str]:
    """Keep the start of an uninterrupted signal, resetting on direction changes."""
    if current_signal_type == "wait":
        return None
    if previous_signal_type == current_signal_type:
        return previous_started_at or previous_computed_at or now
    return now


def estimate_signal_timing(
    started_at,
    halflife: Optional[int],
    now: Optional[datetime] = None,
    fallback_started_at=None,
) -> dict:
    """Estimate a signal horizon from its start timestamp and statistical half-life."""
    timing = {
        "signal_started_at": None,
        "signal_started_date": None,
        "signal_expected_end_at": None,
        "signal_expected_end_date": None,
        "signal_days_elapsed": 0,
        "signal_days_remaining": None,
        "signal_days_overdue": 0,
        "signal_is_expired": False,
        "signal_time_progress_pct": 0,
    }
    if started_at is None and fallback_started_at is None:
        return timing

    start_dt = None
    for candidate in (started_at, fallback_started_at):
        try:
            if isinstance(candidate, datetime):
                parsed = candidate
            else:
                timestamp = str(candidate).strip().replace(" ", "T")
                if timestamp.endswith("Z"):
                    timestamp = f"{timestamp[:-1]}+00:00"
                parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            start_dt = parsed
            break
        except (TypeError, ValueError):
            continue
    if start_dt is None:
        return timing

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    else:
        now_dt = now_dt.astimezone(timezone.utc)

    elapsed_seconds = max(0.0, (now_dt - start_dt).total_seconds())
    timing.update({
        "signal_started_at": start_dt.isoformat(),
        "signal_started_date": start_dt.strftime("%d.%m.%Y"),
        "signal_days_elapsed": int(elapsed_seconds // 86400),
    })

    try:
        hl = int(halflife) if halflife is not None else None
    except (TypeError, ValueError):
        hl = None
    if not hl or hl <= 0:
        return timing

    expected_end = start_dt + timedelta(days=hl)
    remaining_seconds = (expected_end - now_dt).total_seconds()
    is_expired = remaining_seconds <= 0
    days_remaining = 0 if is_expired else math.ceil(remaining_seconds / 86400)
    days_overdue = math.ceil(abs(remaining_seconds) / 86400) if is_expired else 0

    timing.update({
        "signal_expected_end_at": expected_end.isoformat(),
        "signal_expected_end_date": expected_end.strftime("%d.%m.%Y"),
        "signal_days_remaining": days_remaining,
        "signal_days_overdue": days_overdue,
        "signal_is_expired": is_expired,
        "signal_time_progress_pct": min(100, round(elapsed_seconds / (hl * 86400) * 100)),
    })
    return timing


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
