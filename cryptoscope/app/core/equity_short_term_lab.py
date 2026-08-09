"""Point-in-time Short-Term Lab for daily equity data stored in ``prices``.

The equity database contains completed daily closes and volume, not intraday
OHLC.  Signals are therefore formed after a daily close, entered at the next
available session close and held for five further trading sessions.  This is a
deliberately separate calculation path from the MEXC crypto lab.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from threading import Lock

import numpy as np
import pandas as pd

from app.core.short_term_lab import ensure_short_term_schema


STAKE_USD = 100.0
HOLD_SESSIONS = 5
WINDOWS_DAYS = (30, 90, 180, 365)
DAY_MS = 86_400_000
STOP_GRID = (1.0, 2.0, 3.0, 5.0, 8.0)
TARGET_GRID = (1.0, 2.0, 3.0, 5.0, 8.0, 12.0)
OPTIMIZER_TRAIN_SHARE = 0.70
OPTIMIZER_MIN_TRAIN_TRADES = 8
OPTIMIZER_MIN_VALIDATION_TRADES = 4

MARKETS = {
    "ru": {
        "version": "short-term-daily-ru-v1",
        "label": "RU",
        "source": "MOEX ISS",
        "cost_pct": 0.20,
        "benchmark": None,
    },
    "stocks": {
        "version": "short-term-daily-stocks-v1",
        "label": "Акции/ETF",
        "source": "Yahoo Finance",
        "cost_pct": 0.10,
        "benchmark": "SPY",
    },
}

STRATEGIES = {
    "daily_rs": {
        "name": "Относительная сила",
        "description": "Сильнейшие и слабейшие бумаги относительно рынка за 5/10/20 сессий.",
    },
    "daily_rs_liquid": {
        "name": "Относительная сила + ликвидность",
        "description": "Та же модель, но только среди верхних 60% рынка по обороту за 20 сессий.",
    },
    "daily_rs_regime": {
        "name": "Относительная сила + режим",
        "description": "LONG только выше SMA20 рынка, SHORT только ниже SMA20 рынка.",
    },
    "daily_momentum": {
        "name": "Momentum 3/7/14",
        "description": "Согласованный импульс завершённых дневных сессий.",
    },
}

_LOCKS = {market: Lock() for market in MARKETS}


def _config(market: str) -> dict:
    if market not in MARKETS:
        raise ValueError(f"Unsupported equity Short-Term market: {market}")
    return MARKETS[market]


def _load_prices(conn: sqlite3.Connection, market: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT ticker, date, close, volume
        FROM prices
        WHERE market=?
        ORDER BY date, ticker
        """,
        conn,
        params=(market,),
    )
    if frame.empty:
        return frame
    frame["time"] = pd.to_datetime(frame["date"], utc=True, errors="coerce").dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0)
    frame = frame.dropna(subset=["time", "close"])
    frame = frame[(frame["close"] > 0)].drop_duplicates(["ticker", "time"], keep="last")
    frame["time_ms"] = (frame["time"].astype("int64") // 1_000_000).astype("int64")
    return frame.sort_values(["time_ms", "ticker"]).reset_index(drop=True)


def _panels(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    close = frame.pivot(index="time_ms", columns="ticker", values="close").sort_index()
    volume = frame.pivot(index="time_ms", columns="ticker", values="volume").reindex(close.index)
    valid = close.notna().sum() >= 25
    return close.loc[:, valid], volume.loc[:, valid]


def _benchmark(close: pd.DataFrame, market: str) -> pd.Series:
    config = _config(market)
    ticker = config["benchmark"]
    if ticker and ticker in close.columns:
        return close[ticker]
    # MOEX index history is not stored in ``prices``.  The point-in-time
    # equal-weight market series is an explicit, reproducible proxy.
    market_return = close.pct_change(fill_method=None).median(axis=1, skipna=True).fillna(0)
    return (1 + market_return).cumprod() * 100


def _zscore_cross_section(values: pd.DataFrame) -> pd.DataFrame:
    mean = values.mean(axis=1, skipna=True)
    std = values.std(axis=1, ddof=0, skipna=True).replace(0, np.nan)
    return values.sub(mean, axis=0).div(std, axis=0)


def _confidence(score: float) -> str:
    return "high" if abs(score) >= 1.5 else "medium"


def _select_extremes(
    score: pd.DataFrame,
    prices: pd.DataFrame,
    strategy: str,
    *,
    allowed: pd.DataFrame | None = None,
    regime: pd.Series | None = None,
) -> list[dict]:
    rows: list[dict] = []
    # Evaluate every completed session. ``simulate`` prevents overlapping
    # positions per strategy/ticker, while the forward journal can therefore
    # only open a genuinely current signal.
    for position in range(20, len(score.index)):
        timestamp = int(score.index[position])
        values = score.iloc[position].replace([np.inf, -np.inf], np.nan).dropna()
        if allowed is not None:
            permit = allowed.iloc[position].reindex(values.index).fillna(False)
            values = values[permit]
        values = values[values.abs() >= 1.0]
        if values.empty:
            continue
        directions = (("long", values.nlargest(2)), ("short", values.nsmallest(2)))
        for direction, selected in directions:
            if regime is not None:
                state = float(regime.iloc[position]) if pd.notna(regime.iloc[position]) else 0.0
                if (direction == "long" and state <= 0) or (direction == "short" and state >= 0):
                    continue
            for ticker, value in selected.items():
                price = prices.at[timestamp, ticker]
                if pd.isna(price) or price <= 0:
                    continue
                rows.append({
                    "strategy": strategy,
                    "ticker": str(ticker),
                    "direction": direction,
                    "signal_time": timestamp,
                    "signal_price": float(price),
                    "score": float(value),
                    "confidence": _confidence(float(value)),
                    "timeframe_minutes": 1440,
                    "hold_minutes": HOLD_SESSIONS * 1440,
                    "stop_pct": 0.0,
                    "target_pct": 0.0,
                })
    return rows


def generate_candidates(frame: pd.DataFrame, market: str) -> list[dict]:
    close, volume = _panels(frame)
    if close.empty or len(close) < 27:
        return []
    benchmark = _benchmark(close, market)
    relative = (
        (close.pct_change(5, fill_method=None).mul(100).sub(benchmark.pct_change(5).mul(100), axis=0))
        + 2 * (close.pct_change(10, fill_method=None).mul(100).sub(benchmark.pct_change(10).mul(100), axis=0))
        + 3 * (close.pct_change(20, fill_method=None).mul(100).sub(benchmark.pct_change(20).mul(100), axis=0))
    ) / 6
    rs_score = _zscore_cross_section(relative)
    turnover = (close * volume).rolling(20, min_periods=15).mean()
    liquid = turnover.rank(axis=1, pct=True) >= 0.40
    regime = benchmark / benchmark.rolling(20, min_periods=20).mean() - 1
    momentum_raw = (
        close.pct_change(3, fill_method=None).mul(100)
        + 2 * close.pct_change(7, fill_method=None).mul(100)
        + 3 * close.pct_change(14, fill_method=None).mul(100)
    ) / 6
    momentum_score = _zscore_cross_section(momentum_raw)
    candidates: list[dict] = []
    candidates.extend(_select_extremes(rs_score, close, "daily_rs"))
    candidates.extend(_select_extremes(rs_score, close, "daily_rs_liquid", allowed=liquid))
    candidates.extend(_select_extremes(rs_score, close, "daily_rs_regime", regime=regime))
    candidates.extend(_select_extremes(momentum_score, close, "daily_momentum"))
    return candidates


def _ticker_series(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        str(ticker): group.sort_values("time_ms").drop_duplicates("time_ms").reset_index(drop=True)
        for ticker, group in frame.groupby("ticker", sort=False)
    }


def _trade_return(direction: str, entry: float, exit_price: float, cost: float) -> tuple[float, float]:
    gross = (exit_price / entry - 1) * 100
    if direction == "short":
        gross = -gross
    return gross, gross - cost


def simulate(
    candidates: list[dict],
    frame: pd.DataFrame,
    market: str,
    *,
    stop_pct: float = 0.0,
    target_pct: float = 0.0,
) -> list[dict]:
    cost = float(_config(market)["cost_pct"])
    series = _ticker_series(frame)
    trades: list[dict] = []
    occupied_until: dict[tuple[str, str], int] = {}
    for candidate in sorted(candidates, key=lambda item: item["signal_time"]):
        ticker_frame = series.get(candidate["ticker"])
        if ticker_frame is None:
            continue
        times = ticker_frame["time_ms"].to_numpy(dtype=np.int64)
        signal_pos = int(np.searchsorted(times, int(candidate["signal_time"]), side="right"))
        exit_pos = signal_pos + HOLD_SESSIONS
        if signal_pos >= len(times) or exit_pos >= len(times):
            continue
        key = (candidate["strategy"], candidate["ticker"])
        entry_time = int(times[signal_pos])
        if entry_time <= occupied_until.get(key, -1):
            continue
        entry_price = float(ticker_frame.iloc[signal_pos]["close"])
        selected_exit_pos = exit_pos
        exit_reason = f"{HOLD_SESSIONS} торговых сессий"
        if stop_pct > 0 or target_pct > 0:
            # Daily equity data contains closes only.  Thresholds are checked
            # on subsequent completed session closes, never inside a candle.
            for check_pos in range(signal_pos + 1, exit_pos + 1):
                check_price = float(ticker_frame.iloc[check_pos]["close"])
                check_gross, _ = _trade_return(
                    candidate["direction"], entry_price, check_price, 0.0,
                )
                if stop_pct > 0 and check_gross <= -stop_pct:
                    selected_exit_pos = check_pos
                    exit_reason = f"SL {stop_pct:g}% по закрытию"
                    break
                if target_pct > 0 and check_gross >= target_pct:
                    selected_exit_pos = check_pos
                    exit_reason = f"TP {target_pct:g}% по закрытию"
                    break
        exit_price = float(ticker_frame.iloc[selected_exit_pos]["close"])
        gross, net = _trade_return(candidate["direction"], entry_price, exit_price, cost)
        trade = {
            **candidate,
            "entry_time": entry_time,
            "entry_price": entry_price,
            "exit_time": int(times[selected_exit_pos]),
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "stop_pct": float(stop_pct),
            "target_pct": float(target_pct),
            "gross_return_pct": gross,
            "cost_pct": cost,
            "net_return_pct": net,
            "cash_result": STAKE_USD * net / 100,
        }
        trades.append(trade)
        occupied_until[key] = trade["exit_time"]
    return trades


def _trade_summary(trades: list[dict]) -> dict:
    values = [float(item["cash_result"]) for item in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    return {
        "trades": len(values),
        "wins": len(wins),
        "win_rate": len(wins) / len(values) * 100 if values else 0.0,
        "net_cash": sum(values),
        "avg_cash": sum(values) / len(values) if values else 0.0,
        "profit_factor": (
            sum(wins) / abs(sum(losses))
            if losses
            else (999.0 if wins else 0.0)
        ),
    }


def _optimize_close_exits(
    candidates: list[dict],
    frame: pd.DataFrame,
    market: str,
    data_start: int,
    data_end: int,
) -> dict[str, dict]:
    """Select close-based SL/TP on old data and report unseen validation.

    Every grid combination is simulated independently, including its changed
    non-overlap schedule.  Selection sees only the first 70% of each calendar
    window.  The final metrics come exclusively from the remaining 30%.
    """
    coverage = max(0, int((data_end - data_start) // DAY_MS) + 1)
    grid_runs: dict[tuple[float, float], list[dict]] = {}
    for stop_pct in STOP_GRID:
        for target_pct in TARGET_GRID:
            grid_runs[(stop_pct, target_pct)] = simulate(
                candidates,
                frame,
                market,
                stop_pct=stop_pct,
                target_pct=target_pct,
            )

    output: dict[str, dict] = {}
    for strategy in STRATEGIES:
        for days in WINDOWS_DAYS:
            key = f"{strategy}_{days}d"
            window_start = max(data_start, data_end - days * DAY_MS)
            split_time = int(
                window_start + (data_end - window_start) * OPTIMIZER_TRAIN_SHARE
            )
            choices: list[tuple[tuple[float, float, float], float, float, dict, list[dict]]] = []
            for (stop_pct, target_pct), all_trades in grid_runs.items():
                relevant = [
                    item for item in all_trades
                    if item["strategy"] == strategy and item["exit_time"] >= window_start
                ]
                train = [item for item in relevant if item["exit_time"] <= split_time]
                validation = [item for item in relevant if item["exit_time"] > split_time]
                train_metrics = _trade_summary(train)
                if train_metrics["trades"] < OPTIMIZER_MIN_TRAIN_TRADES:
                    continue
                # Net result is primary; profit factor and a smaller threshold
                # are deterministic tie-breakers. Validation is never consulted.
                rank = (
                    float(train_metrics["net_cash"]),
                    float(train_metrics["profit_factor"]),
                    -float(stop_pct + target_pct),
                )
                choices.append((rank, stop_pct, target_pct, train_metrics, validation))

            if not choices:
                output[key] = {
                    "available": False,
                    "reason": "Недостаточно сделок для честного разделения 70/30",
                }
                continue
            _, stop_pct, target_pct, train_metrics, validation = max(
                choices, key=lambda item: item[0]
            )
            validation_metrics = _trade_summary(validation)
            available = validation_metrics["trades"] >= OPTIMIZER_MIN_VALIDATION_TRADES
            output[key] = {
                "available": available,
                "reason": (
                    "Недостаточно сделок в проверочной части"
                    if not available else ""
                ),
                "stop_pct": stop_pct,
                "target_pct": target_pct,
                "train": train_metrics,
                "validation": validation_metrics,
                "train_end": split_time,
                "coverage_days": coverage,
                "method": "session_close_70_30",
            }
    return output


def _metrics(trades: list[dict], data_start: int, data_end: int) -> dict:
    coverage = max(0, int((data_end - data_start) // DAY_MS) + 1)
    output: dict[str, dict] = {}
    for strategy in STRATEGIES:
        for days in WINDOWS_DAYS:
            cutoff = data_end - days * DAY_MS
            subset = [t for t in trades if t["strategy"] == strategy and t["exit_time"] >= cutoff]
            values = [float(t["cash_result"]) for t in subset]
            wins = [v for v in values if v > 0]
            losses = [v for v in values if v < 0]
            output[f"{strategy}_{days}d"] = {
                "window_days": days,
                "coverage_days": coverage,
                "is_complete": coverage >= days,
                "trades": len(subset),
                "wins": len(wins),
                "win_rate": len(wins) / len(subset) * 100 if subset else 0.0,
                "net_cash": sum(values),
                "avg_weekly_cash": sum(values) * 7 / days,
                "profit_factor": sum(wins) / abs(sum(losses)) if losses else (999.0 if wins else 0.0),
            }
    return output


def _advance_forward(conn: sqlite3.Connection, frame: pd.DataFrame, market: str) -> dict:
    version = _config(market)["version"]
    cost = float(_config(market)["cost_pct"])
    series = _ticker_series(frame)
    rows = conn.execute(
        """SELECT * FROM short_term_forward_trades
           WHERE calculation_version=? AND status IN ('pending','active')""",
        (version,),
    ).fetchall()
    activated = closed = 0
    for row in rows:
        item = dict(row)
        ticker_frame = series.get(item["ticker"])
        if ticker_frame is None or ticker_frame.empty:
            continue
        times = ticker_frame["time_ms"].to_numpy(dtype=np.int64)
        if item["status"] == "pending":
            entry_pos = int(np.searchsorted(times, int(item["signal_time"]), side="right"))
            if entry_pos >= len(times):
                continue
            entry_time = int(times[entry_pos])
            planned_pos = entry_pos + HOLD_SESSIONS
            planned = int(times[planned_pos]) if planned_pos < len(times) else entry_time + 7 * DAY_MS
            conn.execute(
                """UPDATE short_term_forward_trades SET status='active', entry_time=?, entry_price=?,
                   planned_exit_time=?, last_evaluated_time=?, last_price=?, updated_at=datetime('now') WHERE id=?""",
                (entry_time, float(ticker_frame.iloc[entry_pos]["close"]), planned, entry_time,
                 float(ticker_frame.iloc[entry_pos]["close"]), item["id"]),
            )
            item.update(status="active", entry_time=entry_time,
                        entry_price=float(ticker_frame.iloc[entry_pos]["close"]), planned_exit_time=planned)
            activated += 1
        entry_pos = int(np.searchsorted(times, int(item["entry_time"]), side="left"))
        if entry_pos >= len(times):
            continue
        latest_pos = len(times) - 1
        evaluate_pos = min(latest_pos, entry_pos + HOLD_SESSIONS)
        current_price = float(ticker_frame.iloc[evaluate_pos]["close"])
        gross, net = _trade_return(item["direction"], float(item["entry_price"]), current_price, cost)
        if latest_pos >= entry_pos + HOLD_SESSIONS:
            conn.execute(
                """UPDATE short_term_forward_trades SET status='closed', exit_time=?, exit_price=?,
                   exit_reason='5 торговых сессий', gross_return_pct=?, cost_pct=?, net_return_pct=?,
                   cash_result=?, last_evaluated_time=?, last_price=?, current_net_return_pct=?,
                   current_cash_result=?, updated_at=datetime('now') WHERE id=?""",
                (int(times[evaluate_pos]), current_price, gross, cost, net, STAKE_USD * net / 100,
                 int(times[evaluate_pos]), current_price, net, STAKE_USD * net / 100, item["id"]),
            )
            closed += 1
        else:
            conn.execute(
                """UPDATE short_term_forward_trades SET last_evaluated_time=?, last_price=?,
                   current_net_return_pct=?, current_cash_result=?, updated_at=datetime('now') WHERE id=?""",
                (int(times[evaluate_pos]), current_price, net, STAKE_USD * net / 100, item["id"]),
            )
    return {"activated": activated, "closed": closed}


def _insert_latest(
    conn: sqlite3.Connection,
    candidates: list[dict],
    market: str,
    data_end: int,
) -> int:
    if not candidates:
        return 0
    version = _config(market)["version"]
    # Never backfill a forward signal from an older session. Historical
    # candidates belong exclusively to the backtest journal.
    selected = [item for item in candidates if int(item["signal_time"]) == data_end]
    inserted = 0
    for item in selected:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO short_term_forward_trades
               (calculation_version,strategy,ticker,direction,signal_time,signal_price,score,
                confidence,timeframe_minutes,hold_minutes,stop_pct,target_pct)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (version, item["strategy"], item["ticker"], item["direction"], item["signal_time"],
             item["signal_price"], item["score"], item["confidence"], item["timeframe_minutes"],
             item["hold_minutes"], item["stop_pct"], item["target_pct"]),
        )
        inserted += int(cursor.rowcount > 0)
    return inserted


def refresh_equity_short_term_lab(db_path: str, market: str) -> dict:
    config = _config(market)
    with _LOCKS[market]:
        conn = sqlite3.connect(db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        ensure_short_term_schema(conn)
        run_id = conn.execute(
            "INSERT INTO short_term_runs(calculation_version,status) VALUES (?, 'running')",
            (config["version"],),
        ).lastrowid
        conn.commit()
        try:
            frame = _load_prices(conn, market)
            if frame.empty or frame["ticker"].nunique() < 5:
                raise ValueError(f"Недостаточно данных {config['label']} в таблице prices")
            data_start, data_end = int(frame["time_ms"].min()), int(frame["time_ms"].max())
            candidates = generate_candidates(frame, market)
            trades = simulate(candidates, frame, market)
            metrics = _metrics(trades, data_start, data_end)
            exit_optimization = _optimize_close_exits(
                candidates, frame, market, data_start, data_end,
            )
            conn.executemany(
                """INSERT OR IGNORE INTO short_term_backtest_trades
                   (run_id,strategy,ticker,direction,signal_time,entry_time,entry_price,exit_time,
                    exit_price,exit_reason,score,confidence,gross_return_pct,cost_pct,
                    net_return_pct,cash_result) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, t["strategy"], t["ticker"], t["direction"], t["signal_time"],
                  t["entry_time"], t["entry_price"], t["exit_time"], t["exit_price"],
                  t["exit_reason"], t["score"], t["confidence"], t["gross_return_pct"],
                  t["cost_pct"], t["net_return_pct"], t["cash_result"]) for t in trades],
            )
            forward = _advance_forward(conn, frame, market)
            inserted = _insert_latest(conn, candidates, market, data_end)
            payload = {
                "strategies": metrics,
                "exit_optimization": exit_optimization,
                "coverage_days": int((data_end - data_start) // DAY_MS) + 1,
                "forward": {**forward, "pending": inserted},
            }
            conn.execute(
                """UPDATE short_term_runs SET status='completed',completed_at=datetime('now'),
                   data_start=?,data_end=?,candle_count=?,metrics_json=? WHERE id=?""",
                (data_start, data_end, len(frame), json.dumps(payload), run_id),
            )
            conn.commit()
            return {"status": "completed", "run_id": run_id, **payload}
        except Exception as exc:
            conn.execute(
                "UPDATE short_term_runs SET status='failed',completed_at=datetime('now'),error=? WHERE id=?",
                (str(exc)[:500], run_id),
            )
            conn.commit()
            raise
        finally:
            conn.close()


def _format_date(value: int | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value / 1000, UTC).strftime("%d.%m.%Y")


def get_equity_short_term_report(db_path: str, market: str) -> dict:
    config = _config(market)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_short_term_schema(conn)
    latest = conn.execute(
        "SELECT * FROM short_term_runs WHERE calculation_version=? ORDER BY id DESC LIMIT 1",
        (config["version"],),
    ).fetchone()
    completed = conn.execute(
        """SELECT * FROM short_term_runs WHERE calculation_version=? AND status='completed'
           ORDER BY id DESC LIMIT 1""",
        (config["version"],),
    ).fetchone()
    open_rows = conn.execute(
        """SELECT * FROM short_term_forward_trades WHERE calculation_version=?
           AND status IN ('pending','active') ORDER BY strategy,ABS(score) DESC""",
        (config["version"],),
    ).fetchall()
    closed_rows = conn.execute(
        """SELECT * FROM short_term_forward_trades WHERE calculation_version=? AND status='closed'
           ORDER BY exit_time DESC,id DESC LIMIT 80""",
        (config["version"],),
    ).fetchall()
    latest_price = conn.execute("SELECT MAX(date) FROM prices WHERE market=?", (market,)).fetchone()[0]
    conn.close()
    payload = json.loads(completed["metrics_json"] or "{}") if completed else {}
    metrics = payload.get("strategies", {})
    has_exit_optimization = "exit_optimization" in payload
    exit_optimization = payload.get("exit_optimization", {})
    cards = []
    for strategy, settings in STRATEGIES.items():
        windows = []
        for days in WINDOWS_DAYS:
            metric_key = f"{strategy}_{days}d"
            windows.append({
                "days": days,
                **metrics.get(metric_key, {}),
                "exit_optimization": exit_optimization.get(metric_key, {}),
            })
        cards.append({"key": strategy, **settings, "windows": windows})

    def trade(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["strategy_name"] = STRATEGIES.get(item["strategy"], {}).get("name", item["strategy"])
        for key in ("signal_time", "entry_time", "planned_exit_time", "exit_time"):
            item[f"{key}_label"] = _format_date(item.get(key))
        return item

    latest_dict = dict(latest) if latest else None
    if latest_dict:
        try:
            started = datetime.fromisoformat(str(latest_dict["started_at"])).replace(tzinfo=UTC)
            latest_dict["is_stale"] = latest_dict["status"] == "running" and (datetime.now(UTC) - started).total_seconds() > 1800
        except (TypeError, ValueError):
            latest_dict["is_stale"] = False
    completed_end = int(completed["data_end"] or 0) if completed else 0
    parsed_price_end = pd.to_datetime(latest_price, utc=True, errors="coerce")
    price_end = (
        int(parsed_price_end.normalize().timestamp() * 1000)
        if pd.notna(parsed_price_end)
        else 0
    )
    return {
        "market": market,
        "market_label": config["label"],
        "source": config["source"],
        "version": config["version"],
        "latest": latest_dict,
        "completed": dict(completed) if completed else None,
        "is_ready": completed is not None,
        "needs_refresh": completed is None or price_end > completed_end,
        # Completed reports created before the SL/TP optimizer was introduced
        # need one migration refresh. Once the payload contains the field,
        # unavailable validation results are still considered calculated.
        "needs_optimizer_refresh": completed is not None and not has_exit_optimization,
        "data_label": _format_date(completed_end),
        "strategies": cards,
        "open": [trade(row) for row in open_rows],
        "closed": [trade(row) for row in closed_rows],
        "settings": {
            "stake": STAKE_USD,
            "cost_pct": config["cost_pct"],
            "hold_sessions": HOLD_SESSIONS,
            "exit_model": "session_close",
            "optimizer_train_share": OPTIMIZER_TRAIN_SHARE,
        },
    }
