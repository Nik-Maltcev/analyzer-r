"""Four isolated, point-in-time short-term crypto experiments."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import UTC, datetime
from threading import Lock

import numpy as np
import pandas as pd

from app.data.mexc_intraday import refresh_reversal_candles
from app.db.schema import (
    CREATE_REVERSAL_CANDLES,
    CREATE_SHORT_TERM_BACKTEST_TRADES,
    CREATE_SHORT_TERM_FORWARD_TRADES,
    CREATE_SHORT_TERM_RUNS,
)

CALCULATION_VERSION = "short-term-lab-v2"
STAKE_USD = 100.0
ROUND_TRIP_COST_PCT = 0.30
FIVE_MINUTES_MS = 5 * 60 * 1000
STRATEGIES = {
    "vwap_reversion": {
        "name": "VWAP Mean Reversion",
        "short_name": "Возврат к VWAP",
        "timeframe": 15,
        "hold": 4 * 60,
        "stop": 3.0,
        "target": 4.0,
        "description": "Входит после подтверждённого возврата от экстремального отклонения к объёмной средней.",
    },
    "liquidity_sweep": {
        "name": "Liquidity Sweep Reversal",
        "short_name": "Ложный пробой",
        "timeframe": 15,
        "hold": 4 * 60,
        "stop": 3.0,
        "target": 5.0,
        "description": "Ищет прокол локального экстремума с длинной тенью и возвратом цены внутрь диапазона.",
    },
    "volatility_squeeze": {
        "name": "Volatility Squeeze",
        "short_name": "Выход из сжатия",
        "timeframe": 15,
        "hold": 12 * 60,
        "stop": 4.0,
        "target": 7.0,
        "description": "Торгует выход из аномально узкого диапазона только при подтверждении объёмом.",
    },
    "btc_lead_lag": {
        "name": "BTC Lead-Lag",
        "short_name": "Запаздывание за BTC",
        "timeframe": 15,
        "hold": 3 * 60,
        "stop": 3.0,
        "target": 4.5,
        "description": "Ищет ликвидные монеты, которые запаздывают после сильного движения BTC.",
    },
}
_REFRESH_LOCK = Lock()


def ensure_short_term_schema(conn: sqlite3.Connection) -> None:
    for statement in (
        CREATE_REVERSAL_CANDLES,
        CREATE_SHORT_TERM_RUNS,
        CREATE_SHORT_TERM_BACKTEST_TRADES,
        CREATE_SHORT_TERM_FORWARD_TRADES,
    ):
        conn.execute(statement)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_short_term_forward_open
        ON short_term_forward_trades(calculation_version, strategy, ticker)
        WHERE status IN ('pending', 'active')
        """
    )
    conn.commit()


def _load_candles(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT ticker, open_time, open, high, low, close, volume, quote_volume
        FROM reversal_candles
        ORDER BY ticker, open_time
        """,
        conn,
    )
    if frame.empty:
        return frame
    for column in ("open", "high", "low", "close", "volume", "quote_volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["open", "high", "low", "close"])


def _aggregate(candles: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if candles.empty:
        return candles.copy()
    rule = f"{minutes}min"
    parts: list[pd.DataFrame] = []
    for ticker, group in candles.groupby("ticker", sort=False):
        indexed = group.copy()
        indexed["time"] = pd.to_datetime(indexed["open_time"], unit="ms", utc=True)
        indexed = indexed.set_index("time")
        result = indexed.resample(rule, label="left", closed="left").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "quote_volume": "sum",
        }).dropna(subset=["open", "high", "low", "close"])
        completed_count = indexed["open"].resample(
            rule, label="left", closed="left"
        ).count()
        result = result[completed_count >= minutes // 5]
        result["ticker"] = ticker
        result["open_time"] = (result.index.astype("int64") // 1_000_000).astype("int64")
        parts.append(result.reset_index(drop=True))
    return pd.concat(parts, ignore_index=True).sort_values(["open_time", "ticker"])


def _confidence(score: float) -> str:
    return "high" if abs(score) >= 2.0 else "medium"


def _candidate(
    strategy: str,
    ticker: str,
    direction: str,
    signal_time: int,
    signal_price: float,
    score: float,
) -> dict:
    settings = STRATEGIES[strategy]
    decision_time = int(signal_time + int(settings["timeframe"]) * 60_000)
    return {
        "strategy": strategy,
        "ticker": ticker,
        "direction": direction,
        # The signal only exists after its aggregate candle has fully closed.
        "signal_time": decision_time,
        "signal_price": float(signal_price),
        "score": float(score),
        "confidence": _confidence(float(score)),
        "timeframe_minutes": int(settings["timeframe"]),
        "hold_minutes": int(settings["hold"]),
        "stop_pct": float(settings["stop"]),
        "target_pct": float(settings["target"]),
    }


def _cap_per_time(frame: pd.DataFrame, count: int = 2) -> pd.DataFrame:
    if frame.empty:
        return frame
    return (
        frame.assign(abs_score=frame["score"].abs())
        .sort_values(["open_time", "direction", "abs_score"], ascending=[True, True, False])
        .groupby(["open_time", "direction"], sort=False)
        .head(count)
        .drop(columns="abs_score")
    )


def _cross_momentum(hourly: pd.DataFrame) -> list[dict]:
    close = hourly.pivot(index="open_time", columns="ticker", values="close")
    if close.empty:
        return []
    score = (
        close.pct_change(6, fill_method=None) * 0.20
        + close.pct_change(12, fill_method=None) * 0.30
        + close.pct_change(24, fill_method=None) * 0.50
    ) * 100
    mean = score.mean(axis=1)
    std = score.std(axis=1).replace(0, np.nan)
    zscore = score.sub(mean, axis=0).div(std, axis=0)
    btc = close.get("BTC/USD")
    btc_sma = btc.rolling(50, min_periods=50).mean() if btc is not None else None
    rows: list[dict] = []
    for position, timestamp in enumerate(score.index):
        if position % 4 != 0:
            continue
        for ticker, value in score.loc[timestamp].dropna().items():
            zvalue = zscore.at[timestamp, ticker]
            price = close.at[timestamp, ticker]
            if not all(math.isfinite(float(item)) for item in (value, zvalue, price)):
                continue
            long_allowed = btc is None or btc_sma is None or pd.isna(btc_sma.loc[timestamp]) or btc.loc[timestamp] >= btc_sma.loc[timestamp] * 0.99
            short_allowed = btc is None or btc_sma is None or pd.isna(btc_sma.loc[timestamp]) or btc.loc[timestamp] <= btc_sma.loc[timestamp] * 1.01
            direction = None
            if value >= 1.5 and zvalue >= 0.8 and long_allowed:
                direction = "long"
            elif value <= -1.5 and zvalue <= -0.8 and short_allowed:
                direction = "short"
            if direction:
                rows.append({
                    "open_time": int(timestamp), "ticker": ticker,
                    "direction": direction, "signal_price": float(price),
                    "score": float(zvalue),
                })
    selected = _cap_per_time(pd.DataFrame(rows))
    return [_candidate("cross_momentum", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _volatility_breakout(fifteen: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in fifteen.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        previous_close = frame["close"].shift(1)
        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean().shift(1)
        high = frame["high"].rolling(20, min_periods=20).max().shift(1)
        low = frame["low"].rolling(20, min_periods=20).min().shift(1)
        volume_base = frame["quote_volume"].rolling(20, min_periods=20).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base
        long_score = (frame["close"] - high) / atr.replace(0, np.nan)
        short_score = (low - frame["close"]) / atr.replace(0, np.nan)
        direction = np.where((long_score > 0.15) & (volume_ratio >= 1.4), "long", np.where((short_score > 0.15) & (volume_ratio >= 1.4), "short", ""))
        score = np.where(direction == "long", long_score + np.log1p(volume_ratio), short_score + np.log1p(volume_ratio))
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        out = out.rename(columns={"close": "signal_price"})
        rows.append(out)
    selected = _cap_per_time(pd.concat(rows, ignore_index=True) if rows else pd.DataFrame())
    return [_candidate("volatility_breakout", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _trend_pullback(fifteen: pd.DataFrame, hourly: pd.DataFrame) -> list[dict]:
    trend_parts: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time")[["open_time", "close"]].copy()
        frame["ema20h"] = frame["close"].ewm(span=20, adjust=False, min_periods=20).mean()
        frame["ema50h"] = frame["close"].ewm(span=50, adjust=False, min_periods=50).mean()
        # Hourly features become known only after the complete hourly candle closes.
        frame["available_time"] = frame["open_time"] + 60 * 60_000
        frame["ticker"] = ticker
        trend_parts.append(frame[["ticker", "available_time", "ema20h", "ema50h"]])
    trend = pd.concat(trend_parts, ignore_index=True)
    rows: list[pd.DataFrame] = []
    for ticker, group in fifteen.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        frame["decision_time"] = frame["open_time"] + 15 * 60_000
        hourly_trend = trend[trend["ticker"] == ticker].sort_values("available_time")
        frame = pd.merge_asof(
            frame,
            hourly_trend.drop(columns="ticker"),
            left_on="decision_time",
            right_on="available_time",
            direction="backward",
        )
        frame["ema20"] = frame["close"].ewm(span=20, adjust=False, min_periods=20).mean()
        previous_close = frame["close"].shift(1)
        previous_ema = frame["ema20"].shift(1)
        bullish = frame["ema20h"] > frame["ema50h"] * 1.002
        bearish = frame["ema20h"] < frame["ema50h"] * 0.998
        long_condition = bullish & (frame["low"] <= frame["ema20"] * 1.003) & (frame["close"] > frame["ema20"]) & (previous_close <= previous_ema)
        short_condition = bearish & (frame["high"] >= frame["ema20"] * 0.997) & (frame["close"] < frame["ema20"]) & (previous_close >= previous_ema)
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        trend_strength = ((frame["ema20h"] / frame["ema50h"] - 1).abs() * 100).clip(lower=0)
        score = 1.0 + trend_strength
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(pd.concat(rows, ignore_index=True) if rows else pd.DataFrame())
    return [_candidate("trend_pullback", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _residual_momentum(hourly: pd.DataFrame) -> list[dict]:
    close = hourly.pivot(index="open_time", columns="ticker", values="close")
    returns = close.pct_change(fill_method=None)
    btc = returns.get("BTC/USD")
    if btc is None:
        return []
    rows: list[dict] = []
    btc_variance = btc.rolling(72, min_periods=48).var().shift(1).replace(0, np.nan)
    for ticker in close.columns:
        if ticker == "BTC/USD":
            continue
        coin = returns[ticker]
        beta = coin.rolling(72, min_periods=48).cov(btc).shift(1) / btc_variance
        residual = coin - beta * btc
        cumulative = residual.rolling(12, min_periods=12).sum()
        baseline = cumulative.rolling(72, min_periods=48).std().shift(1).replace(0, np.nan)
        zscore = cumulative / baseline
        for position, timestamp in enumerate(close.index):
            if position % 4 != 0:
                continue
            value = zscore.loc[timestamp]
            price = close.at[timestamp, ticker]
            if pd.isna(value) or pd.isna(price) or abs(float(value)) < 1.3:
                continue
            rows.append({
                "open_time": int(timestamp), "ticker": ticker,
                "direction": "long" if value > 0 else "short",
                "signal_price": float(price), "score": float(value),
            })
    selected = _cap_per_time(pd.DataFrame(rows))
    return [_candidate("residual_momentum", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _vwap_reversion(fifteen: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in fifteen.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        typical = (frame["high"] + frame["low"] + frame["close"]) / 3
        weight = frame["volume"].replace(0, np.nan)
        weighted = typical * weight
        rolling_weight = weight.rolling(96, min_periods=48).sum().shift(1)
        vwap = weighted.rolling(96, min_periods=48).sum().shift(1) / rolling_weight
        deviation = (frame["close"] / vwap - 1.0) * 100
        scale = deviation.rolling(96, min_periods=48).std().shift(1).replace(0, np.nan)
        zscore = deviation / scale
        previous = zscore.shift(1)
        long_condition = (previous <= -2.0) & (zscore > -1.5) & (frame["close"] > frame["open"])
        short_condition = (previous >= 2.0) & (zscore < 1.5) & (frame["close"] < frame["open"])
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = previous[direction != ""].abs()
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(pd.concat(rows, ignore_index=True) if rows else pd.DataFrame())
    return [_candidate("vwap_reversion", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _liquidity_sweep(fifteen: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in fifteen.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        previous_close = frame["close"].shift(1)
        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean().shift(1).replace(0, np.nan)
        range_high = frame["high"].rolling(48, min_periods=32).max().shift(1)
        range_low = frame["low"].rolling(48, min_periods=32).min().shift(1)
        body = (frame["close"] - frame["open"]).abs().clip(lower=atr * 0.05)
        lower_wick = np.minimum(frame["open"], frame["close"]) - frame["low"]
        upper_wick = frame["high"] - np.maximum(frame["open"], frame["close"])
        volume_base = frame["quote_volume"].rolling(48, min_periods=32).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base
        long_condition = (
            (frame["low"] < range_low - atr * 0.10)
            & (frame["close"] > range_low)
            & (lower_wick >= body * 1.5)
            & (volume_ratio >= 1.2)
        )
        short_condition = (
            (frame["high"] > range_high + atr * 0.10)
            & (frame["close"] < range_high)
            & (upper_wick >= body * 1.5)
            & (volume_ratio >= 1.2)
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        sweep_distance = np.where(
            direction == "long",
            (range_low - frame["low"]) / atr,
            (frame["high"] - range_high) / atr,
        )
        wick_ratio = np.where(direction == "long", lower_wick / body, upper_wick / body)
        score = sweep_distance + np.log1p(volume_ratio) + np.minimum(wick_ratio, 5) / 5
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(pd.concat(rows, ignore_index=True) if rows else pd.DataFrame())
    return [_candidate("liquidity_sweep", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _volatility_squeeze(fifteen: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in fifteen.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        middle = frame["close"].rolling(20, min_periods=20).mean()
        deviation = frame["close"].rolling(20, min_periods=20).std()
        width = (4 * deviation / middle.replace(0, np.nan)).abs()
        squeeze_limit = width.rolling(192, min_periods=96).quantile(0.20).shift(1)
        was_squeezed = width.shift(1) <= squeeze_limit
        range_high = frame["high"].rolling(20, min_periods=20).max().shift(1)
        range_low = frame["low"].rolling(20, min_periods=20).min().shift(1)
        previous_close = frame["close"].shift(1)
        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean().shift(1).replace(0, np.nan)
        volume_base = frame["quote_volume"].rolling(20, min_periods=20).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base
        long_condition = was_squeezed & (frame["close"] > range_high) & (volume_ratio >= 1.3)
        short_condition = was_squeezed & (frame["close"] < range_low) & (volume_ratio >= 1.3)
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        distance = np.where(
            direction == "long",
            (frame["close"] - range_high) / atr,
            (range_low - frame["close"]) / atr,
        )
        score = distance + np.log1p(volume_ratio)
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(pd.concat(rows, ignore_index=True) if rows else pd.DataFrame())
    return [_candidate("volatility_squeeze", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _btc_lead_lag(fifteen: pd.DataFrame) -> list[dict]:
    close = fifteen.pivot(index="open_time", columns="ticker", values="close")
    returns = close.pct_change(fill_method=None)
    btc = returns.get("BTC/USD")
    if btc is None:
        return []
    btc_variance = btc.rolling(672, min_periods=192).var().shift(1).replace(0, np.nan)
    rows: list[dict] = []
    for ticker in close.columns:
        if ticker == "BTC/USD":
            continue
        coin = returns[ticker]
        beta = coin.rolling(672, min_periods=192).cov(btc).shift(1) / btc_variance
        correlation = coin.rolling(672, min_periods=192).corr(btc).shift(1)
        residual = coin - beta * btc
        residual_scale = residual.rolling(672, min_periods=192).std().shift(1).replace(0, np.nan)
        gap = beta * btc - coin
        score = gap.abs() / residual_scale
        long_condition = (btc >= 0.008) & (gap >= 0.004) & (correlation >= 0.50) & (score >= 1.0)
        short_condition = (btc <= -0.008) & (gap <= -0.004) & (correlation >= 0.50) & (score >= 1.0)
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        selected = direction != ""
        for timestamp in close.index[selected]:
            price = close.at[timestamp, ticker]
            value = score.loc[timestamp]
            if pd.isna(price) or pd.isna(value):
                continue
            rows.append({
                "open_time": int(timestamp),
                "ticker": ticker,
                "direction": str(direction[close.index.get_loc(timestamp)]),
                "signal_price": float(price),
                "score": float(value),
            })
    selected = _cap_per_time(pd.DataFrame(rows))
    return [_candidate("btc_lead_lag", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def generate_candidates(candles: pd.DataFrame) -> dict[str, list[dict]]:
    fifteen = _aggregate(candles, 15)
    return {
        "vwap_reversion": _vwap_reversion(fifteen),
        "liquidity_sweep": _liquidity_sweep(fifteen),
        "volatility_squeeze": _volatility_squeeze(fifteen),
        "btc_lead_lag": _btc_lead_lag(fifteen),
    }


def _directional_return(direction: str, entry: float, price: float) -> float:
    if direction == "long":
        return (price / entry - 1.0) * 100
    return ((entry - price) / entry) * 100


def _simulate(candidate: dict, ticker_bars: pd.DataFrame) -> dict | None:
    bars = ticker_bars.sort_values("open_time")
    times = bars["open_time"].to_numpy(dtype=np.int64)
    entry_index = int(np.searchsorted(times, candidate["signal_time"], side="left"))
    if entry_index >= len(bars):
        return None
    entry = bars.iloc[entry_index]
    entry_price = float(entry["open"])
    horizon = int(times[entry_index] + candidate["hold_minutes"] * 60_000)
    exit_index = int(np.searchsorted(times, horizon, side="left"))
    if exit_index <= entry_index or exit_index >= len(bars):
        return None
    stop = float(candidate["stop_pct"])
    target = float(candidate["target_pct"])
    chosen = exit_index
    reason = "time"
    # A timed exit happens at the first tradable open on the horizon boundary.
    exit_price = float(bars.iloc[chosen]["open"])
    for index in range(entry_index, exit_index):
        bar = bars.iloc[index]
        if candidate["direction"] == "long":
            stop_hit = float(bar["low"]) <= entry_price * (1 - stop / 100)
            target_hit = float(bar["high"]) >= entry_price * (1 + target / 100)
            stop_price = entry_price * (1 - stop / 100)
            target_price = entry_price * (1 + target / 100)
        else:
            stop_hit = float(bar["high"]) >= entry_price * (1 + stop / 100)
            target_hit = float(bar["low"]) <= entry_price * (1 - target / 100)
            stop_price = entry_price * (1 + stop / 100)
            target_price = entry_price * (1 - target / 100)
        if stop_hit:
            chosen, reason, exit_price = index, "stop", stop_price
            break
        if target_hit:
            chosen, reason, exit_price = index, "target", target_price
            break
    gross = _directional_return(candidate["direction"], entry_price, exit_price)
    net = gross - ROUND_TRIP_COST_PCT
    return {
        **candidate,
        "entry_time": int(times[entry_index]), "entry_price": entry_price,
        "exit_time": int(times[chosen]), "exit_price": exit_price,
        "exit_reason": reason, "gross_return_pct": gross,
        "cost_pct": ROUND_TRIP_COST_PCT, "net_return_pct": net,
        "cash_result": STAKE_USD * net / 100,
    }


def backtest(candles: pd.DataFrame, candidates: dict[str, list[dict]]) -> tuple[list[dict], dict]:
    by_ticker = {ticker: group for ticker, group in candles.groupby("ticker", sort=False)}
    trades: list[dict] = []
    for strategy, events in candidates.items():
        next_free: dict[str, int] = {}
        for event in sorted(events, key=lambda item: item["signal_time"]):
            if event["signal_time"] < next_free.get(event["ticker"], 0):
                continue
            ticker_bars = by_ticker.get(event["ticker"])
            if ticker_bars is None:
                continue
            trade = _simulate(event, ticker_bars)
            if trade is None:
                continue
            trades.append(trade)
            next_free[event["ticker"]] = int(trade["exit_time"] + FIVE_MINUTES_MS)
    metrics: dict[str, dict] = {}
    for strategy in STRATEGIES:
        subset = [trade for trade in trades if trade["strategy"] == strategy]
        values = [float(trade["cash_result"]) for trade in subset]
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        metrics[strategy] = {
            "trades": len(subset),
            "wins": len(wins),
            "win_rate": len(wins) / len(subset) * 100 if subset else 0.0,
            "net_cash": sum(values),
            "average_net_pct": sum(float(trade["net_return_pct"]) for trade in subset) / len(subset) if subset else 0.0,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else (999.0 if wins else 0.0),
        }
    return trades, metrics


def _advance_forward(conn: sqlite3.Connection, candles: pd.DataFrame) -> dict:
    conn.row_factory = sqlite3.Row
    opened = closed = 0
    by_ticker = {ticker: group.sort_values("open_time") for ticker, group in candles.groupby("ticker", sort=False)}
    rows = conn.execute(
        """
        SELECT * FROM short_term_forward_trades
        WHERE calculation_version=? AND status IN ('pending', 'active')
        ORDER BY signal_time, id
        """,
        (CALCULATION_VERSION,),
    ).fetchall()
    for source in rows:
        trade = dict(source)
        bars = by_ticker.get(trade["ticker"])
        if bars is None or bars.empty:
            continue
        if trade["status"] == "pending":
            candidates = bars[bars["open_time"] >= int(trade["signal_time"])]
            if candidates.empty:
                continue
            entry = candidates.iloc[0]
            entry_time = int(entry["open_time"])
            entry_price = float(entry["open"])
            conn.execute(
                """
                UPDATE short_term_forward_trades
                SET status='active', entry_time=?, entry_price=?, planned_exit_time=?,
                    last_evaluated_time=?, last_price=?, updated_at=datetime('now')
                WHERE id=? AND status='pending'
                """,
                (entry_time, entry_price, entry_time + int(trade["hold_minutes"]) * 60_000,
                 entry_time - 1, entry_price, trade["id"]),
            )
            trade.update({"status": "active", "entry_time": entry_time, "entry_price": entry_price,
                          "planned_exit_time": entry_time + int(trade["hold_minutes"]) * 60_000,
                          "last_evaluated_time": entry_time - 1})
            opened += 1
        unseen = bars[bars["open_time"] > int(trade["last_evaluated_time"] or trade["entry_time"] - 1)]
        for _, bar in unseen.iterrows():
            timestamp = int(bar["open_time"])
            entry_price = float(trade["entry_price"])
            stop = float(trade["stop_pct"])
            target = float(trade["target_pct"])
            direction = trade["direction"]
            if timestamp >= int(trade["planned_exit_time"]):
                reason = "time"
                price = float(bar["open"])
                stop_hit = target_hit = False
                stop_price = target_price = price
            elif direction == "long":
                stop_hit = float(bar["low"]) <= entry_price * (1 - stop / 100)
                target_hit = float(bar["high"]) >= entry_price * (1 + target / 100)
                stop_price = entry_price * (1 - stop / 100)
                target_price = entry_price * (1 + target / 100)
            else:
                stop_hit = float(bar["high"]) >= entry_price * (1 + stop / 100)
                target_hit = float(bar["low"]) <= entry_price * (1 - target / 100)
                stop_price = entry_price * (1 + stop / 100)
                target_price = entry_price * (1 - target / 100)
            if timestamp < int(trade["planned_exit_time"]):
                reason = None
                price = float(bar["close"])
            if reason == "time":
                pass
            elif stop_hit:
                reason, price = "stop", stop_price
            elif target_hit:
                reason, price = "target", target_price
            gross = _directional_return(direction, entry_price, price)
            net = gross - ROUND_TRIP_COST_PCT
            if reason:
                conn.execute(
                    """
                    UPDATE short_term_forward_trades
                    SET status='closed', last_evaluated_time=?, last_price=?,
                        current_net_return_pct=?, current_cash_result=?, exit_time=?,
                        exit_price=?, exit_reason=?, gross_return_pct=?, cost_pct=?,
                        net_return_pct=?, cash_result=?, updated_at=datetime('now')
                    WHERE id=? AND status='active'
                    """,
                    (timestamp, price, net, STAKE_USD * net / 100, timestamp, price,
                     reason, gross, ROUND_TRIP_COST_PCT, net, STAKE_USD * net / 100,
                     trade["id"]),
                )
                closed += 1
                break
            conn.execute(
                """
                UPDATE short_term_forward_trades
                SET last_evaluated_time=?, last_price=?, current_net_return_pct=?,
                    current_cash_result=?, updated_at=datetime('now')
                WHERE id=? AND status='active'
                """,
                (timestamp, price, net, STAKE_USD * net / 100, trade["id"]),
            )
    conn.commit()
    return {"opened": opened, "closed": closed}


def _insert_latest_candidates(
    conn: sqlite3.Connection,
    candidates: dict[str, list[dict]],
    data_available_until: int,
) -> int:
    inserted = 0
    for strategy, events in candidates.items():
        if not events:
            continue
        latest_events = [
            event for event in events
            if int(event["signal_time"]) == int(data_available_until)
        ]
        for event in latest_events:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO short_term_forward_trades (
                    calculation_version, strategy, ticker, direction, signal_time,
                    signal_price, score, confidence, timeframe_minutes,
                    hold_minutes, stop_pct, target_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (CALCULATION_VERSION, event["strategy"], event["ticker"], event["direction"],
                 event["signal_time"], event["signal_price"], event["score"],
                 event["confidence"], event["timeframe_minutes"], event["hold_minutes"],
                 event["stop_pct"], event["target_pct"]),
            )
            inserted += conn.total_changes - before
    conn.commit()
    return inserted


async def _refresh_short_term_lab(db_path: str, *, include_backtest: bool = True) -> dict:
    with _REFRESH_LOCK:
        conn = sqlite3.connect(db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        ensure_short_term_schema(conn)
        conn.execute(
            """
            UPDATE short_term_runs
            SET status='failed', completed_at=datetime('now'),
                error='Calculation interrupted before completion'
            WHERE calculation_version=? AND status='running'
            """,
            (CALCULATION_VERSION,),
        )
        run_id = conn.execute(
            "INSERT INTO short_term_runs(calculation_version, status) VALUES (?, 'running')",
            (CALCULATION_VERSION,),
        ).lastrowid
        conn.commit()
        try:
            refresh = await refresh_reversal_candles(conn)
            candles = _load_candles(conn)
            if candles.empty:
                raise RuntimeError("MEXC did not return completed 5-minute candles")
            latest = int(candles["open_time"].max())
            age_minutes = (datetime.now(UTC).timestamp() * 1000 - latest) / 60_000
            if age_minutes > 20:
                raise RuntimeError(f"MEXC candles are stale by {age_minutes:.0f} minutes")
            latest_by_ticker = candles.groupby("ticker")["open_time"].max()
            fresh_tickers = int((latest_by_ticker >= latest - 10 * 60_000).sum())
            btc_latest = int(latest_by_ticker.get("BTC/USD", 0))
            if fresh_tickers < 24 or btc_latest < latest - 10 * 60_000:
                raise RuntimeError(
                    "MEXC coverage is incomplete: "
                    f"{fresh_tickers}/30 fresh tickers, BTC fresh={btc_latest >= latest - 10 * 60_000}"
                )
            forward = _advance_forward(conn, candles)
            live_cutoff = latest - 10 * 24 * 60 * 60 * 1000
            live_candidates = generate_candidates(candles[candles["open_time"] >= live_cutoff])
            inserted = _insert_latest_candidates(
                conn,
                live_candidates,
                data_available_until=latest + FIVE_MINUTES_MS,
            )
            trades: list[dict] = []
            metrics: dict = {}
            if include_backtest:
                historical_candidates = generate_candidates(candles)
                trades, metrics = backtest(candles, historical_candidates)
            else:
                previous = conn.execute(
                    """
                    SELECT metrics_json FROM short_term_runs
                    WHERE calculation_version=? AND status='completed'
                      AND metrics_json IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (CALCULATION_VERSION,),
                ).fetchone()
                if previous:
                    metrics = json.loads(previous[0] or "{}").get("strategies", {})
            conn.executemany(
                """
                INSERT OR IGNORE INTO short_term_backtest_trades (
                    run_id, strategy, ticker, direction, signal_time, entry_time,
                    entry_price, exit_time, exit_price, exit_reason, score, confidence,
                    gross_return_pct, cost_pct, net_return_pct, cash_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(run_id, trade["strategy"], trade["ticker"], trade["direction"],
                  trade["signal_time"], trade["entry_time"], trade["entry_price"],
                  trade["exit_time"], trade["exit_price"], trade["exit_reason"],
                  trade["score"], trade["confidence"], trade["gross_return_pct"],
                  trade["cost_pct"], trade["net_return_pct"], trade["cash_result"])
                 for trade in trades],
            )
            payload = {"strategies": metrics, "forward": {**forward, "pending": inserted}}
            conn.execute(
                """
                UPDATE short_term_runs
                SET status='completed', completed_at=datetime('now'), data_start=?,
                    data_end=?, candle_count=?, metrics_json=? WHERE id=?
                """,
                (int(candles["open_time"].min()), latest, len(candles), json.dumps(payload), run_id),
            )
            conn.commit()
            return {"status": "completed", "run_id": run_id, "refresh": refresh, **payload}
        except Exception as exc:
            conn.execute(
                "UPDATE short_term_runs SET status='failed', completed_at=datetime('now'), error=? WHERE id=?",
                (str(exc)[:500], run_id),
            )
            conn.commit()
            raise
        finally:
            conn.close()


async def refresh_short_term_lab(db_path: str, *, include_backtest: bool = True) -> dict:
    """Run network and CPU-heavy research outside the FastAPI event loop."""
    return await asyncio.to_thread(
        lambda: asyncio.run(
            _refresh_short_term_lab(db_path, include_backtest=include_backtest)
        )
    )


def _format_time(value: int | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value / 1000, UTC).strftime("%d.%m %H:%M")


def get_short_term_report(db_path: str) -> dict:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_short_term_schema(conn)
    latest = conn.execute(
        """
        SELECT * FROM short_term_runs
        WHERE calculation_version=? ORDER BY id DESC LIMIT 1
        """,
        (CALCULATION_VERSION,),
    ).fetchone()
    completed = conn.execute(
        """
        SELECT * FROM short_term_runs
        WHERE calculation_version=? AND status='completed'
        ORDER BY id DESC LIMIT 1
        """,
        (CALCULATION_VERSION,),
    ).fetchone()
    metrics = json.loads(completed["metrics_json"] or "{}") if completed else {}
    open_rows = conn.execute(
        """
        SELECT * FROM short_term_forward_trades
        WHERE calculation_version=? AND status IN ('pending', 'active')
        ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, strategy, ABS(score) DESC
        """,
        (CALCULATION_VERSION,),
    ).fetchall()
    closed_rows = conn.execute(
        """
        SELECT * FROM short_term_forward_trades
        WHERE calculation_version=? AND status='closed'
        ORDER BY exit_time DESC, id DESC LIMIT 80
        """,
        (CALCULATION_VERSION,),
    ).fetchall()
    conn.close()

    def trade_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["strategy_name"] = STRATEGIES[item["strategy"]]["short_name"]
        item["signal_label"] = _format_time(item.get("signal_time"))
        item["entry_label"] = _format_time(item.get("entry_time"))
        item["exit_label"] = _format_time(item.get("exit_time"))
        item["planned_exit_label"] = _format_time(item.get("planned_exit_time"))
        return item

    latest_dict = dict(latest) if latest else None
    if latest_dict:
        latest_dict["data_label"] = _format_time(latest_dict.get("data_end"))
    completed_dict = dict(completed) if completed else None
    if completed_dict:
        completed_dict["data_label"] = _format_time(completed_dict.get("data_end"))
    strategy_metrics = metrics.get("strategies", {})
    strategy_cards = []
    for key, settings in STRATEGIES.items():
        strategy_cards.append({"key": key, **settings, **strategy_metrics.get(key, {})})
    return {
        "version": CALCULATION_VERSION,
        "latest": latest_dict,
        "completed": completed_dict,
        "is_ready": completed is not None,
        "strategies": strategy_cards,
        "open": [trade_dict(row) for row in open_rows],
        "closed": [trade_dict(row) for row in closed_rows],
        "settings": {"stake": STAKE_USD, "cost_pct": ROUND_TRIP_COST_PCT},
    }
