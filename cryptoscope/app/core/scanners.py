"""Scanner algorithms: correlation breakdown, momentum, drawdown."""

import numpy as np
import pandas as pd
from typing import Dict, Any, List

from app.core.risk import assess_market_regime


def _lower_confidence(confidence: str) -> str:
    return {
        "Высокая": "Средняя",
        "Средняя": "Низкая",
    }.get(confidence, confidence)


def scanner_market_context(prices: np.ndarray, market: str) -> dict:
    """Return broad-market risk context only for the Russian market."""
    context = {
        "market_regime": "normal",
        "market_volatility": None,
        "market_max_5d_move": None,
        "market_regime_reason": None,
    }
    if str(market).lower() != "ru":
        return context

    regime = assess_market_regime(prices)
    return {
        "market_regime": regime.get("market_regime") or "normal",
        "market_volatility": regime.get("market_volatility"),
        "market_max_5d_move": regime.get("market_max_5d_move"),
        "market_regime_reason": regime.get("market_regime_reason"),
    }


def apply_scanner_market_context(
    records: List[Dict[str, Any]],
    market: str,
    context: dict,
) -> List[Dict[str, Any]]:
    """Add RU regime warnings without changing scanner directions."""
    if str(market).lower() != "ru":
        return records

    regime = context.get("market_regime") or "normal"
    if regime not in {"stress", "elevated"}:
        return records

    if regime == "stress":
        regime_note = (
            "Стрессовый режим RU: подтвердите вход после следующего обновления "
            "и уменьшите размер позиции"
        )
    else:
        regime_note = (
            "Повышенная волатильность RU: используйте меньший размер позиции"
        )

    for record in records:
        record["market_regime"] = regime
        if regime == "stress" and record.get("confidence"):
            record["confidence"] = _lower_confidence(
                str(record["confidence"])
            )
        existing_note = str(record.get("risk_note") or "").strip()
        record["risk_note"] = (
            f"{existing_note}. {regime_note}"
            if existing_note
            else regime_note
        )
    return records


def corr_breakdown_scan(price_matrix: pd.DataFrame, tickers: List[str]) -> pd.DataFrame:
    """
    Find pairs where rolling correlation deviates from static correlation by >= 0.2.
    
    Returns DataFrame with columns: ticker_a, ticker_b, static_corr, rolling_corr, deviation, signal
    """
    if len(tickers) < 2:
        return pd.DataFrame()
    
    log_rets = np.log(price_matrix[tickers] / price_matrix[tickers].shift(1))
    
    static_corr = log_rets.corr().values
    
    if len(log_rets) < 30:
        return pd.DataFrame()
    
    rolling_corr = log_rets.iloc[-30:].corr().values
    
    results = []
    n = len(tickers)
    
    for i in range(n):
        for j in range(i + 1, n):
            sc = static_corr[i, j]
            rc = rolling_corr[i, j]
            
            if np.isnan(sc) or np.isnan(rc):
                continue
            
            dev = abs(sc - rc)
            if dev >= 0.2:
                relationship_weakened = (
                    sc * rc < 0
                    or abs(rc) < abs(sc)
                )
                signal = (
                    "Корреляция сломалась"
                    if relationship_weakened
                    else "Синхронизировались сильнее"
                )
                if relationship_weakened:
                    recommendation = "Не открывать пару"
                    recommendation_reason = (
                        "Корреляция заметно ослабла"
                        if dev >= 0.5
                        else "Корреляция ослабла"
                    )
                else:
                    recommendation = "Перепроверить пару"
                    recommendation_reason = "Активы синхронизировались сильнее"
                results.append({
                    "ticker_a": tickers[i],
                    "ticker_b": tickers[j],
                    "static_corr": round(float(sc), 4),
                    "rolling_corr": round(float(rc), 4),
                    "deviation": round(float(dev), 4),
                    "signal": signal,
                    "recommendation": recommendation,
                    "recommendation_class": "wait",
                    "confidence": "Высокая" if dev >= 0.5 else "Средняя",
                    "recommendation_reason": recommendation_reason,
                    "scanner": "corrbreak",
                })
    
    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values("deviation", ascending=False)
    return df


def momentum_scan(prices: np.ndarray, tickers: List[str], dates: List[str]) -> pd.DataFrame:
    """
    Find tickers with strong momentum based on multi-timeframe returns.
    
    Classifies: Strong rise, Rise, Sideways, Fall, Strong fall
    """
    if len(prices) < 14:
        return pd.DataFrame()
    
    results = []
    
    for i, ticker in enumerate(tickers):
        col = prices[:, i]
        ok = ~np.isnan(col)
        if ok.sum() < 14:
            continue
        
        valid = col[ok]
        
        p3 = (valid[-1] / valid[-min(4, len(valid))] - 1) * 100 if len(valid) >= 4 else 0
        p7 = (valid[-1] / valid[-min(8, len(valid))] - 1) * 100 if len(valid) >= 8 else 0
        p14 = (valid[-1] / valid[-min(15, len(valid))] - 1) * 100 if len(valid) >= 15 else 0
        
        vol7 = float(np.std(np.diff(np.log(valid[-min(8, len(valid)):])))) * 100 if len(valid) >= 8 else 0
        
        avg_m = (p3 + p7 * 2 + p14 * 3) / 6
        
        if avg_m > 10:
            trend = "Сильный рост"
            signal = "Лонг"
        elif avg_m > 3:
            trend = "Рост"
            signal = "Лонг"
        elif avg_m < -10:
            trend = "Сильное падение"
            signal = "Шорт"
        elif avg_m < -3:
            trend = "Падение"
            signal = "Шорт"
        else:
            trend = "Боковик"
            signal = "Ждать"

        direction = 1 if signal == "Лонг" else -1 if signal == "Шорт" else 0
        confirmations = sum(
            1 for move in (p3, p7, p14)
            if direction and np.sign(move) == direction
        )
        if signal == "Лонг":
            recommendation = "Рассмотреть лонг"
            recommendation_class = "long"
        elif signal == "Шорт":
            recommendation = "Рассмотреть шорт"
            recommendation_class = "short"
        else:
            recommendation = "Ждать подтверждения"
            recommendation_class = "wait"

        if signal == "Ждать":
            confidence = "Низкая"
            recommendation_reason = "Импульс недостаточно согласован"
        else:
            if confirmations == 3 and abs(avg_m) >= 10:
                confidence = "Высокая"
            elif confirmations >= 2 and abs(avg_m) >= 5:
                confidence = "Средняя"
            else:
                confidence = "Низкая"
            recommendation_reason = (
                f"Импульс подтверждён на {confirmations} из 3 горизонтов"
            )

        risk_note = None
        if vol7 >= 8:
            confidence = _lower_confidence(confidence)
            risk_note = "Высокая волатильность: уменьшите размер позиции"
        elif vol7 >= 4:
            risk_note = "Повышенная волатильность: контролируйте размер позиции"
        
        results.append({
            "ticker": ticker,
            "pct_3d": round(float(p3), 2),
            "pct_7d": round(float(p7), 2),
            "pct_14d": round(float(p14), 2),
            "volatility_7d": round(float(vol7), 2),
            "trend": trend,
            "signal": signal,
            "recommendation": recommendation,
            "recommendation_class": recommendation_class,
            "confidence": confidence,
            "recommendation_reason": recommendation_reason,
            "risk_note": risk_note,
            "momentum_score": round(float(avg_m), 2),
            "scanner": "momentum",
        })
    
    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values("momentum_score", ascending=False, key=abs)
    return df


def drawdown_scan(prices: np.ndarray, tickers: List[str]) -> pd.DataFrame:
    """
    Find tickers in significant drawdown from 90-day high.
    
    Estimates historical recovery from similar drops.
    """
    if len(prices) < 90:
        return pd.DataFrame()
    
    results = []
    
    for i, ticker in enumerate(tickers):
        col = prices[:, i]
        ok = ~np.isnan(col) & (col > 0)
        if ok.sum() < 90:
            continue
        
        valid = col[ok]
        window = min(90, len(valid))
        recent = valid[-window:]
        
        high_90 = float(np.max(recent))
        current = float(recent[-1])
        dd_pct = (1 - current / high_90) * 100
        
        if dd_pct < 10:
            continue
        
        days_from_high = window - np.argmax(recent) - 1
        
        recoveries = []
        for start in range(0, len(valid) - 30):
            seg_high = float(valid[max(0, start - 10):start + 1].max())
            seg_dd = (1 - valid[start] / seg_high) * 100
            
            if abs(seg_dd - dd_pct) < 3:
                end = min(start + 30, len(valid))
                future_min = float(valid[start + 1:end].min())
                future_dd = (1 - future_min / valid[start]) * 100
                recoveries.append(future_dd)
        
        avg_recovery = float(np.mean(recoveries)) if recoveries else 0

        p3 = (
            (valid[-1] / valid[-4] - 1) * 100
            if len(valid) >= 4
            else 0
        )
        p7 = (
            (valid[-1] / valid[-8] - 1) * 100
            if len(valid) >= 8
            else 0
        )
        if p3 > 1 and p7 > 0:
            signal = "Лонг"
            recommendation = "Рассмотреть лонг"
            recommendation_class = "long"
            confidence = (
                "Высокая"
                if p3 >= 3 and p7 >= 3 and dd_pct < 30
                else "Средняя"
            )
            recommendation_reason = "Отскок подтверждается"
        elif p3 < 0 and p7 < 0:
            signal = "Ждать"
            recommendation = "Не входить"
            recommendation_class = "wait"
            confidence = "Высокая" if p3 <= -3 else "Средняя"
            recommendation_reason = "Падение продолжается"
        else:
            signal = "Ждать"
            recommendation = "Ждать подтверждения"
            recommendation_class = "wait"
            confidence = "Низкая"
            recommendation_reason = "Нет подтверждённого отскока"

        risk_note = (
            "Глубокая просадка: риск продолжения движения"
            if dd_pct >= 30
            else None
        )
        
        results.append({
            "ticker": ticker,
            "drawdown_pct": round(float(dd_pct), 2),
            "days_from_high": int(days_from_high),
            "high_90d": round(high_90, 4),
            "current": round(current, 4),
            "avg_historical_recovery": round(float(avg_recovery), 2),
            "pct_3d": round(float(p3), 2),
            "pct_7d": round(float(p7), 2),
            "signal": signal,
            "recommendation": recommendation,
            "recommendation_class": recommendation_class,
            "confidence": confidence,
            "recommendation_reason": recommendation_reason,
            "risk_note": risk_note,
            "scanner": "drawdown",
        })
    
    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values("drawdown_pct", ascending=False)
    return df
