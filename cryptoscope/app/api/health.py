import os
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException

import app.db.database as database
from app.config import get_settings
from app.db.database import db_status, get_connection
from app.product import ALL_MARKETS, get_product_profile

router = APIRouter(tags=["health"])

START_TIME = time.time()
settings = get_settings()


def get_uptime() -> float:
    return time.time() - START_TIME


def _timestamp_age_hours(value: object, now: datetime) -> float | None:
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed.astimezone(UTC)).total_seconds() / 3600


@router.get("/health")
async def health_check():
    """Basic health check endpoint."""
    uptime = get_uptime()
    db_path = database.DB_PATH
    db_ok = os.path.exists(db_path)
    
    status = {
        "status": "ok" if db_ok else "degraded",
        "version": "1.0.0",
        "db_path": db_path,
        "db_exists": db_ok,
        "uptime_seconds": round(uptime, 1),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    
    if db_ok:
        try:
            async with get_connection() as conn:
                db = await db_status(conn)
                status["db_tickers"] = db["n_tickers"]
                status["db_rows"] = db["n_rows"]
                status["db_pairs"] = db["n_pairs"]
                status["last_analysis"] = db["last_analysis"]
                active_by_market = {
                    market: count
                    for market, count in db["active_signals_by_market"].items()
                    if market in ALL_MARKETS
                }
                status["active_signals"] = sum(active_by_market.values())
                status["active_signals_by_market"] = active_by_market
                status["pair_diagnostics_by_market"] = {
                    market: details
                    for market, details in db[
                        "pair_diagnostics_by_market"
                    ].items()
                    if market in ALL_MARKETS
                }
                status["stability_patterns_by_market"] = {
                    market: details
                    for market, details in db[
                        "stability_patterns_by_market"
                    ].items()
                    if market in ALL_MARKETS
                }
        except Exception as e:
            status["db_error"] = str(e)
    
    if not db_ok:
        status["status"] = "degraded"
    
    return status


@router.get("/health/live")
async def liveness():
    """Kubernetes liveness probe."""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness():
    """Verify that serving data and precomputed artifacts are usable."""
    db_path = database.DB_PATH
    if not os.path.exists(db_path):
        raise HTTPException(status_code=503, detail="Database not available")

    profile = get_product_profile(settings)
    required_tables = {
        "prices",
        "pairs",
        "scanner_signal_periods",
        "extension_feed_snapshots",
        "crypto_strategy_trades",
        "crypto_price_versions",
        "market_data_state",
    }
    problems: list[str] = []
    now = datetime.now(UTC)

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            )
            available_tables = {
                str(row["name"]) for row in await cursor.fetchall()
            }
            missing_tables = sorted(required_tables - available_tables)
            if missing_tables:
                problems.append(
                    "missing tables: " + ", ".join(missing_tables)
                )

            if not missing_tables:
                cursor = await conn.execute(
                    "SELECT COUNT(*) AS count FROM prices"
                )
                if int((await cursor.fetchone())["count"] or 0) < 1:
                    problems.append("prices are empty")

                cursor = await conn.execute(
                    "SELECT COUNT(*) AS count FROM pairs"
                )
                if int((await cursor.fetchone())["count"] or 0) < 1:
                    problems.append("pair analysis is empty")

                for market in profile.enabled_markets:
                    cursor = await conn.execute(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM prices WHERE market = ?) AS prices,
                            (
                                SELECT MAX(date)
                                FROM prices
                                WHERE market = ?
                            ) AS price_date,
                            (SELECT COUNT(*) FROM pairs WHERE market = ?) AS pairs,
                            (
                                SELECT MAX(computed_at)
                                FROM pairs
                                WHERE market = ?
                            ) AS analysis_at,
                            (
                                SELECT generated_at
                                FROM extension_feed_snapshots
                                WHERE market = ?
                            ) AS snapshot_at
                        """,
                        (market, market, market, market, market),
                    )
                    row = await cursor.fetchone()
                    if int(row["prices"] or 0) < 1:
                        problems.append(f"{market}: no prices")
                    if market == "crypto":
                        state_cursor = await conn.execute(
                            """
                            SELECT
                                active_provider,
                                latest_data_date,
                                ticker_count,
                                row_count
                            FROM market_data_state
                            WHERE market = 'crypto'
                            """
                        )
                        state = await state_cursor.fetchone()
                        if not state:
                            problems.append(
                                "crypto: provider state is missing"
                            )
                        elif str(state["active_provider"]).lower() != "mexc":
                            problems.append(
                                "crypto: active provider is not MEXC"
                            )
                        else:
                            provider_cursor = await conn.execute(
                                """
                                SELECT
                                    COUNT(*) AS rows,
                                    COUNT(DISTINCT ticker) AS tickers,
                                    COUNT(DISTINCT provider) AS providers,
                                    MIN(provider) AS provider
                                FROM prices
                                WHERE market = 'crypto'
                                """
                            )
                            provider_row = await provider_cursor.fetchone()
                            if (
                                int(provider_row["rows"] or 0)
                                != int(state["row_count"] or 0)
                                or int(provider_row["tickers"] or 0)
                                != int(state["ticker_count"] or 0)
                            ):
                                problems.append(
                                    "crypto: provider state does not match prices"
                                )
                            if (
                                int(provider_row["providers"] or 0) != 1
                                or str(provider_row["provider"]).lower()
                                != "mexc"
                            ):
                                problems.append(
                                    "crypto: mixed or unexpected price providers"
                                )
                    price_age = _timestamp_age_hours(
                        row["price_date"],
                        now,
                    )
                    price_max_age = (
                        settings.readiness_crypto_price_max_age_hours
                        if market == "crypto"
                        else settings.readiness_equity_price_max_age_hours
                    )
                    if price_age is None:
                        problems.append(f"{market}: price date is missing")
                    elif price_age > max(1, price_max_age):
                        problems.append(
                            f"{market}: prices are stale ({price_age:.1f}h)"
                        )
                    if int(row["pairs"] or 0) < 1:
                        problems.append(f"{market}: no pair analysis")
                    analysis_age = _timestamp_age_hours(
                        row["analysis_at"],
                        now,
                    )
                    if analysis_age is None:
                        problems.append(f"{market}: analysis timestamp is missing")
                    elif analysis_age > max(
                        1,
                        settings.readiness_max_age_hours,
                    ):
                        problems.append(
                            f"{market}: analysis is stale ({analysis_age:.1f}h)"
                        )
                    snapshot_age = _timestamp_age_hours(
                        row["snapshot_at"],
                        now,
                    )
                    if snapshot_age is None:
                        problems.append(f"{market}: extension feed is missing")
                    elif snapshot_age > max(
                        1,
                        settings.readiness_max_age_hours,
                    ):
                        problems.append(
                            f"{market}: extension feed is stale "
                            f"({snapshot_age:.1f}h)"
                        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Database readiness check failed: {exc}",
        ) from exc

    if problems:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "problems": problems},
        )
    return {
        "status": "ready",
        "markets": list(profile.enabled_markets),
    }


@router.post("/health/alert")
async def send_telegram_alert(message: str = ""):
    """Send alert to Telegram (if configured)."""
    bot_token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    
    if not bot_token or not chat_id:
        return {"status": "skipped", "reason": "Telegram not configured"}
    
    if not message:
        message = "MEANX health check alert"
    
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": f"[MEANX] {message}",
                "parse_mode": "HTML",
            })
            resp.raise_for_status()
        return {"status": "sent", "message": message}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Telegram send failed: {e}")


@router.get("/health/metrics")
async def metrics():
    """Prometheus-style metrics endpoint."""
    uptime = get_uptime()
    lines = [
        f"# HELP cryptoscope_uptime_seconds Application uptime",
        f"# TYPE cryptoscope_uptime_seconds gauge",
        f"cryptoscope_uptime_seconds {uptime:.1f}",
    ]
    
    if os.path.exists(database.DB_PATH):
        lines.append(f"# HELP cryptoscope_db_exists Database file exists")
        lines.append(f"# TYPE cryptoscope_db_exists gauge")
        lines.append("cryptoscope_db_exists 1")
    
    return "\n".join(lines)
