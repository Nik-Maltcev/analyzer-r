"""Data view API endpoints (Данные tab)."""

import os
from fastapi import APIRouter, Query
from app.db.database import get_connection, db_status

router = APIRouter(prefix="/data", tags=["data"])


@router.get("/status")
async def get_data_status():
    """Get database status for data tab."""
    async with get_connection() as conn:
        status = await db_status(conn)
    return status


@router.get("/preview")
async def get_data_preview(
    market: str = Query("crypto"),
    limit: int = Query(100, le=500),
):
    """Get data preview table."""
    async with get_connection() as conn:
        if market:
            cursor = await conn.execute(
                "SELECT ticker, date, close FROM prices WHERE market = ? ORDER BY ticker, date DESC LIMIT ?",
                (market, limit)
            )
        else:
            cursor = await conn.execute(
                "SELECT ticker, date, close, market FROM prices ORDER BY ticker, date DESC LIMIT ?",
                (limit,)
            )
        rows = await cursor.fetchall()
    
    return {
        "rows": [dict(r) for r in rows],
        "total": len(rows),
    }


@router.get("/update-info")
async def get_update_info():
    """Get last update time and next update schedule."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT timestamp, market FROM update_log ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        last_update = dict(row) if row else None
        
        cursor = await conn.execute("SELECT MAX(computed_at) FROM pairs")
        row = await cursor.fetchone()
        last_analysis = row[0] if row else None
    
    return {
        "last_update": last_update,
        "last_analysis": last_analysis,
        "next_update": "ежедневно в 06:00 UTC (09:00 МСК)",
    }
