"""Health check and monitoring endpoints."""

import time
import os
from fastapi import APIRouter, HTTPException
from app.db.database import get_connection, db_status, DB_PATH
from app.config import get_settings
import httpx
import asyncio

router = APIRouter(tags=["health"])

START_TIME = time.time()
settings = get_settings()


def get_uptime() -> float:
    return time.time() - START_TIME


@router.get("/health")
async def health_check():
    """Basic health check endpoint."""
    uptime = get_uptime()
    db_ok = os.path.exists(DB_PATH)
    
    status = {
        "status": "ok" if db_ok else "degraded",
        "version": "1.0.0",
        "db_path": DB_PATH,
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
                status["active_signals"] = db["n_active_signals"]
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
    """Kubernetes readiness probe."""
    db_ok = os.path.exists(DB_PATH)
    if not db_ok:
        raise HTTPException(status_code=503, detail="Database not available")
    return {"status": "ready"}


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
    
    if os.path.exists(DB_PATH):
        lines.append(f"# HELP cryptoscope_db_exists Database file exists")
        lines.append(f"# TYPE cryptoscope_db_exists gauge")
        lines.append("cryptoscope_db_exists 1")
    
    return "\n".join(lines)
