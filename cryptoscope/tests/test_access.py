"""Server-side trial and subscription access tests."""

from contextlib import closing

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import SESSION_COOKIE_NAME, hash_auth_token
from app.config import get_settings
from app.db.database import get_sync_connection, set_db_path


@pytest.fixture
def app(temp_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_variant", "global")
    monkeypatch.setattr(settings, "resend_api_key", "re_test")
    monkeypatch.setattr(settings, "auth_legacy_owner_email", "")
    set_db_path(temp_db)

    from app.main import app

    return app


def _add_user(temp_db, trial_modifier):
    token = f"session-{trial_modifier}"
    with closing(get_sync_connection(temp_db)) as conn:
        conn.execute(
            """
            INSERT INTO auth_users (
                id, email, trial_started_at, trial_ends_at
            ) VALUES (
                'user-1', 'user@example.com', datetime('now'),
                datetime('now', ?)
            )
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


@pytest.mark.asyncio
async def test_anonymous_api_request_is_blocked(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/signals?market=crypto")

    assert response.status_code == 401
    assert response.json()["access"]["status"] == "unauthenticated"


@pytest.mark.asyncio
async def test_active_trial_can_use_api(app, temp_db):
    token = _add_user(temp_db, "+3 days")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as client:
        response = await client.get("/api/signals?market=crypto")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_global_admin_can_open_every_market(
    app,
    temp_db,
    monkeypatch,
):
    monkeypatch.setattr(
        get_settings(),
        "auth_legacy_owner_email",
        "user@example.com",
    )
    token = _add_user(temp_db, "+3 days")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as client:
        terminal = await client.get("/app?market=br")
        indonesia = await client.get("/api/signals?market=id")

    assert terminal.status_code == 200
    assert 'window.CRYPTOSCOPE_INITIAL_MARKET = "br"' in terminal.text
    assert 'data-market="br"' in terminal.text
    assert 'data-market="id"' in terminal.text
    assert 'data-market="au"' in terminal.text
    assert 'data-market="ca"' in terminal.text
    assert 'data-market="my"' in terminal.text
    assert 'data-market="za"' in terminal.text
    assert indonesia.status_code == 200


@pytest.mark.asyncio
async def test_regular_global_user_cannot_open_regional_markets(
    app,
    temp_db,
):
    token = _add_user(temp_db, "+3 days")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as client:
        terminal = await client.get("/app?market=br")
        brazil = await client.get("/api/signals?market=br")

    assert terminal.status_code == 200
    assert 'window.CRYPTOSCOPE_INITIAL_MARKET = "crypto"' in terminal.text
    assert 'data-market="br"' not in terminal.text
    assert 'data-market="id"' not in terminal.text
    assert 'data-market="au"' not in terminal.text
    assert 'data-market="ca"' not in terminal.text
    assert 'data-market="my"' not in terminal.text
    assert 'data-market="za"' not in terminal.text
    assert brazil.status_code == 404


@pytest.mark.asyncio
async def test_expired_trial_gets_paywall_for_api_and_tabs(app, temp_db):
    token = _add_user(temp_db, "-1 day")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as client:
        api_response = await client.get("/api/signals?market=crypto")
        tab_response = await client.get("/tab/signals?market=crypto")

    assert api_response.status_code == 402
    assert api_response.json()["access"]["status"] == "expired"
    assert tab_response.status_code == 200
    assert "Продлите доступ" in tab_response.text


@pytest.mark.asyncio
async def test_regional_edition_also_enforces_trial_access(
    app,
    temp_db,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "app_variant", "br")
    token = _add_user(temp_db, "-1 day")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as client:
        response = await client.get("/api/signals?market=br")
        tab_response = await client.get("/tab/signals?market=br")

    assert response.status_code == 402
    assert response.json()["access"]["status"] == "expired"
    assert 'href="/app?checkout=month"' in tab_response.text
    assert 'href="/app?checkout=year"' in tab_response.text
    assert "payanyway" not in tab_response.text.lower()
