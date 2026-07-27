"""Admin-only forward test for the Momentum risk portfolio."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.momentum_portfolio import fetch_momentum_portfolio_report
from app.db.database import get_connection
from app.ui.templates import templates

router = APIRouter(
    prefix="/tab/momentum-portfolio",
    tags=["momentum-portfolio"],
)


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("", response_class=HTMLResponse)
async def momentum_portfolio_tab(request: Request):
    await _require_admin(request)
    async with get_connection() as conn:
        report = await fetch_momentum_portfolio_report(conn)
    return templates.TemplateResponse(
        request,
        "components/momentum_portfolio_tab.html",
        {
            "report": report,
            "current": report["current"],
        },
    )
