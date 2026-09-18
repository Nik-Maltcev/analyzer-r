"""Temporary local-dev helper: seed/clean an admin session for browser viewing.

Not part of the app. Safe to delete. Usage:
    python _dev_seed_admin.py seed
    python _dev_seed_admin.py clean
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256

DB = "data/market.db"
USER_ID = "local-dev-admin"
EMAIL = "admin@local.test"
TOKEN = "dev-session-token-local"
TOKEN_HASH = sha256(TOKEN.encode("utf-8")).hexdigest()


def seed() -> None:
    conn = sqlite3.connect(DB)
    try:
        expires = (datetime.now(UTC) + timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute(
            "INSERT OR REPLACE INTO auth_users (id, email) VALUES (?, ?)",
            (USER_ID, EMAIL),
        )
        conn.execute(
            "INSERT OR REPLACE INTO auth_sessions (token_hash, user_id, expires_at)"
            " VALUES (?, ?, ?)",
            (TOKEN_HASH, USER_ID, expires),
        )
        conn.commit()
        print(f"seeded session token={TOKEN} email={EMAIL} expires={expires}")
    finally:
        conn.close()


def clean() -> None:
    conn = sqlite3.connect(DB)
    try:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (TOKEN_HASH,))
        conn.execute("DELETE FROM auth_users WHERE id = ?", (USER_ID,))
        conn.commit()
        print("cleaned dev admin session + user")
    finally:
        conn.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "seed"
    if mode == "clean":
        clean()
    else:
        seed()
