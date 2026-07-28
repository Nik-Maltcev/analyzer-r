"""Signal computation and scoring."""

from datetime import UTC, datetime, timedelta

import numpy as np


def is_actionable_signal(signal_type: str | None, is_coint_stable: bool) -> bool:
    """Return whether a pair has both a direction and validated mean reversion."""
    return signal_type not in (None, "wait") and bool(is_coint_stable)


def resolve_signal_started_at(
    current_signal_type: str,
    previous_signal_type: str | None,
    previous_started_at: str | None,
    previous_computed_at: str | None,
    now: str,
) -> str | None:
    """Keep the start of an uninterrupted signal, resetting on direction changes."""
    if current_signal_type == "wait":
        return None
    if previous_signal_type == current_signal_type:
        return previous_started_at or previous_computed_at or now
    return now


def estimate_signal_timing(
    started_at,
    halflife: int | None,
    now: datetime | None = None,
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
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
            start_dt = parsed
            break
        except (TypeError, ValueError):
            continue
    if start_dt is None:
        return timing

    now_dt = now or datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    else:
        now_dt = now_dt.astimezone(UTC)

    # The UI presents dates, so count crossed calendar days instead of full
    # 24-hour intervals. A signal from the 26th is two days old on the 28th.
    elapsed_days = max(0, (now_dt.date() - start_dt.date()).days)
    timing.update({
        "signal_started_at": start_dt.isoformat(),
        "signal_started_date": start_dt.strftime("%d.%m.%Y"),
        "signal_days_elapsed": elapsed_days,
    })

    try:
        hl = int(halflife) if halflife is not None else None
    except (TypeError, ValueError):
        hl = None
    if not hl or hl <= 0:
        return timing

    expected_end = start_dt + timedelta(days=hl)
    days_until_end = (expected_end.date() - now_dt.date()).days
    is_expired = days_until_end <= 0
    days_remaining = max(0, days_until_end)
    days_overdue = max(0, -days_until_end)

    timing.update({
        "signal_expected_end_at": expected_end.isoformat(),
        "signal_expected_end_date": expected_end.strftime("%d.%m.%Y"),
        "signal_days_remaining": days_remaining,
        "signal_days_overdue": days_overdue,
        "signal_is_expired": is_expired,
        "signal_time_progress_pct": min(100, round(elapsed_days / hl * 100)),
    })
    return timing


def elapsed_holding_days(
    started_at,
    now: datetime | None = None,
) -> float:
    """Return exact elapsed 24-hour periods for cost calculations."""
    if started_at is None:
        return 0.0
    try:
        if isinstance(started_at, datetime):
            start_dt = started_at
        else:
            timestamp = str(started_at).strip().replace(" ", "T")
            if timestamp.endswith("Z"):
                timestamp = f"{timestamp[:-1]}+00:00"
            start_dt = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return 0.0
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    else:
        start_dt = start_dt.astimezone(UTC)
    now_dt = now or datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    else:
        now_dt = now_dt.astimezone(UTC)
    return max(0.0, (now_dt - start_dt).total_seconds() / 86400)


def determine_signal(
    z_now: float | None,
    z_forecast: float | None,
    ticker_a: str,
    ticker_b: str,
    hedge_ratio: float = 1.0,
) -> dict:
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

    try:
        beta_is_negative = float(hedge_ratio) < 0
    except (TypeError, ValueError):
        beta_is_negative = False

    if z_cur >= 2 or z_hat >= 2:
        side_b = "Шорт" if beta_is_negative else "Лонг"
        signal = f"Шорт {ticker_a} / {side_b} {ticker_b}"
        signal_type = "short_a"
    elif z_cur <= -2 or z_hat <= -2:
        side_b = "Лонг" if beta_is_negative else "Шорт"
        signal = f"Лонг {ticker_a} / {side_b} {ticker_b}"
        signal_type = "long_a"

    return {"signal": signal, "signal_type": signal_type}


def determine_strength(is_coint: bool, z_now: float | None, z_forecast: float | None) -> str:
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


def compute_pair_score(corr: float, is_coint: bool, halflife: int | None) -> float:
    """Compute composite pair score for ranking."""
    score = abs(corr)
    if is_coint:
        score += 0.3
    if halflife is not None and 5 <= halflife <= 60:
        score += 0.3
    return round(float(score), 4)


def correlation_matrix(log_returns: np.ndarray) -> np.ndarray:
    """Compute correlation matrix from log return matrix."""
    _, n_assets = log_returns.shape
    corr = np.zeros((n_assets, n_assets))

    for i in range(n_assets):
        for j in range(i, n_assets):
            ok = ~np.isnan(log_returns[:, i]) & ~np.isnan(log_returns[:, j])
            if ok.sum() >= 30:
                if i == j:
                    corr[i, i] = 1.0 if np.std(log_returns[ok, i]) > 0 else 0.0
                    continue
                c = float(np.corrcoef(log_returns[ok, i], log_returns[ok, j])[0, 1])
                corr[i, j] = c if not np.isnan(c) else 0.0
                corr[j, i] = corr[i, j]
    return corr
