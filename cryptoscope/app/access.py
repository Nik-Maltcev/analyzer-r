"""Trial and paid subscription access rules."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from app.auth import AuthUser
from app.config import get_settings
from app.db.database import get_connection


@dataclass(frozen=True)
class AccessState:
    status: str
    has_access: bool
    plan: str | None = None
    access_until: str | None = None
    remaining_days: int = 0
    last_transaction_id: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _active_state(
    status: str,
    until: datetime,
    plan: str | None = None,
    last_transaction_id: str | None = None,
) -> AccessState:
    seconds = max(0.0, (until - datetime.now(UTC)).total_seconds())
    return AccessState(
        status=status,
        has_access=seconds > 0,
        plan=plan,
        access_until=until.isoformat(),
        remaining_days=max(1, math.ceil(seconds / 86400)) if seconds else 0,
        last_transaction_id=last_transaction_id,
    )


def is_admin_user(user: AuthUser | None) -> bool:
    if user is None:
        return False
    settings = get_settings()
    admin_emails = {
        email.strip().lower()
        for email in settings.auth_admin_emails.replace(";", ",").split(",")
        if email.strip()
    }
    owner_email = settings.auth_legacy_owner_email.strip().lower()
    if owner_email:
        admin_emails.add(owner_email)
    return user.email.strip().lower() in admin_emails


async def get_access_state(user: AuthUser | None) -> AccessState:
    settings = get_settings()
    if not settings.resend_api_key:
        return AccessState(status="unrestricted", has_access=True)
    if user is None:
        return AccessState(status="unauthenticated", has_access=False)

    if is_admin_user(user):
        return AccessState(status="admin", has_access=True)

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT users.trial_ends_at,
                   subscriptions.plan,
                   subscriptions.status AS subscription_status,
                   subscriptions.access_until,
                   subscriptions.last_transaction_id
            FROM auth_users AS users
            LEFT JOIN user_subscriptions AS subscriptions
              ON subscriptions.user_id = users.id
            WHERE users.id = ?
            LIMIT 1
            """,
            (user.id,),
        )
        row = await cursor.fetchone()

    if not row:
        return AccessState(status="unauthenticated", has_access=False)

    now = datetime.now(UTC)
    subscription_until = _parse_utc(row["access_until"])
    if (
        row["subscription_status"] == "active"
        and subscription_until
        and subscription_until > now
    ):
        return _active_state(
            "subscription",
            subscription_until,
            str(row["plan"]),
            str(row["last_transaction_id"]),
        )

    trial_until = _parse_utc(row["trial_ends_at"])
    if trial_until and trial_until > now:
        return _active_state("trial", trial_until, "trial")

    expired_dates = [
        value
        for value in (trial_until, subscription_until)
        if value is not None
    ]
    latest_expiry = max(expired_dates) if expired_dates else None
    return AccessState(
        status="expired",
        has_access=False,
        access_until=(
            latest_expiry.isoformat()
            if latest_expiry
            else None
        ),
    )
