"""Passwordless authentication through one-time links sent by Resend."""

from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from html import escape
import os
import re
import secrets
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth import (
    SESSION_COOKIE_NAME,
    AuthUser,
    auth_is_configured,
    get_current_user,
    hash_auth_token,
    require_current_user,
)
from app.config import get_settings
from app.db.database import get_connection

router = APIRouter(prefix="/auth", tags=["auth"])
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MagicLinkPayload(BaseModel):
    email: str


def _normalize_email(value: str) -> str:
    email = parseaddr(value.strip())[1].strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=422, detail="Проверьте адрес электронной почты")
    return email


def _utc_sql(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _request_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:128]
    return (request.client.host if request.client else "unknown")[:128]


def _base_url(request: Request) -> str:
    settings = get_settings()
    if settings.app_base_url:
        return settings.app_base_url.rstrip("/")
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        return f"https://{railway_domain}".rstrip("/")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    return f"{scheme}://{host}".rstrip("/")


def _cookie_is_secure(request: Request) -> bool:
    settings = get_settings()
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return (
        forwarded_proto.split(",", 1)[0].strip().lower() == "https"
        or request.url.scheme == "https"
        or settings.app_base_url.lower().startswith("https://")
        or bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    )


async def send_magic_link_email(email: str, magic_link: str, request_id: str) -> None:
    settings = get_settings()
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")

    safe_link = escape(magic_link, quote=True)
    payload = {
        "from": settings.resend_from_email,
        "to": [email],
        "subject": "Вход в CryptoScope",
        "html": (
            '<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;'
            'padding:32px;color:#172033">'
            '<h1 style="font-size:22px;margin:0 0 16px">Вход в CryptoScope</h1>'
            '<p style="line-height:1.55;margin:0 0 24px">'
            "Нажмите кнопку, чтобы войти. Ссылка действует 15 минут и сработает один раз."
            "</p>"
            f'<a href="{safe_link}" style="display:inline-block;padding:12px 20px;'
            'background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;'
            'font-weight:600">Войти в CryptoScope</a>'
            '<p style="font-size:12px;color:#667085;line-height:1.5;margin:24px 0 0">'
            "Если вы не запрашивали вход, просто проигнорируйте письмо.</p>"
            "</div>"
        ),
        "text": (
            "Вход в CryptoScope\n\n"
            f"Откройте одноразовую ссылку: {magic_link}\n\n"
            "Ссылка действует 15 минут."
        ),
    }
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": request_id,
        "User-Agent": "CryptoScope/1.0",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()


@router.post("/magic-link")
async def request_magic_link(payload: MagicLinkPayload, request: Request):
    settings = get_settings()
    if not settings.resend_api_key:
        raise HTTPException(
            status_code=503,
            detail="Отправка писем пока не настроена",
        )

    email = _normalize_email(payload.email)
    request_ip = _request_ip(request)
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    token_hash = hash_auth_token(token)
    expires_at = now + timedelta(minutes=settings.magic_link_ttl_minutes)

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                SUM(CASE WHEN email = ? THEN 1 ELSE 0 END) AS email_requests,
                SUM(CASE WHEN request_ip = ? THEN 1 ELSE 0 END) AS ip_requests
            FROM auth_magic_links
            WHERE created_at >= datetime('now', '-15 minutes')
            """,
            (email, request_ip),
        )
        rate_row = await cursor.fetchone()
        email_requests = int(rate_row["email_requests"] or 0)
        ip_requests = int(rate_row["ip_requests"] or 0)
        if email_requests >= 5 or ip_requests >= 20:
            raise HTTPException(
                status_code=429,
                detail="Слишком много запросов. Попробуйте через 15 минут",
            )

        await conn.execute(
            """
            INSERT INTO auth_magic_links (
                token_hash, email, expires_at, request_ip
            )
            VALUES (?, ?, ?, ?)
            """,
            (token_hash, email, _utc_sql(expires_at), request_ip),
        )
        await conn.commit()

    magic_link = f"{_base_url(request)}/api/auth/verify?token={quote(token)}"
    request_id = f"magic-link-{token_hash[:32]}"
    try:
        await send_magic_link_email(email, magic_link, request_id)
    except (httpx.HTTPError, RuntimeError) as exc:
        async with get_connection() as conn:
            await conn.execute(
                "DELETE FROM auth_magic_links WHERE token_hash = ?",
                (token_hash,),
            )
            await conn.commit()
        raise HTTPException(
            status_code=502,
            detail="Не удалось отправить письмо. Попробуйте ещё раз",
        ) from exc

    return {
        "ok": True,
        "message": "Ссылка для входа отправлена на почту",
        "expires_in_minutes": settings.magic_link_ttl_minutes,
    }


@router.get("/verify")
async def verify_magic_link(token: str, request: Request):
    settings = get_settings()
    token_hash = hash_auth_token(token)
    now = datetime.now(UTC)

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT email
            FROM auth_magic_links
            WHERE token_hash = ?
              AND used_at IS NULL
              AND expires_at > datetime('now')
            LIMIT 1
            """,
            (token_hash,),
        )
        magic_row = await cursor.fetchone()
        if not magic_row:
            return RedirectResponse(url="/?auth=invalid", status_code=303)

        cursor = await conn.execute(
            """
            UPDATE auth_magic_links
            SET used_at = datetime('now')
            WHERE token_hash = ? AND used_at IS NULL
            """,
            (token_hash,),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            return RedirectResponse(url="/?auth=invalid", status_code=303)

        email = str(magic_row["email"])
        cursor = await conn.execute(
            "SELECT id FROM auth_users WHERE email = ? LIMIT 1",
            (email,),
        )
        user_row = await cursor.fetchone()
        user_id = str(user_row["id"]) if user_row else str(uuid4())
        if user_row:
            await conn.execute(
                "UPDATE auth_users SET last_login_at = datetime('now') WHERE id = ?",
                (user_id,),
            )
        else:
            await conn.execute(
                """
                INSERT INTO auth_users (id, email, last_login_at)
                VALUES (?, ?, datetime('now'))
                """,
                (user_id, email),
            )

        legacy_owner = settings.auth_legacy_owner_email.strip().lower()
        if legacy_owner and email == legacy_owner:
            await conn.execute(
                """
                UPDATE favorites
                SET user_id = ?
                WHERE user_id = 'local'
                """,
                (user_id,),
            )

        session_token = secrets.token_urlsafe(32)
        session_hash = hash_auth_token(session_token)
        session_expires = now + timedelta(days=settings.auth_session_days)
        await conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES (?, ?, ?)
            """,
            (session_hash, user_id, _utc_sql(session_expires)),
        )
        await conn.execute(
            """
            DELETE FROM auth_sessions
            WHERE expires_at <= datetime('now')
            """
        )
        await conn.commit()

    response = RedirectResponse(url="/?auth=success", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=settings.auth_session_days * 24 * 60 * 60,
        httponly=True,
        secure=_cookie_is_secure(request),
        samesite="lax",
        path="/",
    )
    return response


@router.get("/me")
async def auth_me(user: AuthUser | None = Depends(get_current_user)):
    if user is None:
        return {
            "authenticated": False,
            "auth_available": auth_is_configured(),
            "email": None,
        }
    return {
        "authenticated": True,
        "auth_available": True,
        "email": user.email,
    }


@router.post("/logout")
async def auth_logout(
    request: Request,
    user: AuthUser = Depends(require_current_user),
):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        async with get_connection() as conn:
            await conn.execute(
                """
                DELETE FROM auth_sessions
                WHERE token_hash = ? AND user_id = ?
                """,
                (hash_auth_token(token), user.id),
            )
            await conn.commit()

    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
