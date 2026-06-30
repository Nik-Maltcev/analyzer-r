"""Locale selection for bilingual product editions."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.i18n import LOCALE_COOKIE_NAME
from app.product import get_product_profile

router = APIRouter(prefix="/locale", tags=["locale"])


@router.post("")
async def set_locale(lang: str = Query(...)):
    profile = get_product_profile()
    if lang not in profile.supported_locales:
        raise HTTPException(status_code=400, detail="Unsupported locale")
    response = JSONResponse({"ok": True, "locale": lang})
    response.set_cookie(
        LOCALE_COOKIE_NAME,
        lang,
        max_age=365 * 24 * 60 * 60,
        httponly=False,
        samesite="lax",
        secure=True,
    )
    return response
