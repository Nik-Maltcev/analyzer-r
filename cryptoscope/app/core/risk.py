"""Market-regime and pair-stability safeguards."""

from __future__ import annotations

import json
from collections.abc import Sequence

import numpy as np

from app.core.cointegration import engle_granger

COINT_WINDOWS = (60, 120, 252)


def _aligned_prices(pa: np.ndarray, pb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = np.asarray(pa, dtype=float)
    b = np.asarray(pb, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
    return a[ok], b[ok]


def assess_cointegration_stability(
    pa: np.ndarray,
    pb: np.ndarray,
    windows: Sequence[int] = COINT_WINDOWS,
) -> dict:
    """Require cointegration to survive both recent and medium-term windows."""
    a, b = _aligned_prices(pa, pb)
    results: dict[str, dict | None] = {}

    for window in windows:
        if len(a) < window:
            results[str(window)] = None
            continue
        result = engle_granger(a[-window:], b[-window:], min_obs=min(60, window))
        results[str(window)] = {
            "is_coint": bool(result["is_coint"]),
            "hedge_ratio": result["hedge_ratio"],
            "t_stat": result["t_stat"],
        }

    available = [result for result in results.values() if result is not None]
    passed = [result for result in available if result["is_coint"]]
    recent = results.get(str(windows[0])) if windows else None

    ratios = [
        float(result["hedge_ratio"])
        for result in available
        if result["hedge_ratio"] is not None
        and np.isfinite(float(result["hedge_ratio"]))
    ]
    ratio_stable = True
    if len(ratios) >= 2:
        median_ratio = float(np.median(ratios))
        same_direction = all(np.sign(ratio) == np.sign(median_ratio) for ratio in ratios)
        relative_span = (
            (max(ratios) - min(ratios)) / max(abs(median_ratio), 0.05)
        )
        ratio_stable = same_direction and relative_span <= 0.75

    pass_ratio = len(passed) / len(available) if available else 0.0
    stability_pct = round(pass_ratio * 100)
    if not ratio_stable:
        stability_pct = min(stability_pct, 40)

    is_stable = bool(
        len(available) >= 2
        and recent
        and recent["is_coint"]
        and len(passed) >= 2
        and ratio_stable
    )

    if len(available) < 2:
        reason = "Недостаточно истории для проверки устойчивости"
    elif not recent or not recent["is_coint"]:
        reason = "Коинтеграция не подтверждается на окне 60 дней"
    elif len(passed) < 2:
        reason = "Коинтеграция подтверждена только на одном окне"
    elif not ratio_stable:
        reason = "Коэффициент пары заметно меняется между окнами"
    else:
        reason = "Связь подтверждена минимум на двух окнах"

    compact_results = {
        window: None if result is None else bool(result["is_coint"])
        for window, result in results.items()
    }
    return {
        "is_coint_stable": is_stable,
        "coint_stability": stability_pct,
        "coint_windows": json.dumps(compact_results, separators=(",", ":")),
        "coint_windows_passed": len(passed),
        "coint_windows_available": len(available),
        "coint_stability_reason": reason,
    }


def assess_market_regime(prices: np.ndarray) -> dict:
    """Estimate the broad market regime from robust cross-sectional returns."""
    matrix = np.asarray(prices, dtype=float)
    result = {
        "market_regime": "normal",
        "market_volatility": None,
        "market_latest_move": None,
        "market_max_5d_move": None,
        "market_regime_reason": "Недостаточно рыночных данных",
        "market_returns": np.array([], dtype=float),
    }
    if matrix.ndim != 2 or matrix.shape[0] < 3 or matrix.shape[1] < 2:
        return result

    valid = np.isfinite(matrix) & (matrix > 0)
    returns = np.full((matrix.shape[0] - 1, matrix.shape[1]), np.nan)
    pair_valid = valid[1:] & valid[:-1]
    returns[pair_valid] = np.log(matrix[1:][pair_valid] / matrix[:-1][pair_valid])

    minimum_assets = max(2, int(np.ceil(matrix.shape[1] * 0.25)))
    market_returns = np.full(returns.shape[0], np.nan)
    for index, row in enumerate(returns):
        finite = row[np.isfinite(row)]
        if len(finite) >= minimum_assets:
            market_returns[index] = float(np.median(finite))

    clean = market_returns[np.isfinite(market_returns)]
    result["market_returns"] = market_returns
    if len(clean) < 2:
        return result

    latest_move = abs(float(clean[-1])) * 100
    max_5d_move = float(np.max(np.abs(clean[-5:]))) * 100
    vol_sample = clean[-20:]
    annualized_vol = (
        float(np.std(vol_sample, ddof=1) * np.sqrt(252) * 100)
        if len(vol_sample) >= 5
        else None
    )

    if max_5d_move >= 3.0 or (annualized_vol is not None and annualized_vol >= 40):
        regime = "stress"
    elif max_5d_move >= 1.8 or (annualized_vol is not None and annualized_vol >= 25):
        regime = "elevated"
    else:
        regime = "normal"

    if max_5d_move >= 1.8:
        reason = f"Максимальное движение рынка за 5 дней: {max_5d_move:.1f}%"
    elif annualized_vol is not None:
        reason = f"Расчётная волатильность: {annualized_vol:.0f}% годовых"
    else:
        reason = "Рынок движется в обычном диапазоне"

    result.update({
        "market_regime": regime,
        "market_volatility": (
            round(annualized_vol, 2) if annualized_vol is not None else None
        ),
        "market_latest_move": round(latest_move, 2),
        "market_max_5d_move": round(max_5d_move, 2),
        "market_regime_reason": reason,
    })
    return result


def detect_recent_event_gap(
    pa: np.ndarray,
    pb: np.ndarray,
    market_returns: np.ndarray | None = None,
    ticker_a: str = "A",
    ticker_b: str = "B",
    lookback: int = 3,
) -> dict:
    """Flag a recent one-leg move that is much larger than the broad market."""
    a = np.asarray(pa, dtype=float)
    b = np.asarray(pb, dtype=float)
    n = min(len(a), len(b))
    result = {"event_risk": False, "event_risk_reason": None}
    if n < 2:
        return result

    a = a[-n:]
    b = b[-n:]
    valid = (
        np.isfinite(a[1:])
        & np.isfinite(a[:-1])
        & np.isfinite(b[1:])
        & np.isfinite(b[:-1])
        & (a[1:] > 0)
        & (a[:-1] > 0)
        & (b[1:] > 0)
        & (b[:-1] > 0)
    )
    ret_a = np.full(n - 1, np.nan)
    ret_b = np.full(n - 1, np.nan)
    ret_a[valid] = np.log(a[1:][valid] / a[:-1][valid])
    ret_b[valid] = np.log(b[1:][valid] / b[:-1][valid])

    market = np.asarray(
        market_returns if market_returns is not None else np.zeros(n - 1),
        dtype=float,
    )
    if len(market) < n - 1:
        market = np.pad(market, (n - 1 - len(market), 0), constant_values=np.nan)
    market = market[-(n - 1):]
    market = np.where(np.isfinite(market), market, 0.0)

    start = max(0, len(ret_a) - lookback)
    candidates = []
    for index in range(start, len(ret_a)):
        if not np.isfinite(ret_a[index]) or not np.isfinite(ret_b[index]):
            continue
        pair_gap = abs(ret_a[index] - ret_b[index])
        abnormal_a = abs(ret_a[index] - market[index])
        abnormal_b = abs(ret_b[index] - market[index])
        if pair_gap >= 0.06 and max(abnormal_a, abnormal_b) >= 0.04:
            leg = ticker_a if abnormal_a >= abnormal_b else ticker_b
            move = ret_a[index] if leg == ticker_a else ret_b[index]
            candidates.append((pair_gap, leg, move))

    if not candidates:
        return result

    _, leg, move = max(candidates, key=lambda item: item[0])
    return {
        "event_risk": True,
        "event_risk_reason": (
            f"Резкий одиночный гэп {leg}: {move * 100:+.1f}%. "
            "Проверьте дивиденды и корпоративные новости"
        ),
    }


def guard_signal(
    market: str,
    signal: dict,
    strength: str,
    stability: dict,
    event_gap: dict,
    market_regime: str,
) -> dict:
    """Block unreliable Russian signals while preserving ordinary markets."""
    guarded = {
        **signal,
        "strength": strength,
        "signal_eligible": signal.get("signal_type") != "wait",
        "risk_reason": None,
    }
    if market != "ru" or signal.get("signal_type") == "wait":
        return guarded

    if event_gap["event_risk"]:
        return {
            **guarded,
            "signal": "Пауза: ценовой гэп",
            "signal_type": "wait",
            "strength": "Пауза",
            "signal_eligible": False,
            "risk_reason": event_gap["event_risk_reason"],
        }

    if not stability["is_coint_stable"]:
        return {
            **guarded,
            "signal": "Наблюдение: связь нестабильна",
            "signal_type": "wait",
            "strength": "Наблюдение",
            "signal_eligible": False,
            "risk_reason": stability["coint_stability_reason"],
        }

    if market_regime == "stress":
        guarded["strength"] = "Высокий риск"
        guarded["risk_reason"] = "Стрессовый режим рынка: прогноз показан диапазоном"
    elif market_regime == "elevated":
        guarded["strength"] = "Осторожно"
        guarded["risk_reason"] = "Повышенная волатильность рынка"

    return guarded


def forecast_scenario(
    z_forecast: float | None,
    residual_sd: float | None,
    market_regime: str,
) -> dict:
    """Return a one-step scenario range, widened in a stressed market."""
    if (
        z_forecast is None
        or residual_sd is None
        or not np.isfinite(z_forecast)
        or not np.isfinite(residual_sd)
        or residual_sd <= 0
    ):
        return {"z_forecast_low": None, "z_forecast_high": None}

    multiplier = 1.5 if market_regime == "stress" else 1.2 if market_regime == "elevated" else 1.0
    width = float(residual_sd) * multiplier
    return {
        "z_forecast_low": round(float(z_forecast) - width, 4),
        "z_forecast_high": round(float(z_forecast) + width, 4),
    }
