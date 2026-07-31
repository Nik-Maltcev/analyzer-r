"""Point-in-time 5-minute mean-reversion research for crypto."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from app.data.mexc_intraday import refresh_reversal_candles
from app.db.schema import (
    CREATE_REVERSAL_CANDLES,
    CREATE_REVERSAL_RUNS,
    CREATE_REVERSAL_TRADES,
)

STRATEGY_VERSION = "reversal-5m-v1"
STAKE_USD = 100.0
ROUND_TRIP_COST_PCT = 0.30
SHOCK_RETURN_PCT = 1.50
SHOCK_Z = 3.0
MIN_VOLUME_RATIO = 2.0
TARGET_PCT = 0.80
STOP_PCT = 0.80
MAX_HOLD_BARS = 6
LOOKBACK_BARS = 7 * 24 * 12
MIN_LOOKBACK_BARS = 24 * 12


def ensure_reversal_schema(conn: sqlite3.Connection) -> None:
    for statement in (
        CREATE_REVERSAL_CANDLES,
        CREATE_REVERSAL_RUNS,
        CREATE_REVERSAL_TRADES,
    ):
        conn.execute(statement)
    conn.commit()


def _as_float(value: float) -> float:
    return float(value) if math.isfinite(float(value)) else 0.0


def backtest_reversal(candles: pd.DataFrame) -> tuple[list[dict], dict]:
    """Backtest with shifted baselines, next-bar confirmation and close exits."""
    if candles.empty:
        return [], _metrics([])
    frame = candles.copy().sort_values(["open_time", "ticker"])
    closes = frame.pivot(index="open_time", columns="ticker", values="close")
    volumes = frame.pivot(index="open_time", columns="ticker", values="volume")
    returns = closes.pct_change(fill_method=None)
    btc_returns = returns.get("BTC/USD")
    trades: list[dict] = []

    for ticker in closes.columns:
        prices = closes[ticker].dropna()
        ticker_volume = volumes[ticker].reindex(prices.index)
        coin_returns = prices.pct_change(fill_method=None)
        residual = (
            coin_returns
            if ticker == "BTC/USD" or btc_returns is None
            else coin_returns - btc_returns.reindex(prices.index)
        )
        baseline_std = residual.rolling(
            LOOKBACK_BARS, min_periods=MIN_LOOKBACK_BARS
        ).std().shift(1)
        volume_median = ticker_volume.rolling(
            LOOKBACK_BARS, min_periods=MIN_LOOKBACK_BARS
        ).median().shift(1)
        shock_z = residual / baseline_std.replace(0, np.nan)
        volume_ratio = ticker_volume / volume_median.replace(0, np.nan)
        next_free = 0

        for index in range(MIN_LOOKBACK_BARS, len(prices) - MAX_HOLD_BARS - 1):
            if index < next_free:
                continue
            residual_value = residual.iloc[index]
            z_value = shock_z.iloc[index]
            volume_value = volume_ratio.iloc[index]
            if not all(pd.notna(value) for value in (residual_value, z_value, volume_value)):
                continue
            if (
                abs(float(residual_value)) * 100 < SHOCK_RETURN_PCT
                or abs(float(z_value)) < SHOCK_Z
                or float(volume_value) < MIN_VOLUME_RATIO
            ):
                continue
            shock_price = float(prices.iloc[index])
            confirmation_price = float(prices.iloc[index + 1])
            if not math.isfinite(shock_price) or not math.isfinite(confirmation_price):
                continue
            if residual_value < 0 and confirmation_price > shock_price:
                direction = "long"
            elif residual_value > 0 and confirmation_price < shock_price:
                direction = "short"
            else:
                continue

            entry_price = confirmation_price
            exit_index = index + 1 + MAX_HOLD_BARS
            exit_reason = "time"
            gross_pct = 0.0
            for candidate in range(index + 2, exit_index + 1):
                exit_price = float(prices.iloc[candidate])
                if not math.isfinite(exit_price):
                    continue
                if direction == "long":
                    gross_pct = (exit_price / entry_price - 1) * 100
                else:
                    gross_pct = (entry_price - exit_price) / entry_price * 100
                if gross_pct >= TARGET_PCT:
                    exit_index, exit_reason = candidate, "target"
                    break
                if gross_pct <= -STOP_PCT:
                    exit_index, exit_reason = candidate, "stop"
                    break
            exit_price = float(prices.iloc[exit_index])
            if direction == "long":
                gross_pct = (exit_price / entry_price - 1) * 100
            else:
                gross_pct = (entry_price - exit_price) / entry_price * 100
            net_pct = gross_pct - ROUND_TRIP_COST_PCT
            trades.append({
                "ticker": ticker,
                "direction": direction,
                "shock_time": int(prices.index[index]),
                "shock_return_pct": _as_float(float(coin_returns.iloc[index]) * 100),
                "residual_return_pct": _as_float(float(residual_value) * 100),
                "shock_z": _as_float(z_value),
                "volume_ratio": _as_float(volume_value),
                "entry_time": int(prices.index[index + 1]),
                "entry_price": entry_price,
                "exit_time": int(prices.index[exit_index]),
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "gross_return_pct": gross_pct,
                "cost_pct": ROUND_TRIP_COST_PCT,
                "net_return_pct": net_pct,
                "cash_result": STAKE_USD * net_pct / 100,
            })
            next_free = exit_index + 1
    trades.sort(key=lambda item: item["exit_time"])
    return trades, _metrics(trades)


def _metrics(trades: list[dict]) -> dict:
    results = [float(item["cash_result"]) for item in trades]
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    equity = np.cumsum(results) if results else np.array([], dtype=float)
    peak = np.maximum.accumulate(np.insert(equity, 0, 0.0))[1:] if results else equity
    drawdown = equity - peak if results else equity
    return {
        "trades": len(results),
        "wins": len(wins),
        "win_rate": len(wins) / len(results) * 100 if results else 0.0,
        "net_cash": sum(results),
        "average_net_pct": (
            sum(float(item["net_return_pct"]) for item in trades) / len(trades)
            if trades else 0.0
        ),
        "profit_factor": (
            sum(wins) / abs(sum(losses)) if losses else (999.0 if wins else 0.0)
        ),
        "max_drawdown": abs(float(drawdown.min())) if len(drawdown) else 0.0,
        "long_trades": sum(item["direction"] == "long" for item in trades),
        "short_trades": sum(item["direction"] == "short" for item in trades),
    }


def _load_candles(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT ticker, open_time, close, volume
        FROM reversal_candles
        ORDER BY open_time, ticker
        """,
        conn,
    )


async def refresh_and_backtest(db_path: str) -> dict:
    """Collect candles and persist one immutable research run."""
    conn = sqlite3.connect(db_path, timeout=60)
    ensure_reversal_schema(conn)
    stale_before = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        UPDATE reversal_runs
        SET status='failed', completed_at=datetime('now'),
            error=COALESCE(error, 'Refresh process stopped before completion')
        WHERE status='running' AND started_at < ?
        """,
        (stale_before,),
    )
    conn.commit()
    active = conn.execute(
        "SELECT id FROM reversal_runs WHERE status='running' AND started_at >= ? ORDER BY id DESC LIMIT 1",
        (stale_before,),
    ).fetchone()
    if active:
        conn.close()
        return {"status": "running", "run_id": int(active[0])}
    cursor = conn.execute(
        "INSERT INTO reversal_runs(strategy_version, status) VALUES (?, 'running')",
        (STRATEGY_VERSION,),
    )
    run_id = int(cursor.lastrowid)
    conn.commit()
    try:
        collection = await refresh_reversal_candles(conn)
        if collection["ticker_count"] < 10:
            raise RuntimeError(
                "MEXC intraday coverage is incomplete: "
                f"{collection['ticker_count']}/12 tickers"
            )
        if (
            collection.get("data_end") is None
            or int(collection["data_end"])
            < int(collection["completed_before"]) - 30 * 60 * 1000
        ):
            raise RuntimeError("MEXC intraday candles are stale")
        candles = _load_candles(conn)
        trades, metrics = await asyncio.to_thread(backtest_reversal, candles)
        conn.execute("DELETE FROM reversal_trades WHERE run_id = ?", (run_id,))
        conn.executemany(
            """
            INSERT INTO reversal_trades (
                run_id, strategy_version, ticker, direction, shock_time,
                shock_return_pct, residual_return_pct, shock_z, volume_ratio,
                entry_time, entry_price, exit_time, exit_price, exit_reason,
                gross_return_pct, cost_pct, net_return_pct, cash_result
            ) VALUES (
                :run_id, :strategy_version, :ticker, :direction, :shock_time,
                :shock_return_pct, :residual_return_pct, :shock_z, :volume_ratio,
                :entry_time, :entry_price, :exit_time, :exit_price, :exit_reason,
                :gross_return_pct, :cost_pct, :net_return_pct, :cash_result
            )
            """,
            [dict(item, run_id=run_id, strategy_version=STRATEGY_VERSION) for item in trades],
        )
        start_ms = collection.get("data_start")
        end_ms = collection.get("data_end")
        conn.execute(
            """
            UPDATE reversal_runs SET status='completed', completed_at=datetime('now'),
                data_start=?, data_end=?, candle_count=?, trade_count=?, metrics_json=?, error=?
            WHERE id=?
            """,
            (
                _date_label(start_ms), _date_label(end_ms),
                collection["candle_count"], len(trades),
                json.dumps(metrics, ensure_ascii=False),
                json.dumps(collection["failures"], ensure_ascii=False) if collection["failures"] else None,
                run_id,
            ),
        )
        conn.commit()
        return {"status": "completed", "run_id": run_id, **metrics}
    except Exception as exc:
        conn.execute(
            "UPDATE reversal_runs SET status='failed', completed_at=datetime('now'), error=? WHERE id=?",
            (str(exc)[:1000], run_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def _date_label(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, UTC).date().isoformat()


def get_reversal_report(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_reversal_schema(conn)
    stale_before = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    running = conn.execute(
        """
        SELECT * FROM reversal_runs
        WHERE status='running' AND started_at >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (stale_before,),
    ).fetchone()
    latest = conn.execute(
        "SELECT * FROM reversal_runs WHERE status IN ('completed','failed') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    trades: list[dict] = []
    metrics: dict = {}
    if latest and latest["status"] == "completed":
        metrics = json.loads(latest["metrics_json"] or "{}")
        rows = conn.execute(
            "SELECT * FROM reversal_trades WHERE run_id=? ORDER BY exit_time DESC LIMIT 30",
            (latest["id"],),
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["entry_label"] = datetime.fromtimestamp(item["entry_time"] / 1000, UTC).strftime("%d.%m %H:%M")
            item["exit_label"] = datetime.fromtimestamp(item["exit_time"] / 1000, UTC).strftime("%d.%m %H:%M")
            trades.append(item)
    report = {
        "running": bool(running),
        "latest": dict(latest) if latest else None,
        "metrics": metrics,
        "trades": trades,
        "settings": {
            "history_days": 90,
            "cost_pct": ROUND_TRIP_COST_PCT,
            "stake": STAKE_USD,
            "target_pct": TARGET_PCT,
            "stop_pct": STOP_PCT,
            "hold_minutes": MAX_HOLD_BARS * 5,
        },
    }
    conn.close()
    return report
