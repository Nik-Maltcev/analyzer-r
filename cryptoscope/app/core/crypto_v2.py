"""Independent multi-day Momentum strategy for the admin Crypto V2 tab."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from app.db.schema import (
    CREATE_CRYPTO_V2_CANDIDATES,
    CREATE_CRYPTO_V2_DAILY,
    CREATE_CRYPTO_V2_INDICES,
    CREATE_CRYPTO_V2_META,
    CREATE_CRYPTO_V2_TRADES,
)

STRATEGY_VERSION = "crypto-v2-regime-hysteresis-5pos-500-v3"
MODEL_CAPITAL = 500.0
MAX_POSITIONS = 5
MAX_POSITION_WEIGHT = 0.50
MAX_HOLDING_SESSIONS = 5
MISSING_CONFIRMATIONS_TO_EXIT = 2

CONFIDENCE_LABELS = {
    0: "low",
    1: "medium",
    2: "high",
}


async def ensure_crypto_v2_schema(conn) -> None:
    await conn.execute(CREATE_CRYPTO_V2_META)
    await conn.execute(CREATE_CRYPTO_V2_DAILY)
    await conn.execute(CREATE_CRYPTO_V2_CANDIDATES)
    await conn.execute(CREATE_CRYPTO_V2_TRADES)
    for statement in CREATE_CRYPTO_V2_INDICES:
        await conn.execute(statement)
    await conn.commit()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _price_display(value: Any) -> str:
    number = _safe_float(value, float("nan"))
    if not math.isfinite(number):
        return "-"
    if number >= 100:
        return f"${number:,.2f}"
    if number >= 1:
        return f"${number:,.4f}".rstrip("0").rstrip(".")
    return f"${number:.8f}".rstrip("0").rstrip(".")


def _money_display(value: Any) -> str:
    number = _safe_float(value)
    if number > 0:
        return f"+${number:.2f}"
    if number < 0:
        return f"-${abs(number):.2f}"
    return "$0.00"


def _pct_display(value: Any) -> str:
    return f"{_safe_float(value):+.2f}%"


def _confidence_rank(
    score: pd.DataFrame,
    p3: pd.DataFrame,
    p7: pd.DataFrame,
    p14: pd.DataFrame,
    volatility: pd.DataFrame,
) -> pd.DataFrame:
    confirmations = (
        (p3 > 0).astype(int)
        + (p7 > 0).astype(int)
        + (p14 > 0).astype(int)
    )
    rank = pd.DataFrame(0, index=score.index, columns=score.columns)
    rank = rank.mask((confirmations >= 2) & (score >= 5), 1)
    rank = rank.mask((confirmations == 3) & (score >= 10), 2)
    return rank.where(volatility < 8, (rank - 1).clip(lower=0))


def build_crypto_v2_features(
    wide: pd.DataFrame,
) -> dict[str, pd.DataFrame | pd.Series]:
    """Build point-in-time features without reading future observations."""
    frame = wide.copy().sort_index().astype(float)
    frame.index = pd.to_datetime(frame.index)
    p3 = (frame / frame.shift(3) - 1) * 100
    p7 = (frame / frame.shift(7) - 1) * 100
    p14 = (frame / frame.shift(14) - 1) * 100
    score = (p3 + 2 * p7 + 3 * p14) / 6
    log_returns = np.log(frame / frame.shift(1))
    volatility = log_returns.rolling(7, min_periods=4).std(ddof=0) * 100
    confidence_rank = _confidence_rank(
        score,
        p3,
        p7,
        p14,
        volatility,
    )
    eligible = (score > 3) & (confidence_rank >= 1)
    confirmed = eligible & eligible.shift(1, fill_value=False)

    btc_ticker = next(
        (
            str(ticker)
            for ticker in frame.columns
            if str(ticker).upper().split("/", 1)[0] == "BTC"
        ),
        None,
    )
    if btc_ticker:
        btc_close = frame[btc_ticker]
        btc_sma50 = btc_close.rolling(50, min_periods=50).mean()
        btc_above = btc_close > btc_sma50
        btc_distance = (btc_close / btc_sma50 - 1) * 100
        btc_ready = btc_sma50.notna()
    else:
        btc_above = pd.Series(False, index=frame.index)
        btc_distance = pd.Series(np.nan, index=frame.index)
        btc_ready = pd.Series(False, index=frame.index)

    sma20 = frame.rolling(20, min_periods=20).mean()
    breadth_available = frame.notna() & sma20.notna()
    breadth_positive = ((frame > sma20) & breadth_available).sum(axis=1)
    breadth_total = breadth_available.sum(axis=1)
    breadth_pct = breadth_positive / breadth_total.replace(0, np.nan) * 100
    breadth_above = breadth_pct >= 50
    breadth_ready = breadth_total >= 5

    market_ready = btc_ready & breadth_ready
    passed = btc_above.astype(int) + breadth_above.astype(int)
    regime = pd.Series("unavailable", index=frame.index, dtype=object)
    regime = regime.mask(market_ready & (passed == 2), "green")
    regime = regime.mask(market_ready & (passed == 1), "yellow")
    regime = regime.mask(market_ready & (passed == 0), "red")
    exposure = regime.map({
        "green": 1.0,
        "yellow": 0.3,
        "red": 0.0,
        "unavailable": 0.0,
    }).astype(float)

    return {
        "prices": frame,
        "score": score,
        "volatility": volatility,
        "confidence_rank": confidence_rank,
        "eligible": eligible,
        "confirmed": confirmed,
        "btc_above": btc_above,
        "btc_distance": btc_distance,
        "breadth_positive": breadth_positive,
        "breadth_total": breadth_total,
        "breadth_pct": breadth_pct,
        "regime": regime,
        "exposure": exposure,
    }


def _capped_inverse_volatility_weights(
    volatility: list[float],
    cap: float = MAX_POSITION_WEIGHT,
) -> list[float]:
    if not volatility:
        return []
    inverse = np.array(
        [1 / max(_safe_float(value, 0.01), 0.01) for value in volatility],
        dtype=float,
    )
    raw = inverse / inverse.sum()
    weights = np.zeros(len(raw), dtype=float)
    remaining = 1.0
    available = set(range(len(raw)))
    while available and remaining > 1e-9:
        denominator = sum(raw[index] for index in available)
        if denominator <= 0:
            break
        capped_any = False
        for index in list(available):
            proposed = remaining * raw[index] / denominator
            if proposed >= cap:
                weights[index] = cap
                remaining -= cap
                available.remove(index)
                capped_any = True
        if not capped_any:
            for index in available:
                weights[index] = remaining * raw[index] / denominator
            remaining = 0.0
    return weights.tolist()


def simulate_crypto_v2(
    wide: pd.DataFrame,
    forward_started_on: str,
) -> dict[str, list[dict[str, Any]]]:
    """Simulate V2 chronologically using only information known each day."""
    features = build_crypto_v2_features(wide)
    prices = features["prices"]
    score = features["score"]
    volatility = features["volatility"]
    confidence_rank = features["confidence_rank"]
    eligible = features["eligible"]
    confirmed = features["confirmed"]
    regime = features["regime"]
    exposure = features["exposure"]

    active: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []

    for raw_date in prices.index:
        data_date = raw_date.strftime("%Y-%m-%d")
        day_regime = str(regime.loc[raw_date])
        day_exposure = _safe_float(exposure.loc[raw_date])
        exited_today: set[str] = set()

        if data_date == forward_started_on:
            for ticker, trade in list(active.items()):
                if trade["mode"] != "backtest":
                    continue
                exit_price = float(trade.get("last_price") or trade["entry_price"])
                return_pct = (
                    (exit_price / trade["entry_price"] - 1) * 100
                    if trade["entry_price"] > 0
                    else 0.0
                )
                trade.update({
                    "exit_date": trade["last_evaluated_date"],
                    "exit_price": exit_price,
                    "exit_reason": "backtest_boundary",
                    "return_pct": return_pct,
                    "cash_result": trade["allocation"] * return_pct / 100,
                    "status": "closed",
                })
                active.pop(ticker)

        for ticker, trade in list(active.items()):
            current_price = prices.at[raw_date, ticker]
            if pd.isna(current_price):
                continue
            trade["held_sessions"] += 1
            trade["last_evaluated_date"] = data_date
            trade["last_price"] = float(current_price)
            current_score = score.at[raw_date, ticker]
            is_eligible = bool(eligible.at[raw_date, ticker])
            trade["missing_days"] = (
                0 if is_eligible else int(trade["missing_days"]) + 1
            )

            exit_reason = None
            if pd.notna(current_score) and float(current_score) < -3:
                exit_reason = "momentum_reversed"
            elif trade["missing_days"] >= MISSING_CONFIRMATIONS_TO_EXIT:
                exit_reason = "signal_missing_2d"
            elif trade["held_sessions"] >= MAX_HOLDING_SESSIONS:
                exit_reason = "max_horizon_5d"

            if exit_reason:
                exit_price = float(current_price)
                return_pct = (
                    (exit_price / trade["entry_price"] - 1) * 100
                    if trade["entry_price"] > 0
                    else 0.0
                )
                trade.update({
                    "exit_date": data_date,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "return_pct": return_pct,
                    "cash_result": trade["allocation"] * return_pct / 100,
                    "status": "closed",
                })
                active.pop(ticker)
                exited_today.add(ticker)

        day_candidates: list[dict[str, Any]] = []
        for ticker in prices.columns:
            if not bool(eligible.at[raw_date, ticker]):
                continue
            current_score = score.at[raw_date, ticker]
            current_volatility = volatility.at[raw_date, ticker]
            current_rank = int(confidence_rank.at[raw_date, ticker])
            if pd.isna(current_score) or pd.isna(current_volatility):
                continue
            day_candidates.append({
                "ticker": str(ticker),
                "confidence": CONFIDENCE_LABELS.get(current_rank, "low"),
                "confidence_rank": current_rank,
                "momentum_score": float(current_score),
                "volatility_pct": float(current_volatility),
                "confirmed": bool(confirmed.at[raw_date, ticker]),
                "selected": False,
                "rank": None,
                "regime": day_regime,
            })

        day_candidates.sort(
            key=lambda item: (
                -item["confidence_rank"],
                -item["momentum_score"],
                item["volatility_pct"],
                item["ticker"],
            )
        )
        for rank, item in enumerate(day_candidates, start=1):
            item["rank"] = rank

        selected: list[dict[str, Any]] = []
        if day_exposure > 0 and len(active) < MAX_POSITIONS:
            selected = [
                item
                for item in day_candidates
                if item["confirmed"]
                and item["ticker"] not in active
                and item["ticker"] not in exited_today
            ][: MAX_POSITIONS - len(active)]

        current_allocated = sum(
            float(trade["allocation"]) for trade in active.values()
        )
        target_capital = MODEL_CAPITAL * day_exposure
        available_capital = max(0.0, target_capital - current_allocated)
        if selected and available_capital > 0:
            relative_cap = min(
                1.0,
                MODEL_CAPITAL * MAX_POSITION_WEIGHT / available_capital,
            )
            weights = _capped_inverse_volatility_weights(
                [item["volatility_pct"] for item in selected],
                cap=relative_cap,
            )
            for item, weight in zip(selected, weights, strict=True):
                allocation = available_capital * weight
                if allocation <= 0:
                    continue
                ticker = item["ticker"]
                entry_price = prices.at[raw_date, ticker]
                if pd.isna(entry_price) or float(entry_price) <= 0:
                    continue
                trade = {
                    "strategy_version": STRATEGY_VERSION,
                    "mode": (
                        "forward"
                        if data_date >= forward_started_on
                        else "backtest"
                    ),
                    "ticker": ticker,
                    "confidence": item["confidence"],
                    "entry_date": data_date,
                    "entry_price": float(entry_price),
                    "entry_score": item["momentum_score"],
                    "entry_regime": day_regime,
                    "exposure_factor": day_exposure,
                    "allocation": allocation,
                    "weight": weight,
                    "volatility_pct": item["volatility_pct"],
                    "exit_date": None,
                    "exit_price": None,
                    "exit_reason": None,
                    "return_pct": None,
                    "cash_result": None,
                    "status": "active",
                    "last_evaluated_date": data_date,
                    "last_price": float(entry_price),
                    "missing_days": 0,
                    "held_sessions": 1,
                }
                trades.append(trade)
                active[ticker] = trade
                item["selected"] = True

        candidates.extend(
            {"data_date": data_date, **item}
            for item in day_candidates
        )
        daily.append({
            "data_date": data_date,
            "btc_above_sma50": (
                None
                if pd.isna(features["btc_distance"].loc[raw_date])
                else int(bool(features["btc_above"].loc[raw_date]))
            ),
            "btc_distance_pct": (
                None
                if pd.isna(features["btc_distance"].loc[raw_date])
                else float(features["btc_distance"].loc[raw_date])
            ),
            "breadth_positive": int(
                features["breadth_positive"].loc[raw_date]
            ),
            "breadth_total": int(features["breadth_total"].loc[raw_date]),
            "breadth_pct": (
                None
                if pd.isna(features["breadth_pct"].loc[raw_date])
                else float(features["breadth_pct"].loc[raw_date])
            ),
            "regime": day_regime,
            "exposure_factor": day_exposure,
            "candidate_count": len(day_candidates),
            "confirmed_count": sum(
                bool(item["confirmed"]) for item in day_candidates
            ),
            "selected_count": sum(
                bool(item["selected"]) for item in day_candidates
            ),
        })

    return {
        "daily": daily,
        "candidates": candidates,
        "trades": trades,
    }


async def _get_meta(conn, key: str) -> str | None:
    cursor = await conn.execute(
        """
        SELECT value
        FROM crypto_v2_meta
        WHERE strategy_version = ? AND key = ?
        """,
        (STRATEGY_VERSION, key),
    )
    row = await cursor.fetchone()
    return str(row["value"]) if row else None


async def _set_meta(conn, key: str, value: str) -> None:
    await conn.execute(
        """
        INSERT INTO crypto_v2_meta (
            strategy_version, key, value, updated_at
        )
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(strategy_version, key) DO UPDATE SET
            value = excluded.value,
            updated_at = datetime('now')
        """,
        (STRATEGY_VERSION, key, value),
    )


async def sync_crypto_v2_journal(
    db_path: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Freeze V2 history and advance its independent forward journal."""
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await ensure_crypto_v2_schema(conn)
        cursor = await conn.execute(
            """
            SELECT MAX(date) AS data_date
            FROM prices
            WHERE market = 'crypto'
            """
        )
        row = await cursor.fetchone()
        data_date = str(row["data_date"] or "")[:10]
        if not data_date:
            return {"status": "no_crypto_prices"}

        last_synced = await _get_meta(conn, "last_synced_date")
        if not force and last_synced == data_date:
            return {
                "status": "already_synced",
                "data_date": data_date,
            }

        forward_started_on = await _get_meta(conn, "forward_started_on")
        if not forward_started_on:
            forward_started_on = data_date
            await _set_meta(conn, "forward_started_on", forward_started_on)

        cursor = await conn.execute(
            """
            SELECT ticker, date, close
            FROM prices
            WHERE market = 'crypto'
              AND close IS NOT NULL
              AND close > 0
            ORDER BY date, ticker
            """
        )
        price_rows = [dict(item) for item in await cursor.fetchall()]
        if not price_rows:
            return {"status": "no_crypto_prices"}
        price_frame = pd.DataFrame(price_rows)
        wide = price_frame.pivot_table(
            index="date",
            columns="ticker",
            values="close",
            aggfunc="last",
        ).sort_index()
        simulation = simulate_crypto_v2(wide, forward_started_on)

        for item in simulation["daily"]:
            await conn.execute(
                """
                INSERT OR IGNORE INTO crypto_v2_daily (
                    strategy_version, data_date, btc_above_sma50,
                    btc_distance_pct, breadth_positive, breadth_total,
                    breadth_pct, regime, exposure_factor, candidate_count,
                    confirmed_count, selected_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    STRATEGY_VERSION,
                    item["data_date"],
                    item["btc_above_sma50"],
                    item["btc_distance_pct"],
                    item["breadth_positive"],
                    item["breadth_total"],
                    item["breadth_pct"],
                    item["regime"],
                    item["exposure_factor"],
                    item["candidate_count"],
                    item["confirmed_count"],
                    item["selected_count"],
                ),
            )

        for item in simulation["candidates"]:
            await conn.execute(
                """
                INSERT OR IGNORE INTO crypto_v2_candidates (
                    strategy_version, data_date, ticker, confidence,
                    momentum_score, volatility_pct, confirmed, selected,
                    rank, regime
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    STRATEGY_VERSION,
                    item["data_date"],
                    item["ticker"],
                    item["confidence"],
                    item["momentum_score"],
                    item["volatility_pct"],
                    int(item["confirmed"]),
                    int(item["selected"]),
                    item["rank"],
                    item["regime"],
                ),
            )

        cursor = await conn.execute(
            """
            SELECT *
            FROM crypto_v2_trades
            WHERE strategy_version = ?
            """,
            (STRATEGY_VERSION,),
        )
        existing = {
            (str(item["ticker"]), str(item["entry_date"])): dict(item)
            for item in await cursor.fetchall()
        }
        inserted = 0
        closed = 0
        for item in simulation["trades"]:
            key = (item["ticker"], item["entry_date"])
            stored = existing.get(key)
            if stored is None:
                await conn.execute(
                    """
                    INSERT INTO crypto_v2_trades (
                        strategy_version, mode, ticker, confidence,
                        entry_date, entry_price, entry_score, entry_regime,
                        exposure_factor, allocation, weight, volatility_pct,
                        exit_date, exit_price, exit_reason, return_pct,
                        cash_result, status, last_evaluated_date, missing_days
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?
                    )
                    """,
                    (
                        STRATEGY_VERSION,
                        item["mode"],
                        item["ticker"],
                        item["confidence"],
                        item["entry_date"],
                        item["entry_price"],
                        item["entry_score"],
                        item["entry_regime"],
                        item["exposure_factor"],
                        item["allocation"],
                        item["weight"],
                        item["volatility_pct"],
                        item["exit_date"],
                        item["exit_price"],
                        item["exit_reason"],
                        item["return_pct"],
                        item["cash_result"],
                        item["status"],
                        item["last_evaluated_date"],
                        item["missing_days"],
                    ),
                )
                inserted += 1
                closed += int(item["status"] == "closed")
            elif stored["status"] == "active":
                await conn.execute(
                    """
                    UPDATE crypto_v2_trades
                    SET exit_date = ?, exit_price = ?, exit_reason = ?,
                        return_pct = ?, cash_result = ?, status = ?,
                        last_evaluated_date = ?, missing_days = ?,
                        updated_at = datetime('now')
                    WHERE id = ? AND status = 'active'
                    """,
                    (
                        item["exit_date"],
                        item["exit_price"],
                        item["exit_reason"],
                        item["return_pct"],
                        item["cash_result"],
                        item["status"],
                        item["last_evaluated_date"],
                        item["missing_days"],
                        stored["id"],
                    ),
                )
                closed += int(item["status"] == "closed")

        await _set_meta(conn, "last_synced_date", data_date)
        await conn.commit()
        return {
            "status": "synced",
            "data_date": data_date,
            "forward_started_on": forward_started_on,
            "inserted": inserted,
            "closed": closed,
        }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [item for item in rows if item["status"] == "closed"]
    active = [item for item in rows if item["status"] == "active"]
    cash = sum(_safe_float(item.get("cash_result")) for item in closed)
    invested = sum(_safe_float(item.get("allocation")) for item in closed)
    wins = sum(_safe_float(item.get("cash_result")) > 0 for item in closed)
    losses = sum(_safe_float(item.get("cash_result")) < 0 for item in closed)
    return {
        "closed": len(closed),
        "active": len(active),
        "wins": wins,
        "losses": losses,
        "flat": len(closed) - wins - losses,
        "win_rate": wins / len(closed) * 100 if closed else 0.0,
        "win_rate_display": (
            f"{wins / len(closed) * 100:.1f}%" if closed else "-"
        ),
        "cash_result": cash,
        "cash_result_display": _money_display(cash),
        "return_pct": cash / invested * 100 if invested else 0.0,
        "return_display": (
            f"{cash / invested * 100:+.2f}%" if invested else "-"
        ),
    }


async def fetch_crypto_v2_report(conn) -> dict[str, Any]:
    await ensure_crypto_v2_schema(conn)
    cursor = await conn.execute(
        """
        SELECT *
        FROM crypto_v2_daily
        WHERE strategy_version = ?
        ORDER BY data_date DESC
        LIMIT 1
        """,
        (STRATEGY_VERSION,),
    )
    daily_row = await cursor.fetchone()
    latest = dict(daily_row) if daily_row else None

    cursor = await conn.execute(
        """
        SELECT *
        FROM crypto_v2_trades
        WHERE strategy_version = ?
        ORDER BY entry_date DESC, id DESC
        """,
        (STRATEGY_VERSION,),
    )
    trades = [dict(item) for item in await cursor.fetchall()]

    latest_prices: dict[str, float] = {}
    if trades:
        cursor = await conn.execute(
            """
            SELECT ticker, close
            FROM prices
            WHERE market = 'crypto'
              AND date = (
                  SELECT MAX(date)
                  FROM prices
                  WHERE market = 'crypto'
              )
            """
        )
        latest_prices = {
            str(item["ticker"]): float(item["close"])
            for item in await cursor.fetchall()
        }

    active = [item for item in trades if item["status"] == "active"]
    active_tickers = {str(item["ticker"]) for item in active}
    for item in trades:
        item["symbol"] = str(item["ticker"]).split("/", 1)[0]
        item["entry_price_display"] = _price_display(item["entry_price"])
        item["exit_price_display"] = _price_display(item.get("exit_price"))
        item["allocation_display"] = f"${_safe_float(item['allocation']):.2f}"
        item["return_display"] = (
            _pct_display(item["return_pct"])
            if item.get("return_pct") is not None
            else "-"
        )
        item["cash_result_display"] = (
            _money_display(item["cash_result"])
            if item.get("cash_result") is not None
            else "-"
        )
        item["confidence_label"] = {
            "high": "Высокая",
            "medium": "Средняя",
            "low": "Низкая",
        }.get(str(item["confidence"]), str(item["confidence"]))
        item["exit_reason_label"] = {
            "momentum_reversed": "Momentum развернулся",
            "signal_missing_2d": "LONG отсутствовал 2 дня",
            "max_horizon_5d": "Горизонт 5 сессий",
            "backtest_boundary": "Граница запуска forward-теста",
        }.get(str(item.get("exit_reason") or ""), "-")

    for item in active:
        mark = latest_prices.get(str(item["ticker"]), item["entry_price"])
        return_pct = (
            (mark / float(item["entry_price"]) - 1) * 100
            if float(item["entry_price"]) > 0
            else 0.0
        )
        cash_result = float(item["allocation"]) * return_pct / 100
        item["current_price"] = mark
        item["current_price_display"] = _price_display(mark)
        item["current_return_pct"] = return_pct
        item["current_return_display"] = _pct_display(return_pct)
        item["current_cash_result"] = cash_result
        item["current_cash_result_display"] = _money_display(cash_result)

    candidates: list[dict[str, Any]] = []
    if latest:
        cursor = await conn.execute(
            """
            SELECT *
            FROM crypto_v2_candidates
            WHERE strategy_version = ? AND data_date = ?
            ORDER BY rank, ticker
            """,
            (STRATEGY_VERSION, latest["data_date"]),
        )
        candidates = [dict(item) for item in await cursor.fetchall()]
        for item in candidates:
            item["symbol"] = str(item["ticker"]).split("/", 1)[0]
            item["confidence_label"] = {
                "high": "Высокая",
                "medium": "Средняя",
                "low": "Низкая",
            }.get(str(item["confidence"]), str(item["confidence"]))
            item["score_display"] = f"{float(item['momentum_score']):+.2f}"
            item["volatility_display"] = (
                f"{float(item['volatility_pct']):.2f}%"
            )
            item["is_active"] = str(item["ticker"]) in active_tickers
            if item["is_active"]:
                item["status_label"] = "В позиции"
                item["status_class"] = "active"
            elif not item["confirmed"]:
                item["status_label"] = "Ждём второй день"
                item["status_class"] = "waiting"
            elif latest["regime"] in {"red", "unavailable"}:
                item["status_label"] = "Вход закрыт фильтром"
                item["status_class"] = "blocked"
            elif len(active) >= MAX_POSITIONS:
                item["status_label"] = "Портфель заполнен"
                item["status_class"] = "waiting"
            else:
                item["status_label"] = "Кандидат на вход"
                item["status_class"] = "ready"

    if latest:
        latest["btc_distance_display"] = (
            _pct_display(latest["btc_distance_pct"])
            if latest.get("btc_distance_pct") is not None
            else "-"
        )
        latest["breadth_display"] = (
            f"{float(latest['breadth_pct']):.1f}%"
            if latest.get("breadth_pct") is not None
            else "-"
        )
        latest["exposure_display"] = (
            f"{float(latest['exposure_factor']) * 100:.0f}%"
        )
        latest["regime_label"] = {
            "green": "Зелёный",
            "yellow": "Жёлтый",
            "red": "Красный",
            "unavailable": "Нет данных",
        }.get(str(latest["regime"]), str(latest["regime"]))

    backtest_rows = [item for item in trades if item["mode"] == "backtest"]
    forward_rows = [item for item in trades if item["mode"] == "forward"]
    active_cash = sum(
        _safe_float(item.get("current_cash_result")) for item in active
    )
    active_invested = sum(_safe_float(item.get("allocation")) for item in active)
    forward_summary = _summary(forward_rows)
    return {
        "strategy_version": STRATEGY_VERSION,
        "max_positions": MAX_POSITIONS,
        "model_capital": MODEL_CAPITAL,
        "model_capital_display": f"${MODEL_CAPITAL:.0f}",
        "latest": latest,
        "candidates": candidates,
        "active": active,
        "history": [item for item in trades if item["status"] == "closed"][:50],
        "backtest": _summary(backtest_rows),
        "forward": forward_summary,
        "active_cash": active_cash,
        "active_cash_display": _money_display(active_cash),
        "active_return_display": (
            f"{active_cash / active_invested * 100:+.2f}%"
            if active_invested
            else "-"
        ),
        "forward_total_cash": forward_summary["cash_result"] + active_cash,
        "forward_total_cash_display": _money_display(
            forward_summary["cash_result"] + active_cash
        ),
        "forward_started_on": await _get_meta(conn, "forward_started_on"),
    }


def apply_crypto_v2_live_prices(
    report: dict[str, Any],
    live_prices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply live marks to active V2 trades without mutating the journal."""
    live_prices = live_prices or {}
    active_cash = 0.0
    active_invested = 0.0
    for item in report.get("active") or []:
        mark = _safe_float(
            live_prices.get(str(item["ticker"])),
            _safe_float(item.get("current_price"), item["entry_price"]),
        )
        entry = float(item["entry_price"])
        allocation = float(item["allocation"])
        return_pct = (mark / entry - 1) * 100 if entry > 0 else 0.0
        cash_result = allocation * return_pct / 100
        item["current_price"] = mark
        item["current_price_display"] = _price_display(mark)
        item["current_return_pct"] = return_pct
        item["current_return_display"] = _pct_display(return_pct)
        item["current_cash_result"] = cash_result
        item["current_cash_result_display"] = _money_display(cash_result)
        active_cash += cash_result
        active_invested += allocation
    report["active_cash"] = active_cash
    report["active_cash_display"] = _money_display(active_cash)
    report["active_return_display"] = (
        f"{active_cash / active_invested * 100:+.2f}%"
        if active_invested
        else "-"
    )
    total = _safe_float(report["forward"].get("cash_result")) + active_cash
    report["forward_total_cash"] = total
    report["forward_total_cash_display"] = _money_display(total)
    return report
