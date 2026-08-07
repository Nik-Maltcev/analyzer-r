"""Isolated, point-in-time short-term crypto experiments."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import UTC, datetime
from threading import Lock

import numpy as np
import pandas as pd

from app.data.mexc_futures import (
    refresh_short_term_funding_rates,
    refresh_short_term_perp_candles,
)
from app.data.mexc_intraday import (
    INTRADAY_TICKERS,
    refresh_reversal_candles,
    refresh_short_term_execution_candles,
    refresh_short_term_hourly_candles,
)
from app.db.schema import (
    CREATE_REVERSAL_CANDLES,
    CREATE_SHORT_TERM_HOURLY_CANDLES,
    CREATE_SHORT_TERM_BACKTEST_TRADES,
    CREATE_SHORT_TERM_FORWARD_TRADES,
    CREATE_SHORT_TERM_FUNDING_RATES,
    CREATE_SHORT_TERM_PERP_CANDLES,
    CREATE_SHORT_TERM_RUNS,
)

CALCULATION_VERSION = "short-term-lab-v55"
STAKE_USD = 100.0
ROUND_TRIP_COST_PCT = 0.30
FIVE_MINUTES_MS = 5 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
BACKTEST_WINDOWS_DAYS = (30, 90, 180, 365)
STRATEGIES = {
    "rs_low_cost": {
        "name": "RS vs BTC (low cost 0.10%)",
        "short_name": "RS low cost",
        "timeframe": 60,
        "hold": 24 * 60,
        "stop": 4.0,
        "target": 6.0,
        "cost_pct": 0.10,
        "description": "Baseline: cost 0.10% round-trip, выход по времени 24ч.",
    },
    "rs_low_liquidity": {
        "name": "RS vs BTC (liquidity ≥$10M/24h)",
        "short_name": "RS liquidity",
        "timeframe": 60,
        "hold": 24 * 60,
        "stop": 4.0,
        "target": 6.0,
        "cost_pct": 0.10,
        "description": "Фильтр ликвидности: quote_volume 24ч ≥ $10M.",
    },
    "rs_regime_filter": {
        "name": "RS vs BTC (market regime)",
        "short_name": "RS regime",
        "timeframe": 60,
        "hold": 24 * 60,
        "stop": 4.0,
        "target": 6.0,
        "cost_pct": 0.10,
        "description": "Только LONG при BTC ≥0% (24ч), только SHORT при BTC ≤0%. Направленческий фильтр.",
    },
    "momentum": {
        "name": "Momentum (multi-timeframe)",
        "short_name": "Momentum",
        "timeframe": 60,
        "hold": 24 * 60,
        "stop": 4.0,
        "target": 6.0,
        "cost_pct": 0.10,
        "description": "Мульти-таймфрейм импульс: 3д/7д/14д → long при сильном росте, short при сильном падении.",
    },
    "drawdown": {
        "name": "Drawdown mean reversion",
        "short_name": "Drawdown",
        "timeframe": 60,
        "hold": 24 * 60,
        "stop": 4.0,
        "target": 6.0,
        "cost_pct": 0.10,
        "description": "Лонг на отскок от 90-дневного максимума: просадка ≥10% + подтверждённый отскок.",
    },
}
_REFRESH_LOCK = Lock()


def ensure_short_term_schema(conn: sqlite3.Connection) -> None:
    for statement in (
        CREATE_REVERSAL_CANDLES,
        CREATE_SHORT_TERM_HOURLY_CANDLES,
        CREATE_SHORT_TERM_FUNDING_RATES,
        CREATE_SHORT_TERM_PERP_CANDLES,
        CREATE_SHORT_TERM_RUNS,
        CREATE_SHORT_TERM_BACKTEST_TRADES,
        CREATE_SHORT_TERM_FORWARD_TRADES,
    ):
        conn.execute(statement)
    migrations = {
        "short_term_backtest_trades": {
            "hedge_ticker": "TEXT",
            "hedge_direction": "TEXT",
            "hedge_ratio": "REAL",
            "hedge_entry_price": "REAL",
            "hedge_exit_price": "REAL",
        },
        "short_term_forward_trades": {
            "hedge_ticker": "TEXT",
            "hedge_direction": "TEXT",
            "hedge_ratio": "REAL",
            "hedge_entry_price": "REAL",
            "hedge_last_price": "REAL",
            "hedge_exit_price": "REAL",
        },
    }
    for table, columns in migrations.items():
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, declaration in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_short_term_forward_open
        ON short_term_forward_trades(calculation_version, strategy, ticker)
        WHERE status IN ('pending', 'active')
        """
    )
    conn.commit()


def _load_candles(conn: sqlite3.Connection, *, since_ms: int | None = None) -> pd.DataFrame:
    if since_ms is not None:
        frame = pd.read_sql_query(
            """
            SELECT ticker, open_time, open, high, low, close, volume, quote_volume
            FROM reversal_candles
            WHERE open_time >= ?
            ORDER BY ticker, open_time
            """,
            conn,
            params=(since_ms,),
        )
    else:
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


def _load_hourly_candles(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT ticker, open_time, open, high, low, close, volume, quote_volume
        FROM short_term_hourly_candles
        ORDER BY ticker, open_time
        """,
        conn,
    )
    if frame.empty:
        return frame
    for column in ("open", "high", "low", "close", "volume", "quote_volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["open", "high", "low", "close"])


def _load_funding_rates(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT ticker, settle_time, funding_rate
        FROM short_term_funding_rates
        ORDER BY ticker, settle_time
        """,
        conn,
    )
    if frame.empty:
        return frame
    frame["funding_rate"] = pd.to_numeric(frame["funding_rate"], errors="coerce")
    return frame.dropna(subset=["funding_rate"])


def _load_perp_candles(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT ticker, open_time, open, high, low, close, volume, quote_volume
        FROM short_term_perp_candles
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
    **extra,
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
        "cost_pct": float(settings.get("cost_pct", ROUND_TRIP_COST_PCT)),
        "trailing_stop_pct": float(settings.get("trailing_stop", 0.0)),
        **extra,
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


def _dual_momentum(hourly: pd.DataFrame) -> list[dict]:
    """Combine 24h absolute momentum with point-in-time market ranking."""
    snapshots: list[pd.DataFrame] = []
    for ticker, source in hourly.groupby("ticker", sort=False):
        frame = (
            source.sort_values("open_time")
            .drop_duplicates("open_time", keep="last")
            .set_index("open_time")
        )
        if len(frame) < 25:
            continue

        # Reindexing makes a missing clock hour invalidate the 24h return.
        full_index = pd.RangeIndex(
            int(frame.index.min()), int(frame.index.max()) + HOUR_MS, HOUR_MS
        )
        close = pd.to_numeric(frame["close"], errors="coerce").reindex(full_index)
        momentum_24h = close.pct_change(24, fill_method=None)
        complete_24h = close.rolling(25, min_periods=25).count().eq(25)
        eligible = pd.DataFrame({
            "signal_time": full_index,
            "signal_price": close.to_numpy(),
            "momentum_24h": momentum_24h.to_numpy(),
            "complete_24h": complete_24h.to_numpy(),
        })
        eligible = eligible[
            ((eligible["signal_time"] + HOUR_MS) % (6 * HOUR_MS) == 0)
            & eligible["complete_24h"]
            & eligible["signal_price"].gt(0)
            & np.isfinite(eligible["signal_price"])
            & np.isfinite(eligible["momentum_24h"])
        ].copy()
        eligible["ticker"] = ticker
        snapshots.append(eligible)

    if not snapshots:
        return []

    market = pd.concat(snapshots, ignore_index=True)
    rows: list[dict] = []
    for signal_time, cross_section in market.groupby("signal_time", sort=True):
        cross_section = cross_section.sort_values(
            ["momentum_24h", "ticker"], kind="mergesort"
        ).reset_index(drop=True)
        count = len(cross_section)
        if count < 20:
            continue
        tail_size = max(1, math.ceil(count * 0.10))
        dispersion = float(cross_section["momentum_24h"].std(ddof=1))
        center = float(cross_section["momentum_24h"].mean())
        if not math.isfinite(dispersion) or dispersion <= 0:
            continue

        tails = pd.concat([
            cross_section.head(tail_size).assign(direction="short"),
            cross_section.tail(tail_size).assign(direction="long"),
        ])
        tails = tails[
            ((tails["direction"] == "long") & tails["momentum_24h"].gt(0))
            | ((tails["direction"] == "short") & tails["momentum_24h"].lt(0))
        ]
        for row in tails.to_dict("records"):
            z_score = (float(row["momentum_24h"]) - center) / dispersion
            rows.append({
                "ticker": row["ticker"],
                "direction": row["direction"],
                "signal_time": int(signal_time),
                "signal_price": float(row["signal_price"]),
                "score": z_score,
                "momentum_24h_pct": float(row["momentum_24h"] * 100),
                "market_size": count,
                "tail_size": tail_size,
            })
    return [_candidate("dual_momentum", **row) for row in rows]


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


def _trend_persistence(hourly: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    btc = hourly[hourly["ticker"] == "BTC/USD"].set_index("open_time")["close"]
    btc_return_24 = btc.pct_change(24, fill_method=None) * 100
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        ema24 = frame["close"].ewm(span=24, adjust=False, min_periods=24).mean()
        ema72 = frame["close"].ewm(span=72, adjust=False, min_periods=72).mean()
        return_6 = frame["close"].pct_change(6, fill_method=None) * 100
        return_24 = frame["close"].pct_change(24, fill_method=None) * 100
        return_72 = frame["close"].pct_change(72, fill_method=None) * 100
        volume_base = frame["quote_volume"].rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base
        btc_context = frame["open_time"].map(btc_return_24).fillna(0.0)
        decision_time = frame["open_time"] + 60 * 60_000
        cadence = decision_time.mod(6 * 60 * 60_000).eq(0)
        long_condition = (
            cadence & (ema24 > ema72 * 1.015) & (frame["close"] > ema24)
            & (return_6 >= 0.6) & (return_24 >= 3.0) & (return_72 >= 5.0)
            & (volume_ratio >= 1.10) & (btc_context >= -3.0)
        )
        short_condition = (
            cadence & (ema24 < ema72 * 0.985) & (frame["close"] < ema24)
            & (return_6 <= -0.6) & (return_24 <= -3.0) & (return_72 <= -5.0)
            & (volume_ratio >= 1.10) & (btc_context <= 3.0)
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        trend_strength = (ema24 / ema72 - 1.0).abs() * 100
        score = trend_strength + return_24.abs() / 3 + np.log1p(volume_ratio)
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), count=1
    )
    return [_candidate("momentum_breakout", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _rs_btc_signals(
    hourly: pd.DataFrame,
    *,
    z_thresh: float = 0.5,
    r_6h: float = 0.4,
    r_24h: float = 1.5,
    rel_6h: float = 0.3,
    rel_24h: float = 1.0,
    use_72h: bool = False,
    r_72h: float = 4.0,
    use_mtf: bool = False,
    r_12h: float = 0.7,
    r_48h: float = 2.5,
) -> list[dict]:
    """Raw RS-vs-BTC signals without strategy/cost labels.

    Each event dict has fields ready for `_candidate(...)`: signal_time
    (the hourly candle open_time), ticker, direction, signal_price, score.
    The 60-minute execution delay is added later by `_candidate`.
    """
    if hourly.empty:
        return []
    close = hourly.pivot(index="open_time", columns="ticker", values="close").sort_index()
    if "BTC/USD" not in close.columns:
        return []
    return_6h = close.pct_change(6, fill_method=None) * 100
    return_24h = close.pct_change(24, fill_method=None) * 100
    relative_6h = return_6h.sub(return_6h["BTC/USD"], axis=0)
    relative_24h = return_24h.sub(return_24h["BTC/USD"], axis=0)
    relative_mean = relative_24h.mean(axis=1)
    relative_std = relative_24h.std(axis=1).replace(0, np.nan)
    relative_z = relative_24h.sub(relative_mean, axis=0).div(relative_std, axis=0)
    cadence = pd.Series(
        (close.index.to_numpy(dtype=np.int64) // (60 * 60_000)) % 4 == 0,
        index=close.index,
    )
    optional_returns: dict[str, pd.Series] = {}
    if use_72h:
        optional_returns["r72"] = close.pct_change(72, fill_method=None) * 100
    if use_mtf:
        optional_returns["r12"] = close.pct_change(12, fill_method=None) * 100
        optional_returns["r48"] = close.pct_change(48, fill_method=None) * 100
    rows: list[dict] = []
    for ticker in close.columns:
        if ticker == "BTC/USD":
            continue
        long_cond = (
            cadence
            & (return_6h[ticker] >= r_6h)
            & (return_24h[ticker] >= r_24h)
            & (relative_6h[ticker] >= rel_6h)
            & (relative_24h[ticker] >= rel_24h)
            & (relative_z[ticker] >= z_thresh)
        )
        short_cond = (
            cadence
            & (return_6h[ticker] <= -r_6h)
            & (return_24h[ticker] <= -r_24h)
            & (relative_6h[ticker] <= -rel_6h)
            & (relative_24h[ticker] <= -rel_24h)
            & (relative_z[ticker] <= -z_thresh)
        )
        if "r72" in optional_returns:
            r72 = optional_returns["r72"][ticker]
            long_cond &= (r72 >= r_72h)
            short_cond &= (r72 <= -r_72h)
        if "r12" in optional_returns:
            r12 = optional_returns["r12"][ticker]
            long_cond &= (r12 >= r_12h)
            short_cond &= (r12 <= -r_12h)
        if "r48" in optional_returns:
            r48 = optional_returns["r48"][ticker]
            long_cond &= (r48 >= r_48h)
            short_cond &= (r48 <= -r_48h)
        direction = np.where(long_cond, "long", np.where(short_cond, "short", ""))
        selected = direction != ""
        score = relative_z[ticker].abs() + relative_24h[ticker].abs() / 3
        for timestamp in close.index[selected]:
            price = close.at[timestamp, ticker]
            value = score.loc[timestamp]
            if pd.isna(price) or pd.isna(value):
                continue
            rows.append({
                "signal_time": int(timestamp),
                "ticker": ticker,
                "direction": str(direction[close.index.get_loc(timestamp)]),
                "signal_price": float(price),
                "score": float(value),
            })
    return rows


def _liquidity_filter(signals: list[dict], hourly: pd.DataFrame, *, min_quote_24h: float) -> list[dict]:
    """Drop signals whose ticker's trailing 24h quote volume is below `min_quote_24h`."""
    if not signals:
        return []
    qv = hourly.pivot(index="open_time", columns="ticker", values="quote_volume").sort_index()
    qv_24h = qv.rolling(24, min_periods=18).sum()
    out: list[dict] = []
    for sig in signals:
        t = int(sig["signal_time"])
        ticker = sig["ticker"]
        if ticker not in qv_24h.columns:
            continue
        vol = qv_24h.at[t, ticker] if t in qv_24h.index else None
        if pd.isna(vol) or float(vol) < min_quote_24h:
            continue
        out.append(sig)
    return out


def _relative_strength_btc(hourly: pd.DataFrame) -> list[dict]:
    """Legacy wrapper kept for backward compatibility / tests."""
    return _wrap_rs("relative_strength_btc", _rs_btc_signals(hourly))


def _rs_btc_filter_signals(hourly: pd.DataFrame) -> list[dict]:
    """RS-vs-BTC signals filtered to BTC trend direction only.

    LONG candidates are dropped when BTC 24h return < 0 (bear market),
    SHORT candidates are dropped when BTC 24h return > 0 (bull market).
    """
    base = _rs_btc_signals(hourly)
    if not base:
        return []
    close = hourly.pivot(index="open_time", columns="ticker", values="close").sort_index()
    if "BTC/USD" not in close.columns:
        return []
    btc_24h = (close["BTC/USD"].pct_change(24, fill_method=None) * 100)
    out: list[dict] = []
    for sig in base:
        t = int(sig["signal_time"])
        r = btc_24h.get(t)
        if pd.isna(r):
            continue
        if sig["direction"] == "long" and float(r) < 0:
            continue
        if sig["direction"] == "short" and float(r) > 0:
            continue
        out.append(sig)
    return out


def _momentum_signals(hourly: pd.DataFrame) -> list[dict]:
    """Multi-timeframe momentum adapted to hourly candles.

    Mirrors the daily momentum_scan: 3d/7d/14d → 72h/168h/336h returns.
    avg_m = (r3 + r7*2 + r14*3) / 6; long if avg_m > 3, short if avg_m < -3.
    Only one signal per direction each hour is allowed via _cap_per_time.
    """
    if hourly.empty:
        return []
    close = hourly.pivot(index="open_time", columns="ticker", values="close").sort_index()
    if close.shape[0] < 336:
        return []
    r3 = close.pct_change(72, fill_method=None) * 100
    r7 = close.pct_change(168, fill_method=None) * 100
    r14 = close.pct_change(336, fill_method=None) * 100
    avg_m = (r3 + r7 * 2 + r14 * 3) / 6
    cadence = pd.Series(
        (close.index.to_numpy(dtype=np.int64) // (60 * 60_000)) % 4 == 0,
        index=close.index,
    )
    rows: list[dict] = []
    for ticker in close.columns:
        if ticker == "BTC/USD":
            continue
        long_cond = cadence & (avg_m[ticker] > 3)
        short_cond = cadence & (avg_m[ticker] < -3)
        direction = np.where(long_cond, "long", np.where(short_cond, "short", ""))
        selected = direction != ""
        score = avg_m[ticker].abs()
        for timestamp in close.index[selected]:
            price = close.at[timestamp, ticker]
            value = score.loc[timestamp]
            if pd.isna(price) or pd.isna(value):
                continue
            rows.append({
                "signal_time": int(timestamp),
                "ticker": ticker,
                "direction": str(direction[close.index.get_loc(timestamp)]),
                "signal_price": float(price),
                "score": float(value),
            })
    return rows


def _drawdown_signals(hourly: pd.DataFrame) -> list[dict]:
    """Mean-reversion long signals on deep hourly drawdowns.

    Mirrors the daily drawdown_scan: 90-day high → 2160-hour high.
    Enter long when drawdown >= 10% and 3h/7h returns confirm a bounce
    (r3 > 0.5% and r7 > 0%).
    """
    if hourly.empty:
        return []
    close = hourly.pivot(index="open_time", columns="ticker", values="close").sort_index()
    if close.shape[0] < 2160:
        return []
    high_90d = close.rolling(2160, min_periods=1680).max()
    dd_pct = (1 - close / high_90d) * 100
    r3 = close.pct_change(3, fill_method=None) * 100
    r7 = close.pct_change(7, fill_method=None) * 100
    cadence = pd.Series(
        (close.index.to_numpy(dtype=np.int64) // (60 * 60_000)) % 4 == 0,
        index=close.index,
    )
    rows: list[dict] = []
    for ticker in close.columns:
        if ticker == "BTC/USD":
            continue
        long_cond = (
            cadence
            & (dd_pct[ticker] >= 10)
            & (r3[ticker] > 0.5)
            & (r7[ticker] > 0)
        )
        score = dd_pct[ticker]
        for timestamp in close.index[long_cond]:
            price = close.at[timestamp, ticker]
            value = score.loc[timestamp]
            if pd.isna(price) or pd.isna(value):
                continue
            rows.append({
                "signal_time": int(timestamp),
                "ticker": ticker,
                "direction": "long",
                "signal_price": float(price),
                "score": float(value),
            })
    return rows


def _wrap_rs(strategy: str, raw: list[dict], *, count: int = 1) -> list[dict]:
    if not raw:
        return []
    frame = pd.DataFrame(raw).rename(columns={"signal_time": "open_time"})
    selected = _cap_per_time(frame, count=count)
    return [_candidate(strategy, **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _donchian_breakout(hourly: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        channel_high = frame["high"].rolling(24, min_periods=24).max().shift(1)
        channel_low = frame["low"].rolling(24, min_periods=24).min().shift(1)
        previous_close = frame["close"].shift(1)
        previous_channel_high = channel_high.shift(1)
        previous_channel_low = channel_low.shift(1)
        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean().shift(1).replace(0, np.nan)
        long_condition = (
            (frame["close"] > channel_high)
            & (previous_close <= previous_channel_high)
            & (frame["close"] > frame["open"])
        )
        short_condition = (
            (frame["close"] < channel_low)
            & (previous_close >= previous_channel_low)
            & (frame["close"] < frame["open"])
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        breakout_distance = np.where(
            direction == "long",
            (frame["close"] - channel_high) / atr,
            (channel_low - frame["close"]) / atr,
        )
        candle_body = (frame["close"] - frame["open"]).abs() / atr
        score = 1.5 + np.maximum(breakout_distance, 0) + candle_body
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), count=1
    )
    return [_candidate("donchian_breakout", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _opening_range_breakout(hourly: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        timestamps = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
        session = timestamps.dt.floor("D")
        session_hour = timestamps.dt.hour
        opening_mask = session_hour < 4
        opening_high = frame["high"].where(opening_mask).groupby(session).transform("max")
        opening_low = frame["low"].where(opening_mask).groupby(session).transform("min")
        previous_close = frame["close"].shift(1)
        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean().shift(1).replace(0, np.nan)
        volume_base = frame["quote_volume"].rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base
        trading_window = (session_hour >= 4) & (session_hour < 16)
        long_condition = (
            trading_window
            & (previous_close <= opening_high)
            & (frame["close"] > opening_high)
            & (frame["close"] > frame["open"])
            & (volume_ratio >= 1.20)
        )
        short_condition = (
            trading_window
            & (previous_close >= opening_low)
            & (frame["close"] < opening_low)
            & (frame["close"] < frame["open"])
            & (volume_ratio >= 1.20)
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        breakout_distance = np.where(
            direction == "long",
            (frame["close"] - opening_high) / atr,
            (opening_low - frame["close"]) / atr,
        )
        score = 1.5 + np.maximum(breakout_distance, 0) + np.log1p(volume_ratio)
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        out["session"] = session[direction != ""].to_numpy()
        out = out.sort_values("open_time").drop_duplicates(["session", "direction"])
        if not out.empty:
            rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True).drop(columns="session") if rows else pd.DataFrame(), count=1
    )
    return [_candidate("opening_range_breakout", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _bollinger_range_reversion(hourly: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        previous_close = frame["close"].shift(1)
        range_mean = frame["close"].rolling(24, min_periods=24).mean().shift(1)
        range_std = frame["close"].rolling(24, min_periods=24).std().shift(1).replace(0, np.nan)
        upper_band = range_mean + 2.0 * range_std
        lower_band = range_mean - 2.0 * range_std
        previous_mean = range_mean.shift(1)
        previous_std = range_std.shift(1)
        previous_upper = previous_mean + 2.0 * previous_std
        previous_lower = previous_mean - 2.0 * previous_std
        ema24 = frame["close"].ewm(span=24, adjust=False, min_periods=24).mean()
        ema72 = frame["close"].ewm(span=72, adjust=False, min_periods=72).mean()
        return_48h = frame["close"].pct_change(48, fill_method=None) * 100
        trend_gap = (ema24 / ema72 - 1.0).abs() * 100
        range_regime = (trend_gap <= 1.5) & (return_48h.abs() <= 5.0)
        long_condition = (
            range_regime
            & (previous_close < previous_lower)
            & (frame["close"] >= lower_band)
            & (frame["close"] < range_mean)
            & (frame["close"] > frame["open"])
        )
        short_condition = (
            range_regime
            & (previous_close > previous_upper)
            & (frame["close"] <= upper_band)
            & (frame["close"] > range_mean)
            & (frame["close"] < frame["open"])
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        previous_z = ((previous_close - previous_mean) / previous_std).abs()
        score = 1.5 + previous_z + (frame["close"] - frame["open"]).abs() / range_std
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), count=1
    )
    return [_candidate("bollinger_range_reversion", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _trend_rsi_pullback(hourly: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        ema24 = frame["close"].ewm(span=24, adjust=False, min_periods=24).mean()
        ema72 = frame["close"].ewm(span=72, adjust=False, min_periods=72).mean()
        delta = frame["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        return_72h = frame["close"].pct_change(72, fill_method=None) * 100
        volume_base = frame["quote_volume"].rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base
        long_condition = (
            (ema24 > ema72 * 1.01) & (return_72h >= 3.0)
            & (rsi.shift(1) < 45) & (rsi >= 45)
            & (frame["close"] > frame["open"]) & (volume_ratio >= 0.8)
        )
        short_condition = (
            (ema24 < ema72 * 0.99) & (return_72h <= -3.0)
            & (rsi.shift(1) > 55) & (rsi <= 55)
            & (frame["close"] < frame["open"]) & (volume_ratio >= 0.8)
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        trend_strength = (ema24 / ema72 - 1.0).abs() * 100
        score = 2.0 + trend_strength + (rsi - 50).abs() / 25
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), count=1
    )
    return [_candidate("trend_rsi_pullback", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _residual_reversion(hourly: pd.DataFrame) -> list[dict]:
    close = hourly.pivot(index="open_time", columns="ticker", values="close")
    returns = close.pct_change(fill_method=None)
    btc = returns.get("BTC/USD")
    if btc is None:
        return []
    btc_variance = btc.rolling(336, min_periods=168).var().shift(1).replace(0, np.nan)
    rows: list[dict] = []
    for ticker in close.columns:
        if ticker == "BTC/USD":
            continue
        coin = returns[ticker]
        beta = coin.rolling(336, min_periods=168).cov(btc).shift(1) / btc_variance
        correlation = coin.rolling(336, min_periods=168).corr(btc).shift(1)
        residual = coin - beta * btc
        cumulative = residual.rolling(6, min_periods=6).sum()
        baseline = cumulative.rolling(336, min_periods=168).std().shift(1).replace(0, np.nan)
        zscore = cumulative / baseline
        previous = zscore.shift(1)
        long_condition = (previous <= -2.5) & (zscore > previous) & (correlation >= 0.45)
        short_condition = (previous >= 2.5) & (zscore < previous) & (correlation >= 0.45)
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        selected = direction != ""
        for timestamp in close.index[selected]:
            price = close.at[timestamp, ticker]
            value = previous.loc[timestamp]
            if pd.isna(price) or pd.isna(value):
                continue
            rows.append({
                "open_time": int(timestamp),
                "ticker": ticker,
                "direction": str(direction[close.index.get_loc(timestamp)]),
                "signal_price": float(price),
                "score": float(abs(value)),
            })
    selected = _cap_per_time(pd.DataFrame(rows), count=1)
    return [_candidate("residual_reversion", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _volume_flow_breakout(hourly: pd.DataFrame) -> list[dict]:
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        candle_range = (frame["high"] - frame["low"]).replace(0, np.nan)
        close_location = (
            (frame["close"] - frame["low"]) - (frame["high"] - frame["close"])
        ) / candle_range
        signed_flow = close_location * frame["quote_volume"]
        flow_ratio = (
            signed_flow.rolling(12, min_periods=12).sum()
            / frame["quote_volume"].rolling(12, min_periods=12).sum().replace(0, np.nan)
        )
        range_high = frame["high"].rolling(48, min_periods=36).max().shift(1)
        range_low = frame["low"].rolling(48, min_periods=36).min().shift(1)
        previous_close = frame["close"].shift(1)
        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean().shift(1).replace(0, np.nan)
        volume_base = frame["quote_volume"].rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base
        long_condition = (
            (frame["close"] > range_high) & (flow_ratio >= 0.25)
            & (close_location >= 0.5) & (volume_ratio >= 1.5)
        )
        short_condition = (
            (frame["close"] < range_low) & (flow_ratio <= -0.25)
            & (close_location <= -0.5) & (volume_ratio >= 1.5)
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))
        distance = np.where(
            direction == "long",
            (frame["close"] - range_high) / atr,
            (range_low - frame["close"]) / atr,
        )
        score = 1.5 + np.maximum(distance, 0) + np.log1p(volume_ratio) + flow_ratio.abs()
        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), count=1
    )
    return [_candidate("volume_flow_breakout", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _range_mean_reversion(hourly: pd.DataFrame) -> list[dict]:
    """RSI(14) oversold/overbought reversion: LONG below 25, SHORT above 75."""
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        close = frame["close"]
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta.clip(upper=0))

        # Wilder's RSI(14) — all shifted to use only past data.
        avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().shift(1)
        avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().shift(1)
        rsi = 100 - 100 / (1 + avg_gain / avg_loss.replace(0, np.nan))

        long_cond = rsi < 25
        short_cond = rsi > 75
        direction = np.where(long_cond, "long", np.where(short_cond, "short", ""))

        # Score: distance from the 50 midline (further = stronger signal).
        score = (rsi - 50).abs()

        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))

    selected = _cap_per_time(pd.concat(rows, ignore_index=True) if rows else pd.DataFrame())
    return [_candidate("range_mean_reversion", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _false_breakout(hourly: pd.DataFrame) -> list[dict]:
    """Fade false breakouts of a 48h support/resistance range.

    LONG: price pokes below the support low but closes back above it.
    SHORT: price pokes above the resistance high but closes back below it.
    """
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        range_high = frame["high"].rolling(48, min_periods=36).max().shift(1)
        range_low = frame["low"].rolling(48, min_periods=36).min().shift(1)
        previous_close = frame["close"].shift(1)
        previous_high = frame["high"].shift(1)
        previous_low = frame["low"].shift(1)

        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean().shift(1).replace(0, np.nan)

        volume_base = frame["quote_volume"].rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base

        # False breakdown: breaks support but closes back above with bullish candle.
        long_condition = (
            (previous_low >= range_low)
            & (frame["low"] < range_low)
            & (frame["close"] > range_low)
            & (frame["close"] > frame["open"])
            & (volume_ratio >= 1.2)
        )
        # False breakout: breaks resistance but closes back below with bearish candle.
        short_condition = (
            (previous_high <= range_high)
            & (frame["high"] > range_high)
            & (frame["close"] < range_high)
            & (frame["close"] < frame["open"])
            & (volume_ratio >= 1.2)
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))

        body = (frame["close"] - frame["open"]).abs().clip(lower=atr * 0.05)
        upper_wick = frame["high"] - np.maximum(frame["open"], frame["close"])
        lower_wick = np.minimum(frame["open"], frame["close"]) - frame["low"]
        breakout_size = np.where(
            direction == "long",
            (range_low - frame["low"]) / atr,
            (frame["high"] - range_high) / atr,
        )
        wick_ratio = np.where(direction == "long", lower_wick / body, upper_wick / body)
        score = 1.5 + np.maximum(breakout_size, 0) + np.minimum(wick_ratio, 5) / 5 + np.log1p(volume_ratio)

        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))

    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), count=1
    )
    return [_candidate("false_breakout", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _vwap_reclaim_deviation(hourly: pd.DataFrame) -> list[dict]:
    """Fade VWAP deviations after price reclaims the anchored VWAP.

    LONG: price was below 48h VWAP with significant downward deviation,
          then closes back above VWAP on a bullish candle.
    SHORT: price was above 48h VWAP with significant upward deviation,
           then closes back below VWAP on a bearish candle.
    """
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        typical = (frame["high"] + frame["low"] + frame["close"]) / 3
        weight = frame["volume"].replace(0, np.nan)
        weighted = typical * weight

        # Anchored 48h VWAP known only after the prior candle closes.
        rolling_weight = weight.rolling(48, min_periods=36).sum().shift(1)
        vwap = weighted.rolling(48, min_periods=36).sum().shift(1) / rolling_weight

        deviation_pct = (frame["close"] / vwap - 1.0) * 100
        deviation_std = deviation_pct.rolling(48, min_periods=36).std().shift(1).replace(0, np.nan)
        zscore = deviation_pct / deviation_std
        previous_zscore = zscore.shift(1)
        previous_close = frame["close"].shift(1)

        volume_base = frame["quote_volume"].rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
        volume_ratio = frame["quote_volume"] / volume_base

        long_condition = (
            (previous_close < vwap)
            & (frame["close"] > vwap)
            & (previous_zscore < -1.5)
            & (frame["close"] > frame["open"])
            & (volume_ratio >= 1.0)
        )
        short_condition = (
            (previous_close > vwap)
            & (frame["close"] < vwap)
            & (previous_zscore > 1.5)
            & (frame["close"] < frame["open"])
            & (volume_ratio >= 1.0)
        )
        direction = np.where(long_condition, "long", np.where(short_condition, "short", ""))

        score = 1.5 + previous_zscore.abs().fillna(0) + np.log1p(volume_ratio.fillna(1))

        out = frame.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = score[direction != ""]
        rows.append(out.rename(columns={"close": "signal_price"}))

    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), count=1
    )
    return [_candidate("vwap_reclaim_deviation", **row) for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")]


def _funding_crowding_reversal(
    hourly: pd.DataFrame, funding: pd.DataFrame
) -> list[dict]:
    """Fade crowded positioning signaled by extreme settled funding rates.

    Funding settles every 8h (00/08/16 UTC) and is known at settle_time, so
    the latest settlement at an hourly candle's open is usable at its close.
    LONG: funding z-score <= -1.5 (crowded shorts). SHORT: z >= +1.5.
    """
    if hourly.empty or funding.empty:
        return []
    zscore_parts: list[pd.DataFrame] = []
    for ticker, group in funding.groupby("ticker", sort=False):
        frame = group.sort_values("settle_time").copy()
        # ~30 days of 8h settlements; shift keeps normalization point-in-time.
        mean = frame["funding_rate"].rolling(90, min_periods=45).mean().shift(1)
        std = (
            frame["funding_rate"]
            .rolling(90, min_periods=45)
            .std()
            .shift(1)
            .replace(0, np.nan)
        )
        frame["z"] = (frame["funding_rate"] - mean) / std
        frame["ticker"] = ticker
        zscore_parts.append(
            frame[["ticker", "settle_time", "z"]].dropna(subset=["z"])
        )
    if not zscore_parts:
        return []
    zscores = pd.concat(zscore_parts, ignore_index=True)
    zmaps = {
        ticker: group.sort_values("settle_time")
        for ticker, group in zscores.groupby("ticker", sort=False)
    }
    rows: list[pd.DataFrame] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        zmap = zmaps.get(ticker)
        if zmap is None or zmap.empty:
            continue
        frame = group.sort_values("open_time").copy()
        merged = pd.merge_asof(
            frame[["open_time", "open", "close"]],
            zmap[["settle_time", "z"]],
            left_on="open_time",
            right_on="settle_time",
            direction="backward",
        )
        # Only the first hourly candle after each settlement carries fresh
        # funding information; later hours would repeat the same signal.
        cadence = merged["open_time"].mod(8 * HOUR_MS).eq(0)
        z = merged["z"]
        direction = np.where(
            cadence & (z <= -1.5), "long",
            np.where(cadence & (z >= 1.5), "short", ""),
        )
        out = merged.loc[direction != "", ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[direction != ""]
        out["score"] = z[direction != ""].abs()
        rows.append(out.rename(columns={"close": "signal_price"}))
    selected = _cap_per_time(
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), count=2
    )
    return [
        _candidate("funding_crowding_reversal", **row)
        for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")
    ]


def _spot_perp_basis_mean_reversion(
    spot: pd.DataFrame, perp: pd.DataFrame
) -> list[dict]:
    """Fade extreme spot-perp basis deviations.

    basis_pct = (perp_close / spot_close - 1) * 100, z-scored over 7 days.
    Perp premium (z >= 2)  → revert down  → LONG  spot (spot is cheaper).
    Perp discount (z <= -2) → revert up    → SHORT spot.
    """
    if spot.empty or perp.empty:
        return []
    spot_close = spot.pivot(index="open_time", columns="ticker", values="close")
    perp_close = perp.pivot(index="open_time", columns="ticker", values="close")
    common_tickers = sorted(set(spot_close.columns) & set(perp_close.columns))
    if not common_tickers:
        return []
    spot_close = spot_close[common_tickers]
    perp_close = perp_close[common_tickers]
    aligned = spot_close.align(perp_close, join="inner")
    spot_close, perp_close = aligned[0], aligned[1]
    basis_pct = (perp_close / spot_close.replace(0, np.nan) - 1.0) * 100

    rows: list[dict] = []
    for ticker in common_tickers:
        series = basis_pct[ticker].dropna()
        if len(series) < 168:
            continue
        mean = series.rolling(168, min_periods=84).mean().shift(1)
        std = series.rolling(168, min_periods=84).std().shift(1).replace(0, np.nan)
        zscore = (series - mean) / std
        for idx in range(len(series)):
            z = zscore.iloc[idx]
            if not np.isfinite(z):
                continue
            if z >= 2.0:
                direction = "long"
            elif z <= -2.0:
                direction = "short"
            else:
                continue
            rows.append({
                "open_time": int(series.index[idx]),
                "ticker": ticker,
                "direction": direction,
                "signal_price": float(spot_close.iloc[idx][ticker]),
                "score": float(abs(z)),
            })
    if not rows:
        return []
    selected = _cap_per_time(pd.DataFrame(rows), count=2)
    return [
        _candidate("spot_perp_basis", **row)
        for row in selected.rename(columns={"open_time": "signal_time"}).to_dict("records")
    ]


def _btc_liquidation_cascade(hourly: pd.DataFrame) -> list[dict]:
    """Long altcoins after a swift BTC drop that likely triggered liquidation cascades.

    When BTC drops ≥2.0% in 4 hours with volume ≥1.5x the trailing median, leveraged
    longs get liquidated, and the market overshoots down. Long the alts that got
    flushed hardest (4h return ≤ −1.5%) — they tend to bounce back.
    """
    if hourly.empty or "BTC/USD" not in hourly["ticker"].values:
        return []
    btc = hourly[hourly["ticker"] == "BTC/USD"].sort_values("open_time").copy()
    btc_close = btc.set_index("open_time")["close"]
    btc_r4h = btc_close.pct_change(4, fill_method=None) * 100
    btc_vol = btc.set_index("open_time")["quote_volume"]
    btc_vol_base = btc_vol.rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
    btc_vol_ratio = btc_vol / btc_vol_base
    # Use btc_close.index (open_time) — not btc.index which is a row-position RangeIndex.
    times = btc_close.index.to_numpy(dtype=np.int64)
    cadence = (times // HOUR_MS) % 4 == 0
    flush_mask = (
        cadence
        & (btc_r4h.to_numpy() <= -2.0)
        & (btc_vol_ratio.to_numpy() >= 1.5)
    )
    flush_times = btc_close.index[flush_mask]
    if flush_times.empty:
        return []
    flush_set = set(flush_times.to_numpy())
    rows: list[dict] = []
    close = hourly.pivot(index="open_time", columns="ticker", values="close").sort_index()
    for ticker in close.columns:
        if ticker == "BTC/USD":
            continue
        t_series = close[ticker].dropna()
        if len(t_series) < 30:
            continue
        r4h = t_series.pct_change(4, fill_method=None) * 100
        for t in flush_set:
            if t not in r4h.index:
                continue
            r = r4h.loc[t]
            p = t_series.get(t)
            if pd.isna(r) or pd.isna(p) or r > -1.5:
                continue
            rows.append({
                "signal_time": int(t),
                "ticker": ticker,
                "direction": "long",
                "signal_price": float(p),
                "score": float(abs(r)),
            })
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df = df.sort_values(["signal_time", "score"], ascending=[True, False])
    df = df.groupby("signal_time", sort=False).head(5)
    return df.to_dict("records")


def _overnight_drift(hourly: pd.DataFrame) -> list[dict]:
    """Trade the overnight gap at UTC session boundaries.

    The crypto market trades 24/7, but institutional flow concentrates early in
    the UTC day. A large gap between the 18:00 UTC close and the 00:00 UTC open
    (6 hours later) signals overnight positioning. z ≥ 1.5σ → LONG the
    continuation; z ≤ −1.5σ → SHORT the continuation.
    """
    if hourly.empty:
        return []
    rows: list[dict] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        if len(frame) < 50:
            continue
        dt = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
        hour = dt.dt.hour.to_numpy()

        opens_00_mask = hour == 0
        closes_18_mask = hour == 18

        # Each 00:00 open pairs with the 18:00 close 6 hours earlier.
        # Build a mapping: open_time_00 -> close at (open_time_00 - 6h).
        opens_00 = frame.loc[opens_00_mask, ["open_time", "open"]].copy()
        closes_18 = frame.loc[closes_18_mask, ["open_time", "close"]].copy()
        if opens_00.empty or closes_18.empty:
            continue
        # The 18:00 candle has open_time T18; the 00:00 candle has T00 = T18 + 6h.
        # So the expected close time for a 00:00-open is T00 - 6*HOUR_MS.
        closes_18_map = closes_18.set_index("open_time")["close"]
        opens_00["prev_close"] = closes_18_map.reindex(
            opens_00["open_time"].to_numpy() - 6 * HOUR_MS
        ).to_numpy()
        opens_00["gap_pct"] = (
            opens_00["open"] / opens_00["prev_close"].replace(0, np.nan) - 1.0
        ) * 100
        opens_00 = opens_00.dropna(subset=["gap_pct"])
        if opens_00.empty:
            continue
        gap_std = (
            opens_00["gap_pct"]
            .rolling(30, min_periods=20)
            .std()
            .shift(1)
            .replace(0, np.nan)
        )
        opens_00["z"] = opens_00["gap_pct"] / gap_std

        # Volume ratio: this 00:00 candle vs trailing 24h median quote_volume.
        qv = frame.set_index("open_time")["quote_volume"]
        qv_base = qv.rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
        vol_ratio = (qv / qv_base).reindex(opens_00["open_time"]).to_numpy()
        opens_00["vol_ratio"] = vol_ratio

        long_cond = (opens_00["z"] >= 1.5) & (opens_00["vol_ratio"] >= 1.1)
        short_cond = (opens_00["z"] <= -1.5) & (opens_00["vol_ratio"] >= 1.1)
        direction = np.where(long_cond, "long", np.where(short_cond, "short", ""))
        selected = direction != ""
        for idx in np.where(selected)[0]:
            row = opens_00.iloc[idx]
            rows.append({
                "signal_time": int(row["open_time"]),
                "ticker": ticker,
                "direction": str(direction[idx]),
                "signal_price": float(row["open"]),
                "score": float(abs(row["z"])),
            })
    return rows


def _volatility_clustering_reversal(hourly: pd.DataFrame) -> list[dict]:
    """Fade sharp moves that come with abnormal volatility clusters.

    When a single-hour ATR spike reaches ≥2.5σ above its trailing mean AND the
    candle closes against the spike direction (bullish after a down-spike, bearish
    after an up-spike), enter the reversal — these volatility clusters mean-revert.
    """
    if hourly.empty:
        return []
    rows: list[dict] = []
    for ticker, group in hourly.groupby("ticker", sort=False):
        frame = group.sort_values("open_time").copy()
        if len(frame) < 50:
            continue
        previous_close = frame["close"].shift(1)
        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean()
        # Normalize ATR by price to compare across tickers
        atr_pct = (atr / frame["close"].replace(0, np.nan) * 100).shift(1)
        atr_mean = atr_pct.rolling(168, min_periods=84).mean().shift(1)
        atr_std = atr_pct.rolling(168, min_periods=84).std().shift(1).replace(0, np.nan)
        atr_z = (atr_pct - atr_mean) / atr_std
        # Reversal candle: bullish (close>open) after a down-spike (previous candle dropped)
        hourly_return = (frame["close"] / frame["open"].replace(0, np.nan) - 1.0) * 100
        prev_return = hourly_return.shift(1)
        bullish_reversal = (atr_z >= 2.0) & (prev_return < 0) & (frame["close"] > frame["open"])
        bearish_reversal = (atr_z >= 2.0) & (prev_return > 0) & (frame["close"] < frame["open"])
        direction = np.where(bullish_reversal, "long", np.where(bearish_reversal, "short", ""))
        selected = direction != ""
        out = frame.loc[selected, ["open_time", "close"]].copy()
        out["ticker"] = ticker
        out["direction"] = direction[selected]
        out["score"] = atr_z[selected].astype(float).abs()
        rows.append(out.rename(columns={"close": "signal_price"}))
    if not rows:
        return []
    return pd.concat(rows, ignore_index=True).rename(columns={"open_time": "signal_time"}).to_dict("records")


def _cross_sectional_mean_reversion(hourly: pd.DataFrame) -> list[dict]:
    """Fade the 24h cross-sectional return extremes.

    At each 4h cadence point, rank all tickers by their 24h return. The top
    decile (z ≥ +2σ) is overbought → SHORT. The bottom decile (z ≤ −2σ) is
    oversold → LONG. Mean reversion across the market.
    """
    if hourly.empty:
        return []
    close = hourly.pivot(index="open_time", columns="ticker", values="close").sort_index()
    if close.empty:
        return []
    return_24h = close.pct_change(24, fill_method=None) * 100
    cs_mean = return_24h.mean(axis=1)
    cs_std = return_24h.std(axis=1).replace(0, np.nan)
    cs_z = return_24h.sub(cs_mean, axis=0).div(cs_std, axis=0)
    cadence = pd.Series(
        (close.index.to_numpy(dtype=np.int64) // HOUR_MS) % 4 == 0,
        index=close.index,
    )
    rows: list[dict] = []
    for timestamp in close.index[cadence.reindex(close.index).fillna(False).astype(bool)]:
        if not cadence.get(timestamp, False):
            continue
        z_row = cs_z.loc[timestamp].dropna()
        if len(z_row) < 20:
            continue
        longs = z_row[z_row <= -2.0].sort_values()
        shorts = z_row[z_row >= 2.0].sort_values(ascending=False)
        for ticker, z in longs.head(3).items():
            price = close.at[timestamp, ticker]
            if pd.isna(price):
                continue
            rows.append({
                "signal_time": int(timestamp),
                "ticker": ticker,
                "direction": "long",
                "signal_price": float(price),
                "score": float(abs(z)),
            })
        for ticker, z in shorts.head(3).items():
            price = close.at[timestamp, ticker]
            if pd.isna(price):
                continue
            rows.append({
                "signal_time": int(timestamp),
                "ticker": ticker,
                "direction": "short",
                "signal_price": float(price),
                "score": float(abs(z)),
            })
    return rows


def _multi_signal_confluence(hourly: pd.DataFrame) -> list[dict]:
    """Enter only when RS-vs-BTC, RSI crossover, and volume confirmation all align.

    LONG:  RS z ≥ 0.8, RSI(14) crosses up through 50, volume ≥ 1.5x median.
    SHORT: RS z ≤ −0.8, RSI crosses down through 50, volume ≥ 1.5x median.
    """
    if hourly.empty:
        return []
    close = hourly.pivot(index="open_time", columns="ticker", values="close").sort_index()
    if "BTC/USD" not in close.columns:
        return []
    return_6h = close.pct_change(6, fill_method=None) * 100
    return_24h = close.pct_change(24, fill_method=None) * 100
    relative_24h = return_24h.sub(return_24h["BTC/USD"], axis=0)
    relative_6h = return_6h.sub(return_6h["BTC/USD"], axis=0)
    relative_mean = relative_24h.mean(axis=1)
    relative_std = relative_24h.std(axis=1).replace(0, np.nan)
    relative_z = relative_24h.sub(relative_mean, axis=0).div(relative_std, axis=0)
    cadence = pd.Series(
        (close.index.to_numpy(dtype=np.int64) // HOUR_MS) % 4 == 0,
        index=close.index,
    )
    # RSI(14) Wilder's for each ticker
    deltas = close.diff()
    gain = deltas.clip(lower=0)
    loss = (-deltas.clip(upper=0))
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + avg_gain / avg_loss.replace(0, np.nan))
    rsi_prev = rsi.shift(1)
    # Volume
    qv = hourly.pivot(index="open_time", columns="ticker", values="quote_volume").sort_index()
    qv_base = qv.rolling(24, min_periods=18).median().shift(1).replace(0, np.nan)
    vol_ratio = qv / qv_base
    rows: list[dict] = []
    for ticker in close.columns:
        if ticker == "BTC/USD":
            continue
        rsi_cross_up = (rsi_prev[ticker] < 50) & (rsi[ticker] >= 50)
        rsi_cross_dn = (rsi_prev[ticker] > 50) & (rsi[ticker] <= 50)
        long_cond = (
            cadence
            & (relative_z[ticker] >= 0.8)
            & (return_6h[ticker] >= 0.4)
            & rsi_cross_up
            & (vol_ratio[ticker] >= 1.2)
        )
        short_cond = (
            cadence
            & (relative_z[ticker] <= -0.8)
            & (return_6h[ticker] <= -0.4)
            & rsi_cross_dn
            & (vol_ratio[ticker] >= 1.2)
        )
        direction = np.where(long_cond, "long", np.where(short_cond, "short", ""))
        selected = direction != ""
        score = relative_z[ticker].abs() + (rsi[ticker] - 50).abs() / 25
        for timestamp in close.index[selected]:
            price = close.at[timestamp, ticker]
            value = score.loc[timestamp]
            if pd.isna(price) or pd.isna(value):
                continue
            rows.append({
                "signal_time": int(timestamp),
                "ticker": ticker,
                "direction": str(direction[close.index.get_loc(timestamp)]),
                "signal_price": float(price),
                "score": float(value),
            })
    return rows


def generate_candidates(
    candles: pd.DataFrame,
    *,
    already_hourly: bool = False,
    funding: pd.DataFrame | None = None,
    perp: pd.DataFrame | None = None,
) -> dict[str, list[dict]]:
    hourly = candles if already_hourly else _aggregate(candles, 60)
    if funding is None:
        funding = pd.DataFrame(columns=["ticker", "settle_time", "funding_rate"])
    if perp is None:
        perp = pd.DataFrame(columns=["ticker", "open_time", "open", "high", "low", "close", "volume", "quote_volume"])
    return {
        "rs_low_cost": _wrap_rs("rs_low_cost", _rs_btc_signals(hourly), count=1),
        "rs_low_liquidity": _wrap_rs(
            "rs_low_liquidity",
            _liquidity_filter(_rs_btc_signals(hourly), hourly, min_quote_24h=10_000_000),
            count=1,
        ),
        "rs_regime_filter": _wrap_rs("rs_regime_filter", _rs_btc_filter_signals(hourly), count=1),
        "momentum": _wrap_rs("momentum", _momentum_signals(hourly), count=1),
        "drawdown": _wrap_rs("drawdown", _drawdown_signals(hourly), count=1),
    }


def _directional_return(direction: str, entry: float, price: float) -> float:
    if direction == "long":
        return (price / entry - 1.0) * 100
    return ((entry - price) / entry) * 100


def _portfolio_return(
    candidate: dict,
    entry_price: float,
    price: float,
    hedge_entry_price: float | None = None,
    hedge_price: float | None = None,
) -> float:
    primary = _directional_return(candidate["direction"], entry_price, price)
    if not candidate.get("hedge_ticker"):
        return primary
    if hedge_entry_price is None or hedge_price is None:
        raise ValueError("Hedged strategy requires both BTC prices")
    ratio = max(float(candidate.get("hedge_ratio") or 0), 0.0)
    primary_weight = 1.0 / (1.0 + ratio)
    hedge_weight = ratio / (1.0 + ratio)
    hedge = _directional_return(
        candidate["hedge_direction"], hedge_entry_price, hedge_price
    )
    return primary * primary_weight + hedge * hedge_weight


def _simulate(
    candidate: dict,
    ticker_bars: pd.DataFrame,
    hedge_bars: pd.DataFrame | None = None,
) -> dict | None:
    if candidate.get("hedge_ticker"):
        if hedge_bars is None or hedge_bars.empty:
            return None
        bars = ticker_bars.merge(
            hedge_bars,
            on="open_time",
            how="inner",
            suffixes=("", "_hedge"),
        ).sort_values("open_time")
        times = bars["open_time"].to_numpy(dtype=np.int64)
        entry_index = int(np.searchsorted(times, candidate["signal_time"], side="left"))
        if entry_index >= len(bars) or int(times[entry_index]) != int(candidate["signal_time"]):
            return None
        entry = bars.iloc[entry_index]
        entry_price = float(entry["open"])
        hedge_entry_price = float(entry["open_hedge"])
        horizon = int(times[entry_index] + candidate["hold_minutes"] * 60_000)
        exit_index = int(np.searchsorted(times, horizon, side="left"))
        if (
            exit_index <= entry_index
            or exit_index >= len(bars)
            or int(times[exit_index]) != horizon
            or np.any(np.diff(times[entry_index:exit_index + 1]) != FIVE_MINUTES_MS)
        ):
            return None
        chosen = exit_index
        reason = "time"
        exit_price = float(bars.iloc[chosen]["open"])
        hedge_exit_price = float(bars.iloc[chosen]["open_hedge"])
        for index in range(entry_index, exit_index):
            bar = bars.iloc[index]
            current_price = float(bar["close"])
            current_hedge = float(bar["close_hedge"])
            gross = _portfolio_return(
                candidate, entry_price, current_price,
                hedge_entry_price, current_hedge,
            )
            if float(candidate["stop_pct"]) > 0 and gross <= -float(candidate["stop_pct"]):
                chosen, reason = index, "stop"
                exit_price, hedge_exit_price = current_price, current_hedge
                break
            if float(candidate["target_pct"]) > 0 and gross >= float(candidate["target_pct"]):
                chosen, reason = index, "target"
                exit_price, hedge_exit_price = current_price, current_hedge
                break
        gross = _portfolio_return(
            candidate, entry_price, exit_price,
            hedge_entry_price, hedge_exit_price,
        )
        net = gross - float(candidate.get("cost_pct", ROUND_TRIP_COST_PCT))
        return {
            **candidate,
            "entry_time": int(times[entry_index]), "entry_price": entry_price,
            "hedge_entry_price": hedge_entry_price,
            "exit_time": int(times[chosen]), "exit_price": exit_price,
            "hedge_exit_price": hedge_exit_price,
            "exit_reason": reason, "gross_return_pct": gross,
            "cost_pct": float(candidate.get("cost_pct", ROUND_TRIP_COST_PCT)), "net_return_pct": net,
            "cash_result": STAKE_USD * net / 100,
        }
    bars = ticker_bars.sort_values("open_time")
    times = bars["open_time"].to_numpy(dtype=np.int64)
    entry_index = int(np.searchsorted(times, candidate["signal_time"], side="left"))
    if entry_index >= len(bars) or int(times[entry_index]) != int(candidate["signal_time"]):
        return None
    entry = bars.iloc[entry_index]
    entry_price = float(entry["open"])
    horizon = int(times[entry_index] + candidate["hold_minutes"] * 60_000)
    exit_index = int(np.searchsorted(times, horizon, side="left"))
    if (
        exit_index <= entry_index
        or exit_index >= len(bars)
        or int(times[exit_index]) != horizon
        or np.any(np.diff(times[entry_index:exit_index + 1]) != FIVE_MINUTES_MS)
    ):
        return None
    stop = float(candidate["stop_pct"])
    target = float(candidate["target_pct"])
    trailing = float(candidate.get("trailing_stop_pct", 0.0))
    chosen = exit_index
    reason = "time"
    # A timed exit happens at the first tradable open on the horizon boundary.
    exit_price = float(bars.iloc[chosen]["open"])
    peak = entry_price
    for index in range(entry_index, exit_index):
        bar = bars.iloc[index]
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        if candidate["direction"] == "long":
            peak = max(peak, bar_high)
            stop_hit = stop > 0 and bar_low <= entry_price * (1 - stop / 100)
            target_hit = target > 0 and bar_high >= entry_price * (1 + target / 100)
            stop_price = entry_price * (1 - stop / 100)
            target_price = entry_price * (1 + target / 100)
            trail_price = peak * (1 - trailing / 100)
            trail_hit = trailing > 0 and bar_low <= trail_price
        else:
            peak = min(peak, bar_low)
            stop_hit = stop > 0 and bar_high >= entry_price * (1 + stop / 100)
            target_hit = target > 0 and bar_low <= entry_price * (1 - target / 100)
            stop_price = entry_price * (1 + stop / 100)
            target_price = entry_price * (1 - target / 100)
            trail_price = peak * (1 + trailing / 100)
            trail_hit = trailing > 0 and bar_high >= trail_price
        if stop > 0 and stop_hit:
            chosen, reason, exit_price = index, "stop", stop_price
            break
        if trailing > 0 and trail_hit:
            chosen, reason, exit_price = index, "trail", trail_price
            break
        if target > 0 and target_hit:
            chosen, reason, exit_price = index, "target", target_price
            break
    gross = _directional_return(candidate["direction"], entry_price, exit_price)
    net = gross - float(candidate.get("cost_pct", ROUND_TRIP_COST_PCT))
    return {
        **candidate,
        "entry_time": int(times[entry_index]), "entry_price": entry_price,
        "exit_time": int(times[chosen]), "exit_price": exit_price,
        "exit_reason": reason, "gross_return_pct": gross,
        "cost_pct": float(candidate.get("cost_pct", ROUND_TRIP_COST_PCT)), "net_return_pct": net,
        "cash_result": STAKE_USD * net / 100,
    }


def _select_non_overlapping_candidates(events: list[dict]) -> list[dict]:
    selected: list[dict] = []
    next_free: dict[str, int] = {}
    for event in sorted(events, key=lambda item: (item["signal_time"], item["ticker"])):
        if int(event["signal_time"]) < next_free.get(event["ticker"], 0):
            continue
        selected.append(event)
        next_free[event["ticker"]] = (
            int(event["signal_time"])
            + int(event["hold_minutes"]) * 60_000
        )
    return selected


def backtest(candles: pd.DataFrame, candidates: dict[str, list[dict]]) -> tuple[list[dict], dict]:
    by_ticker = {ticker: group for ticker, group in candles.groupby("ticker", sort=False)}
    trades: list[dict] = []
    eligible_counts: dict[str, int] = {}
    missing_counts: dict[str, int] = {}
    for strategy, events in candidates.items():
        selected = _select_non_overlapping_candidates(events)
        eligible_counts[strategy] = len(selected)
        missing_counts[strategy] = 0
        for event in selected:
            ticker_bars = by_ticker.get(event["ticker"])
            if ticker_bars is None:
                missing_counts[strategy] += 1
                continue
            hedge_bars = by_ticker.get(event.get("hedge_ticker"))
            trade = _simulate(event, ticker_bars, hedge_bars)
            if trade is None:
                missing_counts[strategy] += 1
                continue
            trades.append(trade)
    metrics: dict[str, dict] = {}
    latest_time = int(candles["open_time"].max()) if not candles.empty else 0
    earliest_time = int(candles["open_time"].min()) if not candles.empty else 0
    coverage_days = (
        int((latest_time - earliest_time) // (24 * 60 * 60 * 1000)) + 1
        if latest_time and earliest_time else 0
    )
    for strategy in candidates:
        for days in BACKTEST_WINDOWS_DAYS:
            cutoff = latest_time - days * 24 * 60 * 60 * 1000
            subset = [
                trade for trade in trades
                if trade["strategy"] == strategy
                and int(trade["entry_time"]) >= cutoff
            ]
            values = [float(trade["cash_result"]) for trade in subset]
            wins = [value for value in values if value > 0]
            losses = [value for value in values if value < 0]
            metrics[f"{strategy}_{days}d"] = {
                "window_days": days,
                "strategy": strategy,
                "coverage_days": coverage_days,
                "is_complete": coverage_days >= days,
                "eligible_candidates": eligible_counts.get(strategy, 0),
                "missing_executions": missing_counts.get(strategy, 0),
                "trades": len(subset),
                "trades_per_day": len(subset) / days if days else 0.0,
                "wins": len(wins),
                "win_rate": len(wins) / len(subset) * 100 if subset else 0.0,
                "net_cash": sum(values),
                "avg_weekly_cash": sum(values) * 7 / days if days else 0.0,
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
        hedge_bars = by_ticker.get(trade.get("hedge_ticker"))
        is_hedged = bool(trade.get("hedge_ticker"))
        if is_hedged and (hedge_bars is None or hedge_bars.empty):
            continue
        evaluation_bars = (
            bars.merge(
                hedge_bars,
                on="open_time",
                how="inner",
                suffixes=("", "_hedge"),
            ).sort_values("open_time")
            if is_hedged else bars
        )
        if trade["status"] == "pending":
            candidates = evaluation_bars[
                evaluation_bars["open_time"] >= int(trade["signal_time"])
            ]
            if candidates.empty:
                continue
            entry = candidates.iloc[0]
            entry_time = int(entry["open_time"])
            entry_price = float(entry["open"])
            hedge_entry_price = float(entry["open_hedge"]) if is_hedged else None
            conn.execute(
                """
                UPDATE short_term_forward_trades
                SET status='active', entry_time=?, entry_price=?, planned_exit_time=?,
                    last_evaluated_time=?, last_price=?, hedge_entry_price=?,
                    hedge_last_price=?, updated_at=datetime('now')
                WHERE id=? AND status='pending'
                """,
                (entry_time, entry_price, entry_time + int(trade["hold_minutes"]) * 60_000,
                 entry_time - 1, entry_price, hedge_entry_price,
                 hedge_entry_price, trade["id"]),
            )
            trade.update({"status": "active", "entry_time": entry_time, "entry_price": entry_price,
                          "planned_exit_time": entry_time + int(trade["hold_minutes"]) * 60_000,
                          "last_evaluated_time": entry_time - 1,
                          "hedge_entry_price": hedge_entry_price})
            opened += 1
        unseen = evaluation_bars[
            evaluation_bars["open_time"]
            > int(trade["last_evaluated_time"] or trade["entry_time"] - 1)
        ]
        for _, bar in unseen.iterrows():
            timestamp = int(bar["open_time"])
            entry_price = float(trade["entry_price"])
            stop = float(trade["stop_pct"])
            target = float(trade["target_pct"])
            direction = trade["direction"]
            hedge_price = None
            if is_hedged:
                timed_exit = timestamp >= int(trade["planned_exit_time"])
                price = float(bar["open"] if timed_exit else bar["close"])
                hedge_price = float(
                    bar["open_hedge"] if timed_exit else bar["close_hedge"]
                )
                gross = _portfolio_return(
                    trade, entry_price, price,
                    float(trade["hedge_entry_price"]), hedge_price,
                )
                reason = "time" if timed_exit else None
                if not reason and gross <= -stop:
                    reason = "stop"
                elif not reason and gross >= target:
                    reason = "target"
            elif timestamp >= int(trade["planned_exit_time"]):
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
            if not is_hedged and timestamp < int(trade["planned_exit_time"]):
                reason = None
                price = float(bar["close"])
            if not is_hedged:
                if reason == "time":
                    pass
                elif stop > 0 and stop_hit:
                    reason, price = "stop", stop_price
                elif target > 0 and target_hit:
                    reason, price = "target", target_price
                gross = _directional_return(direction, entry_price, price)
            cost = float(trade.get("cost_pct") or trade.get("cost_pct_at_entry") or ROUND_TRIP_COST_PCT)
            net = gross - cost
            if reason:
                conn.execute(
                    """
                    UPDATE short_term_forward_trades
                    SET status='closed', last_evaluated_time=?, last_price=?,
                        current_net_return_pct=?, current_cash_result=?, exit_time=?,
                        exit_price=?, exit_reason=?, gross_return_pct=?, cost_pct=?,
                        net_return_pct=?, cash_result=?, hedge_last_price=?,
                        hedge_exit_price=?, updated_at=datetime('now')
                    WHERE id=? AND status='active'
                    """,
                    (timestamp, price, net, STAKE_USD * net / 100, timestamp, price,
                     reason, gross, cost, net, STAKE_USD * net / 100,
                     hedge_price, hedge_price, trade["id"]),
                )
                closed += 1
                break
            conn.execute(
                """
                UPDATE short_term_forward_trades
                SET last_evaluated_time=?, last_price=?, current_net_return_pct=?,
                    current_cash_result=?, hedge_last_price=?, updated_at=datetime('now')
                WHERE id=? AND status='active'
                """,
                (timestamp, price, net, STAKE_USD * net / 100,
                 hedge_price, trade["id"]),
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
        eligible_times = [
            int(event["signal_time"])
            for event in events
            if int(event["signal_time"]) <= int(data_available_until)
        ]
        if not eligible_times:
            continue
        latest_signal_time = max(eligible_times)
        if int(data_available_until) - latest_signal_time > 6 * 60 * 60 * 1000:
            continue
        latest_events = [
            event for event in events
            if int(event["signal_time"]) == latest_signal_time
        ]
        for event in latest_events:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO short_term_forward_trades (
                    calculation_version, strategy, ticker, direction, signal_time,
                    signal_price, score, confidence, timeframe_minutes,
                    hold_minutes, stop_pct, target_pct, hedge_ticker,
                    hedge_direction, hedge_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (CALCULATION_VERSION, event["strategy"], event["ticker"], event["direction"],
                 event["signal_time"], event["signal_price"], event["score"],
                 event["confidence"], event["timeframe_minutes"], event["hold_minutes"],
                 event["stop_pct"], event["target_pct"], event.get("hedge_ticker"),
                 event.get("hedge_direction"), event.get("hedge_ratio")),
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
            print("[Short-Term Lab] step 1/10: refresh_reversal_candles", flush=True)
            refresh = await refresh_reversal_candles(conn)
            print("[Short-Term Lab] step 2/10: refresh_short_term_hourly_candles", flush=True)
            historical_refresh = await refresh_short_term_hourly_candles(conn)
            print(f"[Short-Term Lab] step 2/10 done: coverage_days={historical_refresh.get('coverage_days')}, inserted={historical_refresh.get('inserted')}", flush=True)
            print("[Short-Term Lab] step 3/10: refresh_short_term_funding_rates", flush=True)
            funding_refresh = await refresh_short_term_funding_rates(conn)
            print("[Short-Term Lab] step 4/10: refresh_short_term_perp_candles", flush=True)
            perp_refresh = await refresh_short_term_perp_candles(conn)
            print("[Short-Term Lab] step 5/10: loading candles from DB", flush=True)
            live_window_ms = 12 * 24 * 60 * 60 * 1000
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            candles = _load_candles(conn, since_ms=now_ms - live_window_ms)
            historical_candles = _load_hourly_candles(conn)
            funding = _load_funding_rates(conn)
            perp_candles = _load_perp_candles(conn)
            print(f"[Short-Term Lab] candles loaded: 5min={len(candles)}, 1h={len(historical_candles)}, perp={len(perp_candles)}", flush=True)
            if candles.empty:
                raise RuntimeError("MEXC did not return completed 5-minute candles")
            if historical_candles.empty:
                raise RuntimeError("MEXC did not return completed hourly candles")
            if perp_candles.empty:
                raise RuntimeError("MEXC did not return perpetual-futures candles")
            latest = int(candles["open_time"].max())
            age_minutes = (datetime.now(UTC).timestamp() * 1000 - latest) / 60_000
            if age_minutes > 20:
                raise RuntimeError(f"MEXC candles are stale by {age_minutes:.0f} minutes")
            latest_by_ticker = candles.groupby("ticker")["open_time"].max()
            fresh_tickers = int((latest_by_ticker >= latest - 10 * 60_000).sum())
            btc_latest = int(latest_by_ticker.get("BTC/USD", 0))
            eligible_tickers = int(refresh.get("eligible_ticker_count") or 0)
            minimum_fresh = max(24, math.ceil(eligible_tickers * 0.80))
            if fresh_tickers < minimum_fresh or btc_latest < latest - 10 * 60_000:
                raise RuntimeError(
                    "MEXC coverage is incomplete: "
                    f"{fresh_tickers}/{eligible_tickers} fresh tickers "
                    f"(minimum {minimum_fresh}), "
                    f"BTC fresh={btc_latest >= latest - 10 * 60_000}"
                )
            forward = _advance_forward(conn, candles)
            print("[Short-Term Lab] step 6/10: generate live candidates", flush=True)
            live_cutoff = latest - 10 * 24 * 60 * 60 * 1000
            live_candidates = generate_candidates(
                candles[candles["open_time"] >= live_cutoff],
                funding=funding, perp=perp_candles,
            )
            inserted = _insert_latest_candidates(
                conn,
                live_candidates,
                data_available_until=latest + FIVE_MINUTES_MS,
            )
            print(f"[Short-Term Lab] live candidates inserted={inserted}", flush=True)
            trades: list[dict] = []
            metrics: dict = {}
            if include_backtest:
                print("[Short-Term Lab] step 7/10: generate historical candidates", flush=True)
                historical_candidates = generate_candidates(
                    historical_candles, already_hourly=True,
                    funding=funding, perp=perp_candles,
                )
                total_candidates = sum(len(v) for v in historical_candidates.values())
                print(f"[Short-Term Lab] historical candidates total={total_candidates}", flush=True)
                coverage_days = int(historical_refresh.get("coverage_days") or 0)
                min_window = min(BACKTEST_WINDOWS_DAYS)
                if coverage_days < min_window:
                    raise RuntimeError(
                        f"Hourly history is incomplete: {coverage_days}/{min_window} days"
                    )
                completed_before = (
                    int(datetime.now(UTC).timestamp() * 1000)
                    // FIVE_MINUTES_MS * FIVE_MINUTES_MS
                )
                cutoff = completed_before - max(BACKTEST_WINDOWS_DAYS) * 24 * 60 * 60 * 1000
                all_eligible: dict[str, list[dict]] = {}
                for strategy in STRATEGIES:
                    eligible = [
                        event
                        for event in historical_candidates.get(strategy, [])
                        if int(event["signal_time"]) >= cutoff
                        and int(event["signal_time"])
                        + int(event["hold_minutes"]) * 60_000 < completed_before
                    ]
                    all_eligible[strategy] = (
                        _select_non_overlapping_candidates(eligible)
                        if eligible else []
                    )
                flat_candidates = [
                    candidate
                    for candidates in all_eligible.values()
                    for candidate in candidates
                ]
                # Free large DataFrames before download to avoid OOM on memory-constrained hosts.
                hc_data_start = int(historical_candles["open_time"].min())
                hc_data_end = int(historical_candles["open_time"].max())
                hc_count = len(historical_candles)
                del candles, historical_candles, funding, perp_candles
                import gc; gc.collect()
                print(f"[Short-Term Lab] step 8/10: download execution candles for {len(flat_candidates)} candidates", flush=True)
                execution_refresh = await refresh_short_term_execution_candles(
                    conn, flat_candidates
                )
                if execution_refresh.get("failures"):
                    raise RuntimeError(
                        "5-minute execution download failed: "
                        f"{len(execution_refresh.get('failures') or [])} failures"
                    )
                missing_windows = int(execution_refresh.get("missing_window_count") or 0)
                if missing_windows > 0:
                    # MEXC historical 5-minute data has gaps for some tickers/windows.
                    # These candidates are skipped by _simulate (counted in
                    # missing_executions) — they do not invalidate the whole run.
                    pass
                earliest_signal = min(
                    (int(c["signal_time"]) for c in flat_candidates),
                    default=0,
                )
                candles = _load_candles(conn, since_ms=earliest_signal)
                print(f"[Short-Term Lab] step 9/10: backtest (5min candles={len(candles)})", flush=True)
                trades, metrics = backtest(candles, all_eligible)
                print(f"[Short-Term Lab] step 9/10 done: trades={len(trades)}, metrics={len(metrics)}", flush=True)
                for metric in metrics.values():
                    metric["coverage_days"] = coverage_days
                    metric["is_complete"] = coverage_days >= int(
                        metric.get("window_days") or 0
                    )
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
                    gross_return_pct, cost_pct, net_return_pct, cash_result,
                    hedge_ticker, hedge_direction, hedge_ratio, hedge_entry_price,
                    hedge_exit_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(run_id, trade["strategy"], trade["ticker"], trade["direction"],
                  trade["signal_time"], trade["entry_time"], trade["entry_price"],
                  trade["exit_time"], trade["exit_price"], trade["exit_reason"],
                  trade["score"], trade["confidence"], trade["gross_return_pct"],
                  trade["cost_pct"], trade["net_return_pct"], trade["cash_result"],
                  trade.get("hedge_ticker"), trade.get("hedge_direction"),
                  trade.get("hedge_ratio"), trade.get("hedge_entry_price"),
                  trade.get("hedge_exit_price"))
                 for trade in trades],
            )
            payload = {
                "strategies": metrics,
                "forward": {**forward, "pending": inserted},
                "coverage_days": int(historical_refresh.get("coverage_days") or 0),
                "historical_refresh": historical_refresh,
                "funding_refresh": funding_refresh,
                "perp_refresh": perp_refresh,
                "execution_refresh": execution_refresh if include_backtest else None,
            }
            conn.execute(
                """
                UPDATE short_term_runs
                SET status='completed', completed_at=datetime('now'), data_start=?,
                    data_end=?, candle_count=?, metrics_json=? WHERE id=?
                """,
                (hc_data_start, hc_data_end, hc_count, json.dumps(payload), run_id),
            )
            conn.commit()
            print("[Short-Term Lab] step 10/10: completed, run persisted", flush=True)
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


def _build_strategy_cards(strategy_metrics: dict) -> list[dict]:
    strategy_cards = []
    for strategy_key, settings in STRATEGIES.items():
        for days in (30, 90, 180, 365):
            key = f"{strategy_key}_{days}d"
            card = {
                "key": key,
                "strategy_key": strategy_key,
                "stop_pct": settings["stop"],
                "target_pct": settings["target"],
                **settings,
                "short_name": f"{settings['short_name']} · {days} дн.",
            }
            card.update(strategy_metrics.get(key, {}))
            strategy_cards.append(card)
        # Average per week across all data (365d)
        full_key = f"{strategy_key}_365d"
        full = strategy_metrics.get(full_key, {})
        weekly_card = {
            "key": f"{strategy_key}_weekly",
            "strategy_key": strategy_key,
            "stop_pct": settings["stop"],
            "target_pct": settings["target"],
            **settings,
            "short_name": f"{settings['short_name']} · ср./неделю",
            "window_days": 365,
            "is_weekly_avg": True,
            "is_complete": full.get("is_complete", True),
            "coverage_days": full.get("coverage_days", 365),
            "net_cash": full.get("net_cash", 0),
            "avg_weekly_cash": full.get("avg_weekly_cash", 0),
            "trades": full.get("trades", 0),
            "trades_per_day": full.get("trades_per_day", 0),
            "win_rate": full.get("win_rate", 0),
            "wins": full.get("wins", 0),
            "profit_factor": full.get("profit_factor", 0),
        }
        strategy_cards.append(weekly_card)
    return strategy_cards


def build_strategy_cards_for_report(strategy_metrics: dict) -> list[dict]:
    return _build_strategy_cards(strategy_metrics)


_RECALC_LOCK = Lock()
_recalc_cache: dict[tuple, dict] = {}


def recalc_short_term_report(
    db_path: str, *, stop_pct: float, target_pct: float
) -> dict:
    """Re-run the backtest for every strategy with custom stop/target overrides.

    Uses the completed run's already-downloaded execution candles so the
    recalculation is fast and does not hit the network. Results are cached by
    (stop, target) to avoid repeated heavy backtests.
    """
    key = (round(float(stop_pct), 2), round(float(target_pct), 2))
    cached = _recalc_cache.get(key)
    if cached is not None:
        return cached
    with _RECALC_LOCK:
        # double-checked after acquiring the lock
        cached = _recalc_cache.get(key)
        if cached is not None:
            return cached
        result = _recalc_short_term_report_impl(db_path, stop_pct, target_pct)
        if len(_recalc_cache) >= 32:
            _recalc_cache.clear()
        _recalc_cache[key] = result
        return result


def _recalc_short_term_report_impl(
    db_path: str, *, stop_pct: float, target_pct: float
) -> dict:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        ensure_short_term_schema(conn)
        latest_ms = int(datetime.now(UTC).timestamp() * 1000)
        completed_before = latest_ms // FIVE_MINUTES_MS * FIVE_MINUTES_MS
        # Candidate signals still need the full hourly window; execution bars are
        # read from the reversal (5-min) start time.
        hourly = _load_hourly_candles(conn)
        funding = _load_funding_rates(conn)
        perp_candles = _load_perp_candles(conn)
        if hourly.empty:
            raise ValueError("No hourly candles stored yet")
        latest_hourly = int(hourly["open_time"].max())
        cutoff = latest_hourly - max(BACKTEST_WINDOWS_DAYS) * 24 * 60 * 60 * 1000
        historical_candidates = generate_candidates(
            hourly, already_hourly=True, funding=funding, perp=perp_candles
        )
        all_eligible: dict[str, list[dict]] = {}
        for strategy in STRATEGIES:
            eligible = [
                event
                for event in historical_candidates.get(strategy, [])
                if int(event["signal_time"]) >= cutoff
            ]
            all_eligible[strategy] = (
                _select_non_overlapping_candidates(eligible) if eligible else []
            )
        # Override the per-strategy stop/target coming from _candidate defaults.
        override = {
            "stop_pct": stop_pct,
            "target_pct": target_pct,
        }
        for events in all_eligible.values():
            for event in events:
                event.update(override)
        flat_candidates = [
            candidate
            for candidates in all_eligible.values()
            for candidate in candidates
        ]
        del hourly, funding, perp_candles
        import gc; gc.collect()
        if not flat_candidates:
            return {"strategies": {}}
        earliest_signal = min(int(c["signal_time"]) for c in flat_candidates)
        candles = _load_candles(conn, since_ms=earliest_signal)
        if candles.empty:
            return {"strategies": {}}
        trades, metrics = backtest(candles, all_eligible)
        coverage_days = 0
        if metrics:
            # reuse coverage from any single metric group
            for metric in metrics.values():
                coverage_days = int(metric.get("coverage_days") or 0)
                break
        return {"strategies": metrics, "coverage_days": coverage_days}
    finally:
        conn.close()


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
        started_at = latest_dict.get("started_at")
        try:
            started = datetime.fromisoformat(str(started_at)).replace(tzinfo=UTC)
            age_minutes = (datetime.now(UTC) - started).total_seconds() / 60
        except (TypeError, ValueError):
            age_minutes = 0
        latest_dict["is_stale"] = bool(
            latest_dict.get("status") == "running" and age_minutes > 30
        )
    completed_dict = dict(completed) if completed else None
    if completed_dict:
        completed_dict["data_label"] = _format_time(completed_dict.get("data_end"))
        completed_dict["coverage_days"] = int(metrics.get("coverage_days") or 0)
    strategy_metrics = metrics.get("strategies", {})
    strategy_cards = _build_strategy_cards(strategy_metrics)
    return {
        "version": CALCULATION_VERSION,
        "latest": latest_dict,
        "completed": completed_dict,
        "is_ready": completed is not None,
        "strategies": strategy_cards,
        "open": [trade_dict(row) for row in open_rows],
        "closed": [trade_dict(row) for row in closed_rows],
        "settings": {
            "stake": STAKE_USD,
            "cost_pct": ROUND_TRIP_COST_PCT,
            "stop": STRATEGIES["rs_low_cost"]["stop"],
            "target": STRATEGIES["rs_low_cost"]["target"],
            "universe_size": len(INTRADAY_TICKERS),
        },
    }
