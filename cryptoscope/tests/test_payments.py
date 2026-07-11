"""PayAnyWay notification verification tests."""

from contextlib import closing
from datetime import UTC, datetime
from hashlib import md5
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.payments import (
    payanyway_checkout_signature,
    payanyway_notification_signature,
    payanyway_response_signature,
)
from app.auth import SESSION_COOKIE_NAME, hash_auth_token
from app.config import get_settings
from app.db.database import get_sync_connection, set_db_path


@pytest.fixture
def app(temp_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_variant", "global")
    monkeypatch.setattr(settings, "app_base_url", "https://www.meanx.pro")
    monkeypatch.setattr(settings, "resend_api_key", "re_test")
    monkeypatch.setattr(settings, "auth_legacy_owner_email", "")
    monkeypatch.setattr(settings, "payanyway_account_id", "12345678")
    monkeypatch.setattr(settings, "payanyway_integrity_code", "test-secret")
    monkeypatch.setattr(settings, "payanyway_test_mode", True)
    set_db_path(temp_db)

    from app.main import app

    return app


def _notification(signature="valid", **overrides):
    parameters = {
        "MNT_ID": "12345678",
        "MNT_TRANSACTION_ID": "order-1",
        "MNT_OPERATION_ID": "operation-1",
        "MNT_AMOUNT": "990.00",
        "MNT_CURRENCY_CODE": "RUB",
        "MNT_SUBSCRIBER_ID": "user-1",
        "MNT_TEST_MODE": "1",
    }
    parameters.update(overrides)
    signed = "".join(parameters.values()) + "test-secret"
    parameters["MNT_SIGNATURE"] = (
        md5(signed.encode()).hexdigest()
        if signature == "valid"
        else "invalid"
    )
    return parameters


def _seed_order(temp_db, plan="month", amount="990.00"):
    with closing(get_sync_connection(temp_db)) as conn:
        conn.execute(
            """
            INSERT INTO auth_users (
                id, email, trial_started_at, trial_ends_at
            ) VALUES (
                'user-1', 'user@example.com', datetime('now'),
                datetime('now', '+3 days')
            )
            """
        )
        conn.execute(
            """
            INSERT INTO payment_orders (
                transaction_id, user_id, plan, amount, currency, test_mode
            ) VALUES ('order-1', 'user-1', ?, ?, 'RUB', 1)
            """,
            (plan, amount),
        )
        conn.commit()


def _seed_session(temp_db):
    session_token = "paypal-test-session"
    with closing(get_sync_connection(temp_db)) as conn:
        conn.execute(
            """
            INSERT INTO auth_users (
                id, email, trial_started_at, trial_ends_at
            ) VALUES (
                'paypal-user', 'paypal@example.com', datetime('now'),
                datetime('now', '+3 days')
            )
            """
        )
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES (?, 'paypal-user', datetime('now', '+1 day'))
            """,
            (hash_auth_token(session_token),),
        )
        conn.commit()
    return session_token


def test_payanyway_signatures_match_official_examples():
    parameters = {
        "MNT_ID": "54600817",
        "MNT_TRANSACTION_ID": "FF790ABCD",
        "MNT_OPERATION_ID": "123456",
        "MNT_AMOUNT": "120.25",
        "MNT_CURRENCY_CODE": "RUB",
        "MNT_TEST_MODE": "0",
    }

    assert payanyway_notification_signature(
        parameters,
        "QWERTY",
    ) == "69bdf9bd91820b8f7b4c4b25d3d22dfa"
    assert payanyway_response_signature(
        "200",
        "54600817",
        "FF790ABCD",
        "QWERTY",
    ) == "29807c8e5d82198b5c4360e6ec711cce"
    assert payanyway_checkout_signature(
        {
            "MNT_ID": "54600817",
            "MNT_TRANSACTION_ID": "FF790ABCD",
            "MNT_AMOUNT": "120.25",
            "MNT_CURRENCY_CODE": "RUB",
            "MNT_TEST_MODE": "0",
        },
        "QWERTY",
    ) == "c8222aef6362c7f1239ccdc729d1a200"


@pytest.mark.asyncio
async def test_checkout_redirects_anonymous_user_to_login(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/payments/payanyway/checkout?plan=month"
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/app?checkout=month&payment=login"
    )


@pytest.mark.asyncio
async def test_checkout_creates_user_bound_signed_order(app, temp_db):
    session_token = "test-session"
    with closing(get_sync_connection(temp_db)) as conn:
        conn.execute(
            """
            INSERT INTO auth_users (
                id, email, trial_started_at, trial_ends_at
            ) VALUES (
                'user-1', 'user@example.com', datetime('now'),
                datetime('now', '+3 days')
            )
            """
        )
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES (?, 'user-1', datetime('now', '+1 day'))
            """,
            (hash_auth_token(session_token),),
        )
        conn.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
        cookies={SESSION_COOKIE_NAME: session_token},
    ) as client:
        response = await client.get(
            "/api/payments/payanyway/checkout?plan=month"
        )

    assert response.status_code == 303
    redirect = urlsplit(response.headers["location"])
    parameters = {
        key: values[0]
        for key, values in parse_qs(redirect.query).items()
    }
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == (
        "https://www.payanyway.ru/assistant.htm"
    )
    assert parameters["MNT_AMOUNT"] == "990.00"
    assert parameters["MNT_SUBSCRIBER_ID"] == "user-1"
    assert parameters["MNT_SUCCESS_URL"].startswith(
        "https://www.meanx.pro/payment/success?transaction_id=meanx-"
    )
    assert parameters["MNT_INPROGRESS_URL"] == parameters["MNT_SUCCESS_URL"]
    assert parameters["paymentSystem.unitId"] == "card"
    assert parameters["MNT_SIGNATURE"] == payanyway_checkout_signature(
        parameters,
        "test-secret",
    )

    with closing(get_sync_connection(temp_db)) as conn:
        order = conn.execute(
            """
            SELECT user_id, plan, amount, status
            FROM payment_orders
            WHERE transaction_id = ?
            """,
            (parameters["MNT_TRANSACTION_ID"],),
        ).fetchone()
    assert tuple(order) == ("user-1", "month", "990.00", "pending")


@pytest.mark.asyncio
async def test_test_checkout_creates_one_ruble_seven_day_order(
    app,
    temp_db,
):
    session_token = "test-plan-session"
    with closing(get_sync_connection(temp_db)) as conn:
        conn.execute(
            """
            INSERT INTO auth_users (
                id, email, trial_started_at, trial_ends_at
            ) VALUES (
                'user-1', 'user@example.com', datetime('now', '-4 days'),
                datetime('now', '-1 day')
            )
            """
        )
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES (?, 'user-1', datetime('now', '+1 day'))
            """,
            (hash_auth_token(session_token),),
        )
        conn.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
        cookies={SESSION_COOKIE_NAME: session_token},
    ) as client:
        response = await client.get(
            "/api/payments/payanyway/checkout?plan=test7"
        )

    assert response.status_code == 303
    redirect = urlsplit(response.headers["location"])
    parameters = {
        key: values[0]
        for key, values in parse_qs(redirect.query).items()
    }
    assert parameters["MNT_AMOUNT"] == "1.00"
    assert parameters["MNT_DESCRIPTION"] == "MEANX test access: 7 days"
    assert parameters["MNT_SUBSCRIBER_ID"] == "user-1"

    with closing(get_sync_connection(temp_db)) as conn:
        order = conn.execute(
            """
            SELECT user_id, plan, amount, status
            FROM payment_orders
            WHERE transaction_id = ?
            """,
            (parameters["MNT_TRANSACTION_ID"],),
        ).fetchone()
    assert tuple(order) == ("user-1", "test7", "1.00", "pending")


@pytest.mark.asyncio
async def test_test_payment_activates_seven_days(app, temp_db):
    _seed_order(temp_db, plan="test7", amount="1.00")
    session_token = "test-plan-paid-session"
    with closing(get_sync_connection(temp_db)) as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES (?, 'user-1', datetime('now', '+1 day'))
            """,
            (hash_auth_token(session_token),),
        )
        conn.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: session_token},
    ) as client:
        response = await client.post(
            "/api/payments/payanyway/notify",
            data=_notification(MNT_AMOUNT="1.00"),
        )
        status_response = await client.get(
            "/api/payments/status?transaction_id=order-1"
        )
        success_response = await client.get(
            "/payment/success?MNT_TRANSACTION_ID=order-1"
        )
        repeated_checkout = await client.get(
            "/api/payments/payanyway/checkout?plan=test7"
        )

    assert response.status_code == 200
    assert response.text == "SUCCESS"
    assert status_response.status_code == 200
    assert status_response.json()["payment_confirmed"] is True
    assert status_response.json()["transaction_id"] == "order-1"
    assert "Оплата подтверждена" in success_response.text
    assert repeated_checkout.status_code == 409
    with closing(get_sync_connection(temp_db)) as conn:
        subscription = conn.execute(
            """
            SELECT plan, status, access_until
            FROM user_subscriptions
            WHERE user_id = 'user-1'
            """
        ).fetchone()
    assert subscription["plan"] == "test7"
    assert subscription["status"] == "active"
    remaining = (
        datetime.fromisoformat(subscription["access_until"]).replace(tzinfo=UTC)
        - datetime.now(UTC)
    ).days
    assert 6 <= remaining <= 7


@pytest.mark.asyncio
async def test_payanyway_notification_is_verified_and_idempotent(
    app,
    temp_db,
):
    _seed_order(temp_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/payments/payanyway/notify",
            data=_notification(),
        )
        second = await client.post(
            "/api/payments/payanyway/notify",
            data=_notification(),
        )

    assert first.status_code == 200
    assert first.text == "SUCCESS"
    assert second.status_code == 200

    with closing(get_sync_connection(temp_db)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM payment_notifications"
        ).fetchone()[0]
        order = conn.execute(
            """
            SELECT status, provider_operation_id
            FROM payment_orders
            WHERE transaction_id = 'order-1'
            """
        ).fetchone()
        subscription = conn.execute(
            """
            SELECT plan, status, access_until, last_transaction_id
            FROM user_subscriptions
            WHERE user_id = 'user-1'
            """
        ).fetchone()
    assert count == 1
    assert tuple(order) == ("paid", "operation-1")
    assert subscription["plan"] == "month"
    assert subscription["status"] == "active"
    assert subscription["last_transaction_id"] == "order-1"
    remaining = (
        datetime.fromisoformat(subscription["access_until"]).replace(tzinfo=UTC)
        - datetime.now(UTC)
    ).days
    assert 32 <= remaining <= 33


@pytest.mark.asyncio
async def test_payanyway_notification_without_subscriber_activates_order(
    app,
    temp_db,
):
    _seed_order(temp_db)
    parameters = _notification()
    parameters.pop("MNT_SUBSCRIBER_ID")
    parameters.pop("MNT_SIGNATURE")
    parameters["MNT_SIGNATURE"] = payanyway_notification_signature(
        parameters,
        "test-secret",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/payments/payanyway/notify",
            data=parameters,
        )

    assert response.status_code == 200
    assert response.text == "SUCCESS"
    with closing(get_sync_connection(temp_db)) as conn:
        order = conn.execute(
            "SELECT status FROM payment_orders WHERE transaction_id = 'order-1'"
        ).fetchone()
        subscription = conn.execute(
            "SELECT status FROM user_subscriptions WHERE user_id = 'user-1'"
        ).fetchone()
    assert order["status"] == "paid"
    assert subscription["status"] == "active"


@pytest.mark.asyncio
async def test_payanyway_rejects_invalid_signature(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/payments/payanyway/notify",
            data=_notification(signature="invalid"),
        )

    assert response.status_code == 400
    assert response.text == "FAIL"


@pytest.mark.asyncio
async def test_payanyway_rejects_amount_that_does_not_match_order(
    app,
    temp_db,
):
    _seed_order(temp_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/payments/payanyway/notify",
            data=_notification(MNT_AMOUNT="1.00"),
        )

    assert response.status_code == 400
    with closing(get_sync_connection(temp_db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM user_subscriptions"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_payment_success_page(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/payment/success")

    assert response.status_code == 200
    assert "Платёж обрабатывается" in response.text


@pytest.mark.asyncio
async def test_paypal_order_is_bound_captured_and_idempotent(
    app,
    temp_db,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_variant", "br")
    monkeypatch.setattr(settings, "paypal_client_id", "live-client")
    monkeypatch.setattr(settings, "paypal_client_secret", "live-secret")
    monkeypatch.setattr(settings, "paypal_mode", "live")
    session_token = _seed_session(temp_db)
    captured_create = {}
    capture_calls = 0

    async def fake_create_remote_order(**kwargs):
        captured_create.update(kwargs)
        return {"id": "PAYPAL-ORDER-1", "status": "CREATED"}

    async def fake_capture_remote_order(order_id):
        nonlocal capture_calls
        capture_calls += 1
        assert order_id == "PAYPAL-ORDER-1"
        return {
            "id": order_id,
            "status": "COMPLETED",
            "purchase_units": [{
                "payments": {
                    "captures": [{
                        "id": "PAYPAL-CAPTURE-1",
                        "status": "COMPLETED",
                        "amount": {
                            "value": "51.44",
                            "currency_code": "BRL",
                        },
                    }],
                },
            }],
        }

    monkeypatch.setattr(
        "app.api.payments._paypal_create_remote_order",
        fake_create_remote_order,
    )
    monkeypatch.setattr(
        "app.api.payments._paypal_capture_remote_order",
        fake_capture_remote_order,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="https://test",
        cookies={SESSION_COOKIE_NAME: session_token},
    ) as client:
        created = await client.post(
            "/api/payments/paypal/orders",
            json={"plan": "month"},
        )
        captured = await client.post(
            "/api/payments/paypal/orders/PAYPAL-ORDER-1/capture",
        )
        repeated = await client.post(
            "/api/payments/paypal/orders/PAYPAL-ORDER-1/capture",
        )

    assert created.status_code == 200
    assert created.json()["id"] == "PAYPAL-ORDER-1"
    assert captured_create["user_id"] == "paypal-user"
    assert captured_create["amount"] == "51.44"
    assert captured_create["currency"] == "BRL"
    assert captured.status_code == 200
    assert captured.json()["status"] == "COMPLETED"
    assert repeated.status_code == 200
    assert capture_calls == 1

    with closing(get_sync_connection(temp_db)) as conn:
        order = conn.execute(
            """
            SELECT provider, user_id, plan, amount, currency, status,
                   provider_operation_id
            FROM payment_orders
            WHERE transaction_id = 'PAYPAL-ORDER-1'
            """
        ).fetchone()
        subscription = conn.execute(
            """
            SELECT provider, plan, status, last_transaction_id
            FROM user_subscriptions
            WHERE user_id = 'paypal-user'
            """
        ).fetchone()
        notification_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM payment_notifications
            WHERE provider = 'paypal'
            """
        ).fetchone()[0]

    assert tuple(order) == (
        "paypal",
        "paypal-user",
        "month",
        "51.44",
        "BRL",
        "paid",
        "PAYPAL-CAPTURE-1",
    )
    assert tuple(subscription) == (
        "paypal",
        "month",
        "active",
        "PAYPAL-ORDER-1",
    )
    assert notification_count == 1


@pytest.mark.asyncio
async def test_paypal_rejects_capture_amount_mismatch(
    app,
    temp_db,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_variant", "id")
    monkeypatch.setattr(settings, "paypal_client_id", "live-client")
    monkeypatch.setattr(settings, "paypal_client_secret", "live-secret")
    monkeypatch.setattr(settings, "paypal_mode", "live")
    session_token = _seed_session(temp_db)

    async def fake_create_remote_order(**_kwargs):
        return {"id": "PAYPAL-ORDER-BAD", "status": "CREATED"}

    async def fake_capture_remote_order(order_id):
        return {
            "id": order_id,
            "status": "COMPLETED",
            "purchase_units": [{
                "payments": {
                    "captures": [{
                        "id": "PAYPAL-CAPTURE-BAD",
                        "status": "COMPLETED",
                        "amount": {
                            "value": "1.00",
                            "currency_code": "USD",
                        },
                    }],
                },
            }],
        }

    monkeypatch.setattr(
        "app.api.payments._paypal_create_remote_order",
        fake_create_remote_order,
    )
    monkeypatch.setattr(
        "app.api.payments._paypal_capture_remote_order",
        fake_capture_remote_order,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="https://test",
        cookies={SESSION_COOKIE_NAME: session_token},
    ) as client:
        await client.post(
            "/api/payments/paypal/orders",
            json={"plan": "year"},
        )
        response = await client.post(
            "/api/payments/paypal/orders/PAYPAL-ORDER-BAD/capture",
        )

    assert response.status_code == 502
    with closing(get_sync_connection(temp_db)) as conn:
        subscription_count = conn.execute(
            "SELECT COUNT(*) FROM user_subscriptions"
        ).fetchone()[0]
        order_status = conn.execute(
            """
            SELECT status
            FROM payment_orders
            WHERE transaction_id = 'PAYPAL-ORDER-BAD'
            """
        ).fetchone()[0]
    assert subscription_count == 0
    assert order_status == "pending"
