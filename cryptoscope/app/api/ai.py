"""AI Analyst API endpoints."""

from fastapi import APIRouter, Query, HTTPException, Request
from app.db.database import get_connection, fetch_pairs
from app.config import get_settings
from app.core.ai import build_analysis_prompt, call_deepseek
from app.i18n import request_locale
from app.product import get_product_profile

router = APIRouter(prefix="/ai", tags=["ai"])
settings = get_settings()


@router.post("/analyze")
async def analyze_market(
    request: Request,
    market: str = Query("crypto"),
    api_key: str = Query("", description="DeepSeek API key (uses DEEPSEEK_API_KEY env if empty)"),
):
    """Run AI analysis on current market signals."""
    key = api_key or settings.deepseek_api_key
    
    if not key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_API_KEY not configured")
    
    async with get_connection() as conn:
        pairs = await fetch_pairs(conn, market, min_corr=0.5)
    
    if pairs.empty:
        raise HTTPException(status_code=404, detail="No pair data available")
    
    # Build context
    active = pairs[pairs["signal_type"] != "wait"].head(10)
    top = pairs.head(5)
    
    active_signals = []
    for _, row in active.iterrows():
        active_signals.append({
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "z_now": row.get("z_now"),
            "z_forecast": row.get("z_forecast"),
            "signal": row["signal"],
            "strength": row.get("strength", "Нет"),
        })
    
    top_pairs = []
    for _, row in top.iterrows():
        top_pairs.append({
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "corr": row.get("corr"),
            "is_coint": bool(row.get("is_coint")),
            "score": row.get("score"),
        })
    
    context = {
        "market": market,
        "active_signals": active_signals,
        "top_pairs": top_pairs,
    }
    
    locale = request_locale(request, get_product_profile())
    prompt = build_analysis_prompt(context, locale=locale)
    result = await call_deepseek(key, prompt, locale=locale)
    
    if result["error"]:
        raise HTTPException(status_code=500, detail=result["error"])
    
    return {
        "response": result["response"],
        "tokens": result["tokens"],
        "market": market,
        "n_active": len(active_signals),
        "n_total": len(pairs),
    }


@router.get("/check")
async def check_ai_config():
    """Check if DeepSeek API is configured."""
    key = settings.deepseek_api_key
    return {
        "configured": bool(key),
        "key_masked": key[:8] + "..." if len(key) > 8 else "not set",
    }
