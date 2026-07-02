"""PayAnyWay notification verification tests."""

from hashlib import md5

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.db.database import get_sync_connection, set_db_path
from app.api.payments import (
    payanyway_notification_signature,
    payanyway_response_signature,
)


@pytest.fixture
def app(temp_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_variant", "global")
    monkeypatch.setattr(settings, "payanyway_account_id", "12345678")
    monkeypatch.setattr(settings, "payanyway_integrity_code", "test-secret")
    set_db_path(temp_db)

    from app.main import app

    return app


def _notification(signature="valid"):
    parameters = {
        "MNT_ID": "12345678",
        "MNT_TRANSACTION_ID": "order-1",
        "MNT_OPERATION_ID": "operation-1",
        "MNT_AMOUNT": "990.00",
        "MNT_CURRENCY_CODE": "RUB",
        "MNT_SUBSCRIBER_ID": "user-1",
        "MNT_TEST_MODE": "1",
    }
    signed = "".join(parameters.values()) + "test-secret"
    parameters["MNT_SIGNATURE"] = (
        md5(signed.encode()).hexdigest()
        if signature == "valid"
        else "invalid"
    )
    return parameters


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


@pytest.mark.asyncio
async def test_payanyway_notification_is_verified_and_idempotent(
    app,
    temp_db,
):
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

    with get_sync_connection(temp_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM payment_notifications"
        ).fetchone()[0]
    assert count == 1


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
async def test_payment_success_page(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/payment/success")

    assert response.status_code == 200
    assert "Платёж обрабатывается" in response.text
