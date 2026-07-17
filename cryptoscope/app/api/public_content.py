"""Public read-only assets used by social publishing providers."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get_settings

router = APIRouter(prefix="/public/content", tags=["public-content"])
SAFE_CARD_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.(?:png|jpe?g)$",
    re.IGNORECASE,
)


@router.get("/cards/{filename}", response_class=FileResponse)
async def public_content_card(filename: str) -> FileResponse:
    """Serve a generated card so Meta can fetch it for a Threads image post."""
    if not SAFE_CARD_NAME.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Card not found")
    card_dir = Path(get_settings().content_card_dir).resolve()
    card_path = (card_dir / filename).resolve()
    if card_path.parent != card_dir or not card_path.is_file():
        raise HTTPException(status_code=404, detail="Card not found")
    return FileResponse(
        card_path,
        media_type=(
            "image/png"
            if card_path.suffix.lower() == ".png"
            else "image/jpeg"
        ),
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )
