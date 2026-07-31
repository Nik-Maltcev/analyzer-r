"""Point-in-time 5-minute mean-reversion research for crypto."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from app.data.mexc_intraday import REVERSAL_TICKERS, refresh_reversal_candles
from app.db.schema import (
    CREATE_REVERSAL_CANDLES,
    CREATE_REVERSAL_FORWARD_STATE,
    CREATE_REVERSAL_FORWARD_NOTIFICATIONS,
    CREATE_REVERSAL_FORWARD_TRADES,
    CREATE_REVERSAL_RUNS,
    CREATE_REVERSAL_TRADES,
)

STRATEGY_VERSION = "reversal-5m-v2"
STAKE_USD = 100.0
ROUND_TRIP_COST_PCT = 0.30
SHOCK_RETURN_PCT = 1.50
SHOCK_Z = 3.0
MIN_VOLUME_RATIO = 2.0
MIN_DAILY_QUOTE_VOLUME_USD = 5_000_000.0
MIN_CANDLE_COVERAGE = 0.95
DAILY_BARS = 24 * 12
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
        CREATE_REVERSAL_FORWARD_STATE,
        CREATE_REVERSAL_FORWARD_TRADES,
        CREATE_REVERSAL_FORWARD_NOTIFICATIONS,
    ):
        conn.execute(statement)
    conn.commit()


def _as_float(value: float) -> float:
    return float(value) if math.isfinite(float(value)) else 0.0


def _directional_return_pct(direction: str, entry_price: float, price: float) -> float:
    if direction == "long":
        return (price / entry_price - 1) * 100
    return (entry_price - price) / entry_price * 100


def _queue_forward_notification(
    conn: sqlite3.Connection,
    trade_id: int,
    event_type: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO reversal_forward_notifications (
            strategy_version, trade_id, event_type
        ) VALUES (?, ?, ?)
        """,
        (STRATEGY_VERSION, trade_id, event_type),
    )


def find_latest_confirmed_signals(
    candles: pd.DataFrame,
    confirmation_time: int,
) -> list[dict]:
    """Return signals that became tradable on one completed confirmation bar."""
    if candles.empty:
        return []
    frame = candles.copy().sort_values(["open_time", "ticker"])
    closes = frame.pivot(index="open_time", columns="ticker", values="close")
    volumes = frame.pivot(index="open_time", columns="ticker", values="volume")
    quote_volumes = (
        frame.pivot(index="open_time", columns="ticker", values="quote_volume")
        if "quote_volume" in frame.columns
        else volumes * closes
    )
    returns = closes.pct_change(fill_method=None)
    btc_returns = returns.get("BTC/USD")
    signals: list[dict] = []

    for ticker in closes.columns:
        prices = closes[ticker].dropna()
        if prices.reindex(closes.index).notna().mean() < MIN_CANDLE_COVERAGE:
            continue
        if confirmation_time not in prices.index:
            continue
        confirmation_index = int(prices.index.get_loc(confirmation_time))
        shock_index = confirmation_index - 1
        if shock_index < MIN_LOOKBACK_BARS:
            continue
        ticker_volume = volumes[ticker].reindex(prices.index)
        daily_quote_volume = quote_volumes[ticker].reindex(prices.index).rolling(
            DAILY_BARS, min_periods=DAILY_BARS
        ).sum().shift(1)
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
        residual_value = residual.iloc[shock_index]
        z_value = shock_z.iloc[shock_index]
        volume_value = volume_ratio.iloc[shock_index]
        liquidity_value = daily_quote_volume.iloc[shock_index]
        if not all(pd.notna(value) for value in (
            residual_value, z_value, volume_value, liquidity_value
        )):
            continue
        if (
            abs(float(residual_value)) * 100 < SHOCK_RETURN_PCT
            or abs(float(z_value)) < SHOCK_Z
            or float(volume_value) < MIN_VOLUME_RATIO
            or float(liquidity_value) < MIN_DAILY_QUOTE_VOLUME_USD
        ):
            continue
        shock_price = float(prices.iloc[shock_index])
        confirmation_price = float(prices.iloc[confirmation_index])
        if residual_value < 0 and confirmation_price > shock_price:
            direction = "long"
        elif residual_value > 0 and confirmation_price < shock_price:
            direction = "short"
        else:
            continue
        signals.append({
            "ticker": ticker,
            "direction": direction,
            "shock_time": int(prices.index[shock_index]),
            "shock_return_pct": _as_float(float(coin_returns.iloc[shock_index]) * 100),
            "residual_return_pct": _as_float(float(residual_value) * 100),
            "shock_z": _as_float(z_value),
            "volume_ratio": _as_float(volume_value),
            "entry_time": confirmation_time,
            "entry_price": confirmation_price,
        })
    return signals


def backtest_reversal(candles: pd.DataFrame) -> tuple[list[dict], dict]:
    """Backtest with shifted baselines, next-bar confirmation and close exits."""
    if candles.empty:
        return [], _metrics([])
    frame = candles.copy().sort_values(["open_time", "ticker"])
    closes = frame.pivot(index="open_time", columns="ticker", values="close")
    volumes = frame.pivot(index="open_time", columns="ticker", values="volume")
    quote_volumes = (
        frame.pivot(index="open_time", columns="ticker", values="quote_volume")
        if "quote_volume" in frame.columns
        else volumes * closes
    )
    returns = closes.pct_change(fill_method=None)
    btc_returns = returns.get("BTC/USD")
    trades: list[dict] = []

    for ticker in closes.columns:
        prices = closes[ticker].dropna()
        if prices.reindex(closes.index).notna().mean() < MIN_CANDLE_COVERAGE:
            continue
        ticker_volume = volumes[ticker].reindex(prices.index)
        daily_quote_volume = quote_volumes[ticker].reindex(prices.index).rolling(
            DAILY_BARS, min_periods=DAILY_BARS
        ).sum().shift(1)
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
            liquidity_value = daily_quote_volume.iloc[index]
            if not all(pd.notna(value) for value in (
                residual_value, z_value, volume_value, liquidity_value
            )):
                continue
            if (
                abs(float(residual_value)) * 100 < SHOCK_RETURN_PCT
                or abs(float(z_value)) < SHOCK_Z
                or float(volume_value) < MIN_VOLUME_RATIO
                or float(liquidity_value) < MIN_DAILY_QUOTE_VOLUME_USD
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
        SELECT ticker, open_time, close, volume, quote_volume
        FROM reversal_candles
        ORDER BY open_time, ticker
        """,
        conn,
    )


def process_reversal_forward(conn: sqlite3.Connection) -> dict:
    """Advance the shadow journal using only candles unseen by this process."""
    conn.row_factory = sqlite3.Row
    ensure_reversal_schema(conn)
    latest_row = conn.execute(
        "SELECT MAX(open_time) FROM reversal_candles WHERE ticker='BTC/USD'"
    ).fetchone()
    latest_time = int(latest_row[0]) if latest_row and latest_row[0] is not None else None
    if latest_time is None:
        return {"status": "waiting_for_candles", "opened": 0, "closed": 0}

    state = conn.execute(
        "SELECT * FROM reversal_forward_state WHERE strategy_version=?",
        (STRATEGY_VERSION,),
    ).fetchone()
    if state is None:
        conn.execute(
            """
            INSERT INTO reversal_forward_state(strategy_version, last_confirmation_time)
            VALUES (?, ?)
            """,
            (STRATEGY_VERSION, latest_time),
        )
        conn.commit()
        return {
            "status": "initialized",
            "watermark": latest_time,
            "opened": 0,
            "closed": 0,
        }

    last_confirmation_time = int(state["last_confirmation_time"])
    active_rows = conn.execute(
        """
        SELECT * FROM reversal_forward_trades
        WHERE strategy_version=? AND status='active'
        ORDER BY entry_time, id
        """,
        (STRATEGY_VERSION,),
    ).fetchall()
    closed = 0
    closed_tickers: set[str] = set()
    for row in active_rows:
        trade = dict(row)
        bars = conn.execute(
            """
            SELECT open_time, close FROM reversal_candles
            WHERE ticker=? AND open_time>? AND open_time<=?
            ORDER BY open_time
            """,
            (trade["ticker"], trade["last_evaluated_time"], latest_time),
        ).fetchall()
        bars_held = int(trade["bars_held"])
        for candle_time, candle_close in bars:
            price = float(candle_close)
            bars_held += 1
            gross_pct = _directional_return_pct(
                trade["direction"], float(trade["entry_price"]), price
            )
            net_pct = gross_pct - ROUND_TRIP_COST_PCT
            reason = None
            if gross_pct >= TARGET_PCT:
                reason = "target"
            elif gross_pct <= -STOP_PCT:
                reason = "stop"
            elif bars_held >= MAX_HOLD_BARS:
                reason = "time"
            if reason:
                conn.execute(
                    """
                    UPDATE reversal_forward_trades
                    SET status='closed', last_evaluated_time=?, bars_held=?,
                        last_price=?, current_gross_return_pct=?,
                        current_net_return_pct=?, current_cash_result=?,
                        exit_time=?, exit_price=?, exit_reason=?,
                        gross_return_pct=?, net_return_pct=?, cash_result=?,
                        updated_at=datetime('now')
                    WHERE id=? AND status='active'
                    """,
                    (
                        candle_time, bars_held, price, gross_pct, net_pct,
                        STAKE_USD * net_pct / 100, candle_time, price, reason,
                        gross_pct, net_pct, STAKE_USD * net_pct / 100,
                        trade["id"],
                    ),
                )
                _queue_forward_notification(conn, int(trade["id"]), "closed")
                closed += 1
                closed_tickers.add(str(trade["ticker"]))
                break
            conn.execute(
                """
                UPDATE reversal_forward_trades
                SET last_evaluated_time=?, bars_held=?, last_price=?,
                    current_gross_return_pct=?, current_net_return_pct=?,
                    current_cash_result=?, updated_at=datetime('now')
                WHERE id=? AND status='active'
                """,
                (
                    candle_time, bars_held, price, gross_pct, net_pct,
                    STAKE_USD * net_pct / 100, trade["id"],
                ),
            )

    opened = 0
    if latest_time > last_confirmation_time:
        candles = _load_candles(conn)
        signals = find_latest_confirmed_signals(candles, latest_time)
        active_tickers = {
            str(row[0]) for row in conn.execute(
                """
                SELECT ticker FROM reversal_forward_trades
                WHERE strategy_version=? AND status='active'
                """,
                (STRATEGY_VERSION,),
            ).fetchall()
        }
        for signal in signals:
            if signal["ticker"] in active_tickers or signal["ticker"] in closed_tickers:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO reversal_forward_trades (
                    strategy_version, ticker, direction, shock_time,
                    shock_return_pct, residual_return_pct, shock_z, volume_ratio,
                    entry_time, entry_price, last_evaluated_time, last_price,
                    current_gross_return_pct, current_net_return_pct,
                    current_cash_result, cost_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    STRATEGY_VERSION, signal["ticker"], signal["direction"],
                    signal["shock_time"], signal["shock_return_pct"],
                    signal["residual_return_pct"], signal["shock_z"],
                    signal["volume_ratio"], signal["entry_time"],
                    signal["entry_price"], signal["entry_time"],
                    signal["entry_price"], -ROUND_TRIP_COST_PCT,
                    -STAKE_USD * ROUND_TRIP_COST_PCT / 100,
                    ROUND_TRIP_COST_PCT,
                ),
            )
            if cursor.rowcount:
                _queue_forward_notification(conn, int(cursor.lastrowid), "opened")
                opened += 1
                active_tickers.add(str(signal["ticker"]))

        conn.execute(
            """
            UPDATE reversal_forward_state
            SET last_confirmation_time=?, updated_at=datetime('now')
            WHERE strategy_version=?
            """,
            (latest_time, STRATEGY_VERSION),
        )
    conn.commit()
    return {
        "status": "processed",
        "watermark": latest_time,
        "opened": opened,
        "closed": closed,
    }


async def refresh_reversal_forward(db_path: str) -> dict:
    """Fetch missing completed candles and advance the forward journal."""
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        ensure_reversal_schema(conn)
        stale_before = (datetime.now(UTC) - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        running = conn.execute(
            """
            SELECT id FROM reversal_runs
            WHERE status='running' AND started_at>=?
            ORDER BY id DESC LIMIT 1
            """,
            (stale_before,),
        ).fetchone()
        if running:
            return {"status": "waiting_for_backtest", "run_id": int(running[0])}
        collection = await refresh_reversal_candles(conn)
        if collection.get("data_end") is None or int(collection["data_end"]) < (
            int(collection["completed_before"]) - 30 * 60 * 1000
        ):
            raise RuntimeError("MEXC forward candles are stale")
        result = process_reversal_forward(conn)
        return {**result, "collection": collection}
    finally:
        conn.close()


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
        minimum_tickers = math.ceil(len(REVERSAL_TICKERS) * 0.80)
        if collection["ticker_count"] < minimum_tickers:
            raise RuntimeError(
                "MEXC intraday coverage is incomplete: "
                f"{collection['ticker_count']}/{len(REVERSAL_TICKERS)} tickers"
            )
        if (
            collection.get("data_end") is None
            or int(collection["data_end"])
            < int(collection["completed_before"]) - 30 * 60 * 1000
        ):
            raise RuntimeError("MEXC intraday candles are stale")
        candles = _load_candles(conn)
        eligible_tickers = candles.groupby("ticker")["open_time"].nunique()
        expected_bars = max(1, candles["open_time"].nunique())
        eligible_count = int(
            (eligible_tickers / expected_bars >= MIN_CANDLE_COVERAGE).sum()
        )
        if eligible_count < minimum_tickers:
            raise RuntimeError(
                "MEXC candle completeness is insufficient: "
                f"{eligible_count}/{len(REVERSAL_TICKERS)} tickers have "
                f"at least {MIN_CANDLE_COVERAGE:.0%} coverage"
            )
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


def _forward_credibility(trades: list[dict]) -> dict:
    count = len(trades)
    wins = [trade for trade in trades if float(trade["cash_result"]) > 0]
    losses = [trade for trade in trades if float(trade["cash_result"]) < 0]
    win_rate = len(wins) / count if count else 0.0
    if count:
        z = 1.96
        denominator = 1 + z * z / count
        center = (win_rate + z * z / (2 * count)) / denominator
        margin = z * math.sqrt(
            win_rate * (1 - win_rate) / count + z * z / (4 * count * count)
        ) / denominator
        win_rate_low = max(0.0, center - margin) * 100
        win_rate_high = min(1.0, center + margin) * 100
    else:
        win_rate_low = win_rate_high = 0.0
    gross_wins = sum(float(trade["cash_result"]) for trade in wins)
    gross_losses = abs(sum(float(trade["cash_result"]) for trade in losses))
    profit_factor = gross_wins / gross_losses if gross_losses else (999.0 if wins else 0.0)
    equity = np.cumsum([float(trade["cash_result"]) for trade in trades])
    if len(equity):
        peaks = np.maximum.accumulate(np.insert(equity, 0, 0.0))[1:]
        max_drawdown = abs(float((equity - peaks).min()))
    else:
        max_drawdown = 0.0
    average_net_pct = (
        sum(float(trade["net_return_pct"]) for trade in trades) / count if count else 0.0
    )
    if count < 10:
        verdict = "Данных пока мало"
        verdict_tone = "neutral"
        verdict_detail = "Не делайте вывод о стратегии по нескольким сделкам."
    elif count < 30:
        verdict = "Предварительный результат"
        verdict_tone = "neutral"
        verdict_detail = "Нужны минимум 30 закрытых forward-сделок."
    elif profit_factor >= 1.2 and average_net_pct > 0:
        verdict = "Преимущество подтверждается"
        verdict_tone = "positive"
        verdict_detail = "После расходов результат положительный, но наблюдение продолжается."
    else:
        verdict = "Преимущество не подтверждено"
        verdict_tone = "negative"
        verdict_detail = "Текущий forward-тест не показывает устойчивой прибыли после расходов."
    return {
        "sample": count,
        "sample_target": 30,
        "sample_progress": min(count / 30 * 100, 100.0),
        "profit_factor": profit_factor,
        "average_win_pct": (
            sum(float(trade["net_return_pct"]) for trade in wins) / len(wins)
            if wins else 0.0
        ),
        "average_loss_pct": (
            sum(float(trade["net_return_pct"]) for trade in losses) / len(losses)
            if losses else 0.0
        ),
        "max_drawdown": max_drawdown,
        "win_rate_low": win_rate_low,
        "win_rate_high": win_rate_high,
        "long_trades": sum(trade["direction"] == "long" for trade in trades),
        "short_trades": sum(trade["direction"] == "short" for trade in trades),
        "verdict": verdict,
        "verdict_tone": verdict_tone,
        "verdict_detail": verdict_detail,
    }


def _forward_report(conn: sqlite3.Connection) -> dict:
    state = conn.execute(
        "SELECT * FROM reversal_forward_state WHERE strategy_version=?",
        (STRATEGY_VERSION,),
    ).fetchone()
    active_rows = conn.execute(
        """
        SELECT * FROM reversal_forward_trades
        WHERE strategy_version=? AND status='active'
        ORDER BY entry_time DESC
        """,
        (STRATEGY_VERSION,),
    ).fetchall()
    closed_rows = conn.execute(
        """
        SELECT * FROM reversal_forward_trades
        WHERE strategy_version=? AND status='closed'
        ORDER BY exit_time DESC, id DESC LIMIT 30
        """,
        (STRATEGY_VERSION,),
    ).fetchall()
    summary = conn.execute(
        """
        SELECT COUNT(*) AS trades,
               SUM(CASE WHEN cash_result>0 THEN 1 ELSE 0 END) AS wins,
               COALESCE(SUM(cash_result), 0) AS net_cash,
               COALESCE(AVG(net_return_pct), 0) AS average_net_pct
        FROM reversal_forward_trades
        WHERE strategy_version=? AND status='closed'
        """,
        (STRATEGY_VERSION,),
    ).fetchone()
    credibility_rows = conn.execute(
        """
        SELECT direction, net_return_pct, cash_result
        FROM reversal_forward_trades
        WHERE strategy_version=? AND status='closed'
        ORDER BY exit_time, id
        """,
        (STRATEGY_VERSION,),
    ).fetchall()

    def serialize(row: sqlite3.Row, *, closed: bool) -> dict:
        item = dict(row)
        item["entry_label"] = datetime.fromtimestamp(
            item["entry_time"] / 1000, UTC
        ).strftime("%d.%m %H:%M")
        if closed:
            item["exit_label"] = datetime.fromtimestamp(
                item["exit_time"] / 1000, UTC
            ).strftime("%d.%m %H:%M")
        else:
            item["updated_label"] = datetime.fromtimestamp(
                item["last_evaluated_time"] / 1000, UTC
            ).strftime("%d.%m %H:%M")
        return item

    closed_count = int(summary["trades"] or 0)
    wins = int(summary["wins"] or 0)
    last_confirmation_time = int(state["last_confirmation_time"]) if state else None
    freshness_minutes = (
        max(0.0, (datetime.now(UTC).timestamp() * 1000 - last_confirmation_time) / 60000)
        if last_confirmation_time else None
    )
    return {
        "initialized": state is not None,
        "initialized_at": state["initialized_at"] if state else None,
        "updated_at": state["updated_at"] if state else None,
        "data_label": (
            datetime.fromtimestamp(last_confirmation_time / 1000, UTC).strftime("%d.%m %H:%M UTC")
            if last_confirmation_time else None
        ),
        "freshness_minutes": freshness_minutes,
        "data_stale": freshness_minutes is not None and freshness_minutes > 15,
        "active": [serialize(row, closed=False) for row in active_rows],
        "closed": [serialize(row, closed=True) for row in closed_rows],
        "metrics": {
            "active": len(active_rows),
            "closed": closed_count,
            "wins": wins,
            "win_rate": wins / closed_count * 100 if closed_count else 0.0,
            "net_cash": float(summary["net_cash"] or 0),
            "average_net_pct": float(summary["average_net_pct"] or 0),
        },
        "credibility": _forward_credibility([dict(row) for row in credibility_rows]),
    }


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
        "forward": _forward_report(conn),
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
