"""Small public market feed used by the browser extension."""

from datetime import UTC, datetime
from time import monotonic

import numpy as np
from fastapi import APIRouter, Query, Response

from app.core.scanners import drawdown_scan, momentum_scan
from app.core.signals import estimate_signal_timing, is_actionable_signal
from app.db.database import fetch_pairs, fetch_prices, get_connection
from app.product import get_product_profile, require_market_enabled

router = APIRouter(prefix="/public/extension", tags=["public-extension"])
SCANNER_CACHE_TTL_SECONDS = 300
_scanner_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

MARKET_LABELS = {
    "br": "B3",
    "stocks": "Ações EUA",
    "crypto": "Cripto",
}


def _finite_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if np.isfinite(number) else default


def _finite_int(value, default=None):
    number = _finite_float(value)
    return int(number) if number is not None else default


def _is_validated(row) -> bool:
    value = row.get("is_coint_stable")
    if value is None:
        value = row.get("is_coint")
    return bool(_finite_int(value, 0))


def _clean_ticker(ticker: str) -> str:
    return str(ticker).replace("/USD", "")


def _present_pair(row, market: str) -> dict:
    signal_type = str(row.get("signal_type") or "wait")
    ticker_a = str(row["ticker_a"])
    ticker_b = str(row["ticker_b"])
    a = _clean_ticker(ticker_a)
    b = _clean_ticker(ticker_b)
    long_a = signal_type == "long_a"
    timing = estimate_signal_timing(
        row.get("signal_started_at"),
        _finite_int(row.get("halflife")),
        fallback_started_at=row.get("computed_at"),
    )

    return {
        "id": f"{market}:{ticker_a}:{ticker_b}",
        "source": "pair",
        "market": market,
        "market_label": MARKET_LABELS.get(market, market.upper()),
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "pair": f"{a} / {b}",
        "direction": signal_type,
        "recommendation": (
            f"Comprar {a} / Vender {b}"
            if long_a
            else f"Vender {a} / Comprar {b}"
        ),
        "z_score": round(_finite_float(row.get("z_now"), 0.0), 2),
        "correlation_pct": round(_finite_float(row.get("corr"), 0.0) * 100),
        "estimated_days": _finite_int(row.get("halflife")),
        "signal_days": timing["signal_days_elapsed"],
        "review_in_days": timing["signal_days_remaining"],
        "started_at": timing["signal_started_at"],
        "primary_metric": f"Z {_finite_float(row.get('z_now'), 0.0):.2f}",
        "secondary_metric": f"corr. {round(_finite_float(row.get('corr'), 0.0) * 100)}%",
    }


def _present_scanner(row, market: str) -> dict:
    ticker = str(row["ticker_a"])
    scanner = str(row["scanner"])
    direction = str(row["direction"])
    age = max(1, _finite_int(row["observation_count"], 1))
    horizon = 5 if scanner == "momentum" else 10
    action = "Comprar" if direction == "long" else "Vender"
    if scanner == "momentum":
        metric = f"Momentum {_finite_float(row.get('momentum_score'), 0.0):+.1f}"
    else:
        metric = f"Drawdown -{_finite_float(row.get('drawdown_pct'), 0.0):.1f}%"

    return {
        "id": f"{market}:scanner:{scanner}:{ticker}:{direction}",
        "source": scanner,
        "market": market,
        "market_label": MARKET_LABELS.get(market, market.upper()),
        "ticker_a": ticker,
        "ticker_b": "",
        "pair": _clean_ticker(ticker),
        "direction": direction,
        "recommendation": f"Considerar {action.lower()} {_clean_ticker(ticker)}",
        "z_score": None,
        "correlation_pct": None,
        "estimated_days": horizon,
        "signal_days": age,
        "review_in_days": max(0, horizon - age),
        "started_at": row["first_seen_date"],
        "confidence": "high",
        "primary_metric": metric,
        "secondary_metric": "Confiança alta",
    }


def _is_high_confidence_scanner(row: dict, scanner: str) -> bool:
    direction = str(row.get("recommendation_class") or "wait")
    if scanner == "momentum":
        if direction not in {"long", "short"}:
            return False
        sign = 1 if direction == "long" else -1
        confirmations = sum(
            1
            for key in ("pct_3d", "pct_7d", "pct_14d")
            if np.sign(_finite_float(row.get(key), 0.0)) == sign
        )
        return bool(
            confirmations == 3
            and abs(_finite_float(row.get("momentum_score"), 0.0)) >= 10
            and _finite_float(row.get("volatility_7d"), 0.0) < 8
        )
    return bool(
        direction == "long"
        and _finite_float(row.get("pct_3d"), 0.0) >= 3
        and _finite_float(row.get("pct_7d"), 0.0) >= 3
        and _finite_float(row.get("drawdown_pct"), 0.0) < 30
    )


def _high_confidence_scanner_items(prices, periods, market: str) -> list[dict]:
    if prices.empty:
        return []

    wide = prices.pivot(index="date", columns="ticker", values="close")
    tickers = list(wide.columns)
    frames = {
        "momentum": momentum_scan(
            wide.values,
            tickers,
            list(wide.index.astype(str)),
        ),
        "drawdown": drawdown_scan(wide.values, tickers),
    }
    period_map = {
        (str(row["scanner"]), str(row["ticker_a"]), str(row["direction"])): row
        for row in periods
    }
    candidates = []
    for scanner, frame in frames.items():
        if frame.empty:
            continue
        for record in frame.to_dict(orient="records"):
            if not _is_high_confidence_scanner(record, scanner):
                continue
            ticker = str(record["ticker"])
            direction = str(record["recommendation_class"])
            period = period_map.get((scanner, ticker, direction), {})
            record.update({
                "scanner": scanner,
                "ticker_a": ticker,
                "direction": direction,
                "observation_count": period.get("observation_count", 1),
                "first_seen_date": period.get("first_seen_date", str(max(wide.index))[:10]),
            })
            rank = (
                abs(_finite_float(record.get("momentum_score"), 0.0))
                if scanner == "momentum"
                else _finite_float(record.get("drawdown_pct"), 0.0)
            )
            candidates.append((rank, record))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [_present_scanner(record, market) for _, record in candidates]


@router.get("/feed")
async def extension_feed(
    response: Response,
    market: str | None = Query(None),
):
    """Return pair ideas and every high-confidence scanner recommendation."""
    profile = get_product_profile()
    selected_market = require_market_enabled(market or profile.default_market, profile)

    async with get_connection() as conn:
        pairs = await fetch_pairs(conn, selected_market, min_corr=0.5)
        cursor = await conn.execute(
            """
            SELECT scanner, ticker_a, direction, first_seen_date,
                   observation_count, last_seen_date
            FROM scanner_signal_periods
            WHERE market = ?
              AND scanner IN ('momentum', 'drawdown')
              AND direction IN ('long', 'short')
              AND status = 'active'
            ORDER BY observation_count DESC, updated_at DESC
            """,
            (selected_market,),
        )
        scanner_rows = [dict(row) for row in await cursor.fetchall()]

        db_cursor = await conn.execute("PRAGMA database_list")
        db_row = await db_cursor.fetchone()
        cache_key = (str(db_row["file"]), selected_market)
        cached = _scanner_cache.get(cache_key)
        if cached and monotonic() - cached[0] < SCANNER_CACHE_TTL_SECONDS:
            scanner_items = cached[1]
        else:
            prices = await fetch_prices(conn, selected_market)
            scanner_items = _high_confidence_scanner_items(
                prices,
                scanner_rows,
                selected_market,
            )
            _scanner_cache[cache_key] = (monotonic(), scanner_items)

    items = []
    if not pairs.empty:
        candidates = []
        for _, row in pairs.iterrows():
            signal_type = row.get("signal_type")
            halflife = _finite_int(row.get("halflife"))
            validated = _is_validated(row)
            if (
                halflife is None
                or halflife <= 0
                or halflife > 30
                or not is_actionable_signal(signal_type, validated)
            ):
                continue
            candidates.append(row)

        candidates.sort(
            key=lambda row: (
                abs(_finite_float(row.get("z_now"), 0.0)),
                _finite_float(row.get("score"), 0.0),
            ),
            reverse=True,
        )
        items = [_present_pair(row, selected_market) for row in candidates[:4]]

    items.extend(scanner_items)

    response.headers["Cache-Control"] = (
        "public, max-age=300"
        if items
        else "no-store"
    )
    return {
        "market": selected_market,
        "market_label": MARKET_LABELS.get(selected_market, selected_market.upper()),
        "updated_at": datetime.now(UTC).isoformat(),
        "items": items,
        "total": len(items),
        "full_analysis_url": f"/app?market={selected_market}",
    }
