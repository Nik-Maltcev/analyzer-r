"""Server-side session helpers for passwordless authentication."""

from dataclasses import dataclass
from hashlib import sha256

from fastapi import HTTPException, Request

from app.config import get_settings
from app.db.database import get_connection

SESSION_COOKIE_NAME = "cryptoscope_session"


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str


def hash_auth_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


async def get_current_user(request: Request) -> AuthUser | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT users.id, users.email
            FROM auth_sessions AS sessions
            JOIN auth_users AS users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
              AND sessions.expires_at > datetime('now')
            LIMIT 1
            """,
            (hash_auth_token(token),),
        )
        row = await cursor.fetchone()

    if not row:
        return None
    return AuthUser(id=str(row["id"]), email=str(row["email"]))


def auth_is_configured() -> bool:
    return bool(get_settings().resend_api_key.strip())


async def get_current_or_legacy_user(request: Request) -> AuthUser | None:
    user = await get_current_user(request)
    if user is not None:
        return user
    if not auth_is_configured():
        return AuthUser(id="local", email="")
    return None


async def require_current_user(request: Request) -> AuthUser:
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Войдите по ссылке из письма",
        )
    return user


async def require_current_or_legacy_user(request: Request) -> AuthUser:
    user = await get_current_or_legacy_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Войдите по ссылке из письма",
        )
    return user
