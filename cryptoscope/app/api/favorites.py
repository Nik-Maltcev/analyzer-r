"""Favorites API endpoints."""

import math

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import AuthUser, require_current_or_legacy_user
from app.core.calculator import (
    CALCULATION_VERSION,
    calc_pair_performance,
    calc_single_performance,
    frozen_execution_settings,
    infer_position_kind,
)
from app.core.signals import elapsed_holding_days, estimate_signal_timing
from app.data.mexc_market import refresh_crypto_live_prices
from app.data.moex import get_ru_live_snapshot, refresh_ru_live_prices
from app.db.database import (
    close_favorite,
    delete_favorite,
    fetch_favorites,
    fetch_favorites_history,
    get_connection,
    toggle_favorite,
)
from app.product import get_product_profile

router = APIRouter(prefix="/favorites", tags=["favorites"])


def _query_float(value, default=0.0):
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _query_int(value, default=None):
    f = _query_float(value, None)
    return int(f) if f is not None else default


def _get_current_price(ticker: str, db_prices: dict, market: str) -> float:
    """Get a live market price when available, then fall back to the database."""
    if market == "crypto":
        try:
            from app.data.mexc_market import get_live_price
            live = get_live_price(ticker)
            if live is not None and live > 0:
                return float(live)
        except ImportError:
            pass
    elif market == "ru":
        live_prices, _ = get_ru_live_snapshot()
        live = live_prices.get(ticker)
        if live is not None and live > 0:
            return float(live)
    return float(db_prices.get((market, ticker), 0) or 0)


@router.get("/live-status")
async def live_prices_status():
    """Check the MEXC public-price poller status."""
    try:
        from app.data.mexc_market import get_all_live_tickers, get_uptime, is_connected, live_prices
        return {
            "connected": is_connected(),
            "uptime_seconds": round(get_uptime(), 1),
            "symbols_tracked": len(live_prices),
            "tickers_tracked": len(get_all_live_tickers()),
        }
    except ImportError:
        return {"connected": False, "error": "websockets module not installed"}


@router.get("")
async def get_favorites(
    capital: float = Query(1000.0, ge=10),
    leverage: float = Query(1.0, ge=1, le=20),
    taker_fee: float = Query(0.02, ge=0, le=1),
    funding_rate: float = Query(0.01, ge=0, le=1),
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Get active favorites with live P&L."""
    async with get_connection() as conn:
        favs = await fetch_favorites(conn, user.id)

    if favs.empty:
        return {"favorites": [], "total": 0}

    # Fetch latest price ONLY for tickers in favorites (not all 159k rows)
    ticker_keys = set()
    for _, favorite in favs.iterrows():
        market = favorite.get("market") or "crypto"
        ticker_keys.add((market, favorite["ticker_a"]))
        if favorite.get("ticker_b"):
            ticker_keys.add((market, favorite["ticker_b"]))

    latest_prices = {}
    pair_risks = {}
    async with get_connection() as conn:
        for market, ticker in ticker_keys:
            cursor = await conn.execute(
                """
                SELECT close FROM prices
                WHERE ticker = ? AND market = ?
                ORDER BY date DESC LIMIT 1
                """,
                (ticker, market)
            )
            row = await cursor.fetchone()
            if row:
                latest_prices[(market, ticker)] = float(row[0])
        cursor = await conn.execute("SELECT * FROM pairs")
        for pair_row in await cursor.fetchall():
            pair_data = dict(pair_row)
            pair_risks[(
                pair_data.get("market"),
                pair_data.get("ticker_a"),
                pair_data.get("ticker_b"),
            )] = pair_data

    active_positions = []
    for _, row in favs.iterrows():
        market = row.get("market") or "crypto"
        position_kind = infer_position_kind(
            row.get("position_kind"),
            row.get("ticker_b"),
        )
        is_single = position_kind == "single"
        entry_a = row.get("price_a_entry")
        entry_b = row.get("price_b_entry")

        # Backfill missing entry prices from latest_prices
        if not entry_a or entry_a == 0:
            entry_a = float(latest_prices.get((market, row["ticker_a"]), 0) or 0)
        if not is_single and (not entry_b or entry_b == 0):
            entry_b = float(latest_prices.get((market, row["ticker_b"]), 0) or 0)

        price_a_now = _get_current_price(row["ticker_a"], latest_prices, market)
        price_b_now = (
            0
            if is_single
            else _get_current_price(row["ticker_b"], latest_prices, market)
        )

        # Fall back to DB prices if live price is 0 and DB has one
        if (price_a_now or 0) == 0:
            price_a_now = float(latest_prices.get((market, row["ticker_a"]), 0) or 0)
        if not is_single and (price_b_now or 0) == 0:
            price_b_now = float(latest_prices.get((market, row["ticker_b"]), 0) or 0)

        sig_type = row.get("signal_type", "wait")
        hl = _query_int(row.get("halflife"), None)
        entry_time = row.get("entry_time")
        timing = estimate_signal_timing(entry_time, hl)
        days_held = timing["signal_days_elapsed"]
        hold_days_exact = elapsed_holding_days(entry_time)
        hl_remaining = timing["signal_days_remaining"]
        is_expired = timing["signal_is_expired"]
        execution = frozen_execution_settings(
            row,
            fallback_capital=capital,
            fallback_leverage=leverage,
            fallback_taker_fee_pct=taker_fee,
            fallback_funding_rate_pct=(
                funding_rate if market == "crypto" else 0
            ),
        )
        performance = (
            calc_single_performance(
                sig_type,
                entry_a,
                price_a_now,
                capital=execution["capital"],
                leverage=execution["leverage"],
                taker_fee_pct=execution["taker_fee_pct"],
                funding_rate_8h_pct=(
                    execution["funding_rate_pct"]
                    if market == "crypto"
                    else 0
                ),
                hold_days=hold_days_exact,
            )
            if is_single
            else calc_pair_performance(
                sig_type,
                entry_a,
                entry_b,
                price_a_now,
                price_b_now,
                capital=execution["capital"],
                leverage=execution["leverage"],
                taker_fee_pct=execution["taker_fee_pct"],
                funding_rate_8h_pct=(
                    execution["funding_rate_pct"]
                    if market == "crypto"
                    else 0
                ),
                hold_days=hold_days_exact,
                hedge_ratio=_query_float(
                    row.get("hedge_ratio_entry"), 1.0
                ),
            )
        )
        pnl_total = _query_float(
            performance.get("pair_move_pct"),
            0.0,
        )
        pair_risk = (
            {}
            if is_single
            else pair_risks.get(
                (market, row["ticker_a"], row["ticker_b"]),
                {},
            )
        )
        default_eligible = 1 if is_single else 0
        risk_reason = pair_risk.get("risk_reason")
        if not is_single and not pair_risk:
            risk_reason = "Пара отсутствует в свежем анализе"

        active_positions.append({
            "id": int(row["id"]),
            "pair": row["pair"],
            "market": market,
            "position_kind": position_kind,
            "source": row.get("source") or "signal",
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "signal": row.get("signal", ""),
            "signal_type": sig_type,
            "z_at_entry": row.get("z_at_entry"),
            "price_a_entry": round(float(entry_a), 4) if entry_a else None,
            "price_b_entry": round(float(entry_b), 4) if entry_b else None,
            "price_a_now": round(float(price_a_now), 4) if price_a_now else None,
            "price_b_now": round(float(price_b_now), 4) if price_b_now else None,
            "pnl_total_pct": round(float(pnl_total), 2),
            "entry_time": entry_time,
            "halflife": hl,
            "days_held": days_held,
            "hl_remaining": hl_remaining,
            "is_expired": is_expired,
            "hold_days_exact": round(hold_days_exact, 4),
            "legacy_execution_assumptions": execution[
                "legacy_execution_assumptions"
            ],
            "signal_eligible": _query_int(
                pair_risk.get("signal_eligible"),
                default_eligible,
            ) == 1,
            "is_coint_stable": _query_int(
                pair_risk.get("is_coint_stable"),
                0,
            ) == 1,
            "coint_stability": _query_float(
                pair_risk.get("coint_stability"),
                None,
            ),
            "market_regime": pair_risk.get("market_regime") or "normal",
            "market_volatility": _query_float(
                pair_risk.get("market_volatility"),
                None,
            ),
            "event_risk": _query_int(pair_risk.get("event_risk"), 0) == 1,
            "risk_reason": risk_reason,
            **timing,
            "corr": row.get("corr"),
            "status": row.get("status", "active"),
            **performance,
        })

    return {"favorites": active_positions, "total": len(active_positions)}


@router.get("/history")
async def get_favorites_history(
    limit: int | None = Query(None, ge=1),
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Get closed favorites history."""
    async with get_connection() as conn:
        hist = await fetch_favorites_history(conn, user.id, limit)

    return {
        "history": hist.to_dict(orient="records") if not hist.empty else [],
        "total": len(hist),
    }


@router.post("/toggle")
async def toggle_fav(
    pair: str = Query(...),
    ticker_a: str = Query(...),
    ticker_b: str = Query(...),
    signal: str = Query(""),
    signal_type: str = Query("wait"),
    z_at_entry: str | None = Query(None),
    price_a_entry: str | None = Query(None),
    price_b_entry: str | None = Query(None),
    halflife: str | None = Query(None),
    corr: str | None = Query(None),
    market: str = Query("crypto"),
    position_kind: str = Query("pair"),
    source: str = Query("signal"),
    capital: float = Query(1000.0, ge=10),
    leverage: float = Query(1.0, ge=1, le=20),
    taker_fee: float = Query(0.02, ge=0, le=1),
    funding_rate: float = Query(0.01, ge=0, le=1),
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Toggle favorite (add/remove)."""
    if position_kind not in {"pair", "single"}:
        raise HTTPException(status_code=400, detail="Unknown position type")
    if position_kind == "single":
        if source not in {"scanner_momentum", "scanner_drawdown"}:
            raise HTTPException(status_code=400, detail="Unknown scanner source")
        if signal_type not in {"long_a", "short_a"}:
            raise HTTPException(
                status_code=400,
                detail="Single position requires long or short direction",
            )
    async with get_connection() as conn:
        result = await toggle_favorite(
            conn, pair, ticker_a, ticker_b, user.id, market,
            signal=signal, signal_type=signal_type,
            z_at_entry=_query_float(z_at_entry, 0),
            price_a_entry=_query_float(price_a_entry, 0),
            price_b_entry=_query_float(price_b_entry, 0),
            halflife=_query_int(halflife),
            corr=_query_float(corr, 0),
            position_kind=position_kind,
            source=source,
            capital_at_entry=capital,
            leverage_at_entry=leverage,
            taker_fee_pct_at_entry=taker_fee,
            funding_rate_pct_at_entry=(
                funding_rate if market == "crypto" else 0
            ),
            calculation_version=CALCULATION_VERSION,
        )
    return result


@router.post("/refresh-ru")
async def refresh_ru_favorites(
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Refresh delayed MOEX quotes only for the user's active RU favorites."""
    if "ru" not in get_product_profile().enabled_markets:
        raise HTTPException(status_code=404, detail="Market is not available")

    async with get_connection() as conn:
        favorites = await fetch_favorites(conn, user.id)

    tickers = set()
    if not favorites.empty:
        for _, favorite in favorites.iterrows():
            if (favorite.get("market") or "crypto") == "ru":
                tickers.add(favorite["ticker_a"])
                if favorite.get("ticker_b"):
                    tickers.add(favorite["ticker_b"])
    if not tickers:
        raise HTTPException(
            status_code=400,
            detail="В избранном нет активных позиций рынка РФ",
        )

    try:
        result = await refresh_ru_live_prices(sorted(tickers))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="MOEX временно не отвечает. Попробуйте немного позже",
        ) from exc

    if not result["prices"]:
        raise HTTPException(
            status_code=502,
            detail="MOEX не вернул котировки для избранных инструментов",
        )

    updated_at = result["updated_at"]
    return {
        "ok": True,
        "updated": len(result["prices"]),
        "cached": result["cached"],
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


@router.post("/refresh-crypto")
async def refresh_crypto_favorites(
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Refresh MEXC quotes only for the user's active crypto favorites."""
    if "crypto" not in get_product_profile().enabled_markets:
        raise HTTPException(status_code=404, detail="Market is not available")

    async with get_connection() as conn:
        favorites = await fetch_favorites(conn, user.id)

    tickers = set()
    if not favorites.empty:
        for _, favorite in favorites.iterrows():
            if (favorite.get("market") or "crypto") == "crypto":
                tickers.add(favorite["ticker_a"])
                if favorite.get("ticker_b"):
                    tickers.add(favorite["ticker_b"])
    if not tickers:
        raise HTTPException(
            status_code=400,
            detail="В избранном нет активных crypto-позиций",
        )

    try:
        result = await refresh_crypto_live_prices(sorted(tickers))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="MEXC временно не отвечает. Попробуйте немного позже",
        ) from exc

    if not result["prices"]:
        raise HTTPException(
            status_code=502,
            detail="MEXC не вернул котировки для избранных инструментов",
        )

    updated_at = result["updated_at"]
    return {
        "ok": True,
        "updated": len(result["prices"]),
        "cached": result["cached"],
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


@router.post("/close/{fav_id}")
async def close_fav(
    fav_id: int,
    exit_price_a: float = Query(0),
    exit_price_b: float = Query(0),
    exit_pnl_pct: float | None = Query(None),
    use_net: bool = Query(False),
    capital: float = Query(1000.0, ge=10),
    leverage: float = Query(1.0, ge=1, le=20),
    taker_fee: float = Query(0.02, ge=0, le=1),
    funding_rate: float = Query(0.01, ge=0, le=1),
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Close an active favorite position."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM favorites
            WHERE id = ? AND status = 'active' AND user_id = ?
            """,
            (fav_id, user.id),
        )
        favorite = await cursor.fetchone()
        if not favorite:
            raise HTTPException(status_code=404, detail="Позиция не найдена")
        if favorite:
            market = favorite["market"] or "crypto"
            is_single = infer_position_kind(
                favorite["position_kind"],
                favorite["ticker_b"],
            ) == "single"
            performance = {"complete": False}
            tickers = [
                str(ticker)
                for ticker in (favorite["ticker_a"], favorite["ticker_b"])
                if ticker
            ]
            latest_prices: dict[tuple[str, str], float] = {}

            if market in {"crypto", "ru"}:
                try:
                    if market == "crypto":
                        live_result = await refresh_crypto_live_prices(
                            tickers,
                            ttl_seconds=0,
                        )
                        provider_name = "MEXC"
                    else:
                        live_result = await refresh_ru_live_prices(
                            tickers,
                            ttl_seconds=0,
                        )
                        provider_name = "MOEX"
                except Exception as exc:
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            f"{market.upper()}: не удалось получить свежую "
                            "серверную котировку. Позиция не закрыта."
                        ),
                    ) from exc

                live_prices = live_result.get("prices") or {}
                missing = [
                    ticker
                    for ticker in tickers
                    if _query_float(live_prices.get(ticker), 0.0) <= 0
                ]
                if missing:
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            f"{provider_name} не вернул свежую котировку для "
                            f"{', '.join(missing)}. Позиция не закрыта."
                        ),
                    )
                latest_prices.update({
                    (market, ticker): float(live_prices[ticker])
                    for ticker in tickers
                })

            for ticker in tickers:
                if (market, ticker) in latest_prices:
                    continue
                if not ticker:
                    continue
                price_cursor = await conn.execute(
                    """
                    SELECT close FROM prices
                    WHERE ticker = ? AND market = ?
                    ORDER BY date DESC LIMIT 1
                    """,
                    (ticker, market),
                )
                price_row = await price_cursor.fetchone()
                if price_row:
                    latest_prices[(market, ticker)] = float(price_row[0])

            # The server quote is authoritative. Client-supplied P&L and exit
            # prices must never be able to rewrite the trade journal.
            exit_price_a = _get_current_price(
                favorite["ticker_a"],
                latest_prices,
                market,
            )
            exit_price_b = (
                0.0
                if is_single
                else _get_current_price(
                    favorite["ticker_b"],
                    latest_prices,
                    market,
                )
            )
            entry_a = _query_float(favorite["price_a_entry"], 0.0)
            entry_b = _query_float(favorite["price_b_entry"], 0.0)
            can_price_position = (
                entry_a > 0
                and exit_price_a > 0
                and (is_single or (entry_b > 0 and exit_price_b > 0))
            )
            if not can_price_position:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Не удалось получить полные серверные котировки. "
                        "Позиция не закрыта."
                    ),
                )
            if can_price_position:
                timing = estimate_signal_timing(
                    favorite["entry_time"],
                    _query_int(favorite["halflife"]),
                )
                hold_days_exact = elapsed_holding_days(
                    favorite["entry_time"]
                )
                execution = frozen_execution_settings(
                    favorite,
                    fallback_capital=capital,
                    fallback_leverage=leverage,
                    fallback_taker_fee_pct=taker_fee,
                    fallback_funding_rate_pct=(
                        funding_rate if market == "crypto" else 0
                    ),
                )
                performance = (
                    calc_single_performance(
                        favorite["signal_type"],
                        entry_a,
                        exit_price_a,
                        capital=execution["capital"],
                        leverage=execution["leverage"],
                        taker_fee_pct=execution["taker_fee_pct"],
                        funding_rate_8h_pct=(
                            execution["funding_rate_pct"]
                            if market == "crypto"
                            else 0
                        ),
                        hold_days=hold_days_exact,
                    )
                    if is_single
                    else calc_pair_performance(
                        favorite["signal_type"],
                        entry_a,
                        entry_b,
                        exit_price_a,
                        exit_price_b,
                        capital=execution["capital"],
                        leverage=execution["leverage"],
                        taker_fee_pct=execution["taker_fee_pct"],
                        funding_rate_8h_pct=(
                            execution["funding_rate_pct"]
                            if market == "crypto"
                            else 0
                        ),
                        hold_days=hold_days_exact,
                        hedge_ratio=_query_float(
                            favorite["hedge_ratio_entry"], 1.0
                        ),
                    )
                )
            if not performance.get("complete"):
                raise HTTPException(
                    status_code=409,
                    detail="Расчёт позиции неполный. Позиция не закрыта.",
                )
            exit_pnl_pct = round(
                _query_float(
                    performance.get(
                        "net_return_pct"
                        if use_net
                        else "unlevered_return_pct"
                    ),
                    0.0,
                ),
                4,
            )
            exit_pair_move_pct = _query_float(
                performance.get("unlevered_return_pct"),
                0.0,
            )
            exit_net_return_pct = _query_float(
                performance.get("net_return_pct"),
                exit_pnl_pct,
            )
            exit_net_pnl = _query_float(
                performance.get("net_pnl"),
                execution["capital"] * exit_net_return_pct / 100,
            )
            exit_total_cost = _query_float(performance.get("total_cost"), 0.0)
        result = await close_favorite(
            conn,
            fav_id,
            exit_price_a,
            exit_price_b,
            exit_pnl_pct,
            user_id=user.id,
            exit_net_pnl=round(exit_net_pnl, 2),
            exit_net_return_pct=round(exit_net_return_pct, 4),
            exit_pair_move_pct=round(exit_pair_move_pct, 4),
            exit_total_cost=round(exit_total_cost, 2),
            close_capital=round(float(execution["capital"]), 2),
            exit_spread_move_pp=round(
                _query_float(performance.get("spread_move_pp"), 0.0),
                4,
            ),
            exit_unlevered_return_pct=round(
                _query_float(
                    performance.get("unlevered_return_pct"),
                    0.0,
                ),
                4,
            ),
            exit_gross_pnl=round(
                _query_float(performance.get("gross_pnl"), 0.0),
                2,
            ),
            exit_gross_return_pct=round(
                _query_float(performance.get("gross_return_pct"), 0.0),
                4,
            ),
            exit_hold_days=round(hold_days_exact, 6),
            exit_leverage=round(float(execution["leverage"]), 4),
            exit_taker_fee_pct=round(
                float(execution["taker_fee_pct"]),
                6,
            ),
            exit_funding_rate_pct=round(
                float(execution["funding_rate_pct"]),
                6,
            ),
            calculation_version=CALCULATION_VERSION,
        )
    return result


@router.delete("/{fav_id}")
async def delete_fav(
    fav_id: int,
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Delete a favorite from history."""
    async with get_connection() as conn:
        result = await delete_favorite(conn, fav_id, user_id=user.id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return result
