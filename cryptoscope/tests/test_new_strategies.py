"""Тесты раздела «Новые стратегии» (admin-only, крипта)."""

from __future__ import annotations

from contextlib import closing

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import SESSION_COOKIE_NAME, hash_auth_token
from app.config import get_settings
from app.core.new_strategies import build_new_strategies_report
from app.db.database import get_sync_connection, set_db_path


@pytest.fixture
def app(temp_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_variant", "global")
    monkeypatch.setattr(settings, "resend_api_key", "re_test")
    monkeypatch.setattr(settings, "auth_legacy_owner_email", "")
    monkeypatch.setattr(settings, "auth_admin_emails", "")

    from app.main import app

    # app.main на уровне модуля вызывает set_db_path(settings.db_path); ставим
    # temp_db ПОСЛЕ импорта, чтобы путь не перезаписался при изолированном прогоне.
    set_db_path(temp_db)
    return app


def _add_user(temp_db, trial_modifier="+3 days"):
    token = "session-ns"
    with closing(get_sync_connection(temp_db)) as conn:
        conn.execute(
            """
            INSERT INTO auth_users (id, email, trial_started_at, trial_ends_at)
            VALUES ('user-1', 'user@example.com', datetime('now'), datetime('now', ?))
            """,
            (trial_modifier,),
        )
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES (?, 'user-1', datetime('now', '+1 day'))
            """,
            (hash_auth_token(token),),
        )
        conn.commit()
    return token


# --- Чистая структура отчёта (без HTTP) -------------------------------------

def test_report_structure():
    report = build_new_strategies_report()
    assert report["summary"]["total"] == 2
    assert report["summary"]["positive_ev"] >= 1
    keys = {s["key"] for s in report["strategies"]}
    assert keys == {"xs_momentum", "funding_carry"}
    mom = next(s for s in report["strategies"] if s["key"] == "xs_momentum")
    assert mom["verdict_tone"] == "positive"
    assert mom["capital_table"]["rows"][0][0] == "1x"
    # плечо 3x/5x помечены как ликвидация
    assert mom["capital_table"]["rows"][2][5] == "bad"
    assert mom["capital_table"]["rows"][3][5] == "bad"
    assert "не является" in report["disclaimer"].lower() or "рекомендацией" in report["disclaimer"].lower()


# --- Доступ через HTTP -------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_sees_new_strategies(app, temp_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_legacy_owner_email", "user@example.com")
    token = _add_user(temp_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: token}
    ) as client:
        resp = await client.get("/tab/new-strategies?market=crypto")

    assert resp.status_code == 200
    assert "Новые стратегии" in resp.text
    assert "Кросс-секционный моментум" in resp.text
    assert "BÄCKTEST" not in resp.text  # страховка от опечатки в лейбле
    assert "пережил survivorship" in resp.text.lower() or "survivorship" in resp.text.lower()
    assert "funding" in resp.text.lower()


@pytest.mark.asyncio
async def test_non_admin_gets_404(app, temp_db):
    token = _add_user(temp_db)  # обычный юзер с триалом, не админ
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: token}
    ) as client:
        resp = await client.get("/tab/new-strategies?market=crypto")

    assert resp.status_code == 404
