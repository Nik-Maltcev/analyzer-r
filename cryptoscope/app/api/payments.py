"""Payment callbacks and customer return pages."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import md5
from secrets import compare_digest
from urllib.parse import parse_qsl, urlencode
from uuid import uuid4
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from pydantic import BaseModel

from app.access import get_access_state
from app.auth import get_current_user
from app.config import get_settings
from app.db.database import get_connection
from app.pricing import get_subscription_pricing
from app.product import get_product_profile
from app.ui.templates import templates

router = APIRouter(tags=["payments"])
logger = logging.getLogger(__name__)
PAYANYWAY_ASSISTANT_URL = "https://www.payanyway.ru/assistant.htm"
PAYMENT_PLANS = {
    "test7": {
        "amount": "1.00",
        "days": 7,
        "description": "MEANX test access: 7 days",
    },
    "month": {
        "amount": "990.00",
        "days": 30,
        "description": "MEANX subscription: 1 month",
    },
    "year": {
        "amount": "7900.00",
        "days": 365,
        "description": "MEANX subscription: 1 year",
    },
}
PAYPAL_PLANS = {"month", "year"}
PUBLIC_PAYANYWAY_PLANS = {"month", "year"}


class PayPalOrderRequest(BaseModel):
    plan: str


def normalize_amount(value: str) -> str:
    try:
        amount = Decimal(value).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid payment amount") from exc
    if amount <= 0:
        raise ValueError("Invalid payment amount")
    return f"{amount:.2f}"


def payanyway_notification_signature(
    parameters: dict[str, str],
    integrity_code: str,
) -> str:
    amount = normalize_amount(parameters.get("MNT_AMOUNT", ""))
    signed = "".join((
        parameters.get("MNT_COMMAND", ""),
        parameters.get("MNT_ID", ""),
        parameters.get("MNT_TRANSACTION_ID", ""),
        parameters.get("MNT_OPERATION_ID", ""),
        amount,
        parameters.get("MNT_CURRENCY_CODE", ""),
        parameters.get("MNT_SUBSCRIBER_ID", ""),
        parameters.get("MNT_TEST_MODE", "0"),
        integrity_code,
    ))
    return md5(signed.encode("utf-8")).hexdigest()


def payanyway_checkout_signature(
    parameters: dict[str, str],
    integrity_code: str,
) -> str:
    amount = normalize_amount(parameters["MNT_AMOUNT"])
    signed = "".join((
        parameters["MNT_ID"],
        parameters["MNT_TRANSACTION_ID"],
        amount,
        parameters["MNT_CURRENCY_CODE"],
        parameters.get("MNT_SUBSCRIBER_ID", ""),
        parameters.get("MNT_TEST_MODE", "0"),
        integrity_code,
    ))
    return md5(signed.encode("utf-8")).hexdigest()


def payanyway_response_signature(
    result_code: str,
    account_id: str,
    transaction_id: str,
    integrity_code: str,
) -> str:
    signed = f"{result_code}{account_id}{transaction_id}{integrity_code}"
    return md5(signed.encode("utf-8")).hexdigest()


async def _request_parameters(request: Request) -> dict[str, str]:
    parameters = dict(request.query_params)
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", errors="strict")
        parameters.update(dict(parse_qsl(body, keep_blank_values=True)))
    return {key: str(value) for key, value in parameters.items()}


def _public_base_url(request: Request) -> str:
    settings = get_settings()
    if settings.app_base_url:
        return settings.app_base_url.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get(
        "x-forwarded-host",
        request.headers.get("host", ""),
    )
    return f"{scheme}://{host}".rstrip("/")


def _utc_sql(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _xml_response(
    parameters: dict[str, str],
    result_code: str,
    integrity_code: str,
) -> PlainTextResponse:
    account_id = parameters["MNT_ID"]
    transaction_id = parameters["MNT_TRANSACTION_ID"]
    signature = payanyway_response_signature(
        result_code,
        account_id,
        transaction_id,
        integrity_code,
    )
    amount = normalize_amount(parameters["MNT_AMOUNT"])
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<MNT_RESPONSE>"
        f"<MNT_ID>{escape(account_id)}</MNT_ID>"
        f"<MNT_TRANSACTION_ID>{escape(transaction_id)}</MNT_TRANSACTION_ID>"
        f"<MNT_RESULT_CODE>{result_code}</MNT_RESULT_CODE>"
        "<MNT_DESCRIPTION>Order is ready for payment</MNT_DESCRIPTION>"
        f"<MNT_AMOUNT>{amount}</MNT_AMOUNT>"
        f"<MNT_SIGNATURE>{signature}</MNT_SIGNATURE>"
        "</MNT_RESPONSE>"
    )
    return PlainTextResponse(content, media_type="application/xml")


async def _load_order(conn, transaction_id: str):
    cursor = await conn.execute(
        """
        SELECT transaction_id, provider, user_id, plan, amount, currency,
               status, provider_operation_id, test_mode
        FROM payment_orders
        WHERE transaction_id = ?
        LIMIT 1
        """,
        (transaction_id,),
    )
    return await cursor.fetchone()


async def _load_user_order_status(
    conn,
    transaction_id: str,
    user_id: str,
):
    cursor = await conn.execute(
        """
        SELECT orders.transaction_id, orders.plan, orders.status,
               subscriptions.access_until
        FROM payment_orders AS orders
        LEFT JOIN user_subscriptions AS subscriptions
          ON subscriptions.user_id = orders.user_id
        WHERE orders.transaction_id = ? AND orders.user_id = ?
        LIMIT 1
        """,
        (transaction_id, user_id),
    )
    return await cursor.fetchone()


def _validate_order_notification(
    order,
    parameters: dict[str, str],
    amount: str,
    test_mode: int,
) -> None:
    if not order:
        raise ValueError("Unknown payment order")
    if str(order["provider"]) != "payanyway":
        raise ValueError("Unexpected payment provider")
    if amount != str(order["amount"]):
        raise ValueError("Unexpected payment amount")
    if parameters["MNT_CURRENCY_CODE"] != str(order["currency"]):
        raise ValueError("Unexpected payment currency")
    subscriber_id = parameters.get("MNT_SUBSCRIBER_ID", "")
    if subscriber_id and subscriber_id != str(order["user_id"]):
        raise ValueError("Unexpected payment subscriber")
    if test_mode != int(order["test_mode"]):
        raise ValueError("Unexpected payment mode")


async def _activate_subscription(
    conn,
    order,
    operation_id: str,
    provider: str = "payanyway",
) -> str:
    plan = str(order["plan"])
    plan_config = PAYMENT_PLANS.get(plan)
    if not plan_config:
        raise ValueError("Unknown subscription plan")

    cursor = await conn.execute(
        """
        SELECT access_until
        FROM user_subscriptions
        WHERE user_id = ?
        LIMIT 1
        """,
        (order["user_id"],),
    )
    subscription = await cursor.fetchone()
    cursor = await conn.execute(
        """
        SELECT trial_ends_at
        FROM auth_users
        WHERE id = ?
        LIMIT 1
        """,
        (order["user_id"],),
    )
    user_row = await cursor.fetchone()
    now = datetime.now(UTC)
    current_until = (
        _parse_utc(subscription["access_until"])
        if subscription
        else None
    )
    trial_until = (
        _parse_utc(user_row["trial_ends_at"])
        if user_row
        else None
    )
    base = (
        now
        if plan == "test7"
        else max(
            value
            for value in (now, current_until, trial_until)
            if value is not None
        )
    )
    access_until = base + timedelta(days=int(plan_config["days"]))

    await conn.execute(
        """
        INSERT INTO user_subscriptions (
            user_id, plan, status, access_until, provider,
            last_transaction_id, updated_at
        ) VALUES (?, ?, 'active', ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            plan = excluded.plan,
            status = 'active',
            access_until = excluded.access_until,
            provider = excluded.provider,
            last_transaction_id = excluded.last_transaction_id,
            updated_at = datetime('now')
        """,
        (
            order["user_id"],
            plan,
            _utc_sql(access_until),
            provider,
            order["transaction_id"],
        ),
    )
    await conn.execute(
        """
        UPDATE payment_orders
        SET status = 'paid',
            provider_operation_id = ?,
            paid_at = datetime('now')
        WHERE transaction_id = ?
        """,
        (operation_id, order["transaction_id"]),
    )
    return _utc_sql(access_until)


def _paypal_base_url() -> str:
    mode = get_settings().paypal_mode.strip().lower()
    return (
        "https://api-m.paypal.com"
        if mode == "live"
        else "https://api-m.sandbox.paypal.com"
    )


def _paypal_plan(plan: str) -> dict[str, str | int]:
    if plan not in PAYPAL_PLANS:
        raise HTTPException(status_code=404, detail="Unknown payment plan")
    base = PAYMENT_PLANS.get(plan)
    if not base:
        raise HTTPException(status_code=404, detail="Unknown payment plan")
    pricing = get_subscription_pricing()
    amount = (
        pricing.checkout_month_amount
        if plan == "month"
        else pricing.checkout_year_amount
    )
    try:
        amount = normalize_amount(amount)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="PayPal plan amount is not configured",
        ) from exc
    currency = pricing.checkout_currency
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(
            status_code=503,
            detail="PayPal currency is not configured",
        )
    return {
        "amount": amount,
        "currency": currency,
        "days": int(base["days"]),
    }


def _require_paypal_settings():
    settings = get_settings()
    if (
        not settings.paypal_client_id.strip()
        or not settings.paypal_client_secret.strip()
    ):
        raise HTTPException(
            status_code=503,
            detail="PayPal is not configured",
        )
    if settings.paypal_mode.strip().lower() not in {"sandbox", "live"}:
        raise HTTPException(
            status_code=503,
            detail="PayPal mode is not configured",
        )
    return settings


def _paypal_api_error(response: httpx.Response) -> HTTPException:
    message = "PayPal request failed"
    try:
        payload = response.json()
        details = payload.get("details") or []
        if details:
            issue = details[0].get("issue") or ""
            description = details[0].get("description") or ""
            message = " · ".join(
                value for value in (issue, description) if value
            ) or message
        elif payload.get("message"):
            message = str(payload["message"])
    except (ValueError, TypeError, AttributeError):
        pass
    return HTTPException(status_code=502, detail=message)


async def _paypal_access_token() -> str:
    settings = _require_paypal_settings()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{_paypal_base_url()}/v1/oauth2/token",
                auth=(
                    settings.paypal_client_id.strip(),
                    settings.paypal_client_secret.strip(),
                ),
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "en_US",
                },
                data={"grant_type": "client_credentials"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="PayPal is temporarily unavailable",
        ) from exc
    if response.status_code != 200:
        raise _paypal_api_error(response)
    token = str(response.json().get("access_token") or "")
    if not token:
        raise HTTPException(
            status_code=502,
            detail="PayPal authentication failed",
        )
    return token


async def _paypal_create_remote_order(
    *,
    plan: str,
    amount: str,
    currency: str,
    user_id: str,
    invoice_id: str,
) -> dict:
    access_token = await _paypal_access_token()
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": invoice_id,
            "custom_id": user_id,
            "invoice_id": invoice_id,
            "description": (
                "MEANX subscription: 1 month"
                if plan == "month"
                else "MEANX subscription: 1 year"
            ),
            "amount": {
                "currency_code": currency,
                "value": amount,
            },
        }],
        "application_context": {
            "brand_name": "MEANX",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                f"{_paypal_base_url()}/v2/checkout/orders",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": invoice_id,
                    "Prefer": "return=representation",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="PayPal is temporarily unavailable",
        ) from exc
    if response.status_code != 201:
        raise _paypal_api_error(response)
    return response.json()


async def _paypal_capture_remote_order(order_id: str) -> dict:
    access_token = await _paypal_access_token()
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                (
                    f"{_paypal_base_url()}/v2/checkout/orders/"
                    f"{order_id}/capture"
                ),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": f"capture-{order_id}",
                    "Prefer": "return=representation",
                },
                json={},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="PayPal is temporarily unavailable",
        ) from exc
    if response.status_code not in {200, 201}:
        raise _paypal_api_error(response)
    return response.json()


def _paypal_completed_capture(payload: dict) -> dict | None:
    for purchase_unit in payload.get("purchase_units") or []:
        payments = purchase_unit.get("payments") or {}
        for capture in payments.get("captures") or []:
            if capture.get("status") == "COMPLETED":
                return capture
    return None


@router.get("/api/payments/payanyway/checkout")
async def payanyway_checkout(request: Request, plan: str):
    settings = get_settings()
    if get_product_profile(settings).variant != "global":
        raise HTTPException(status_code=404, detail="Payment is not available")
    if plan not in PUBLIC_PAYANYWAY_PLANS:
        raise HTTPException(status_code=404, detail="Unknown payment plan")

    user = await get_current_user(request)
    if user is None:
        return RedirectResponse(
            url=f"/app?checkout={plan}&payment=login",
            status_code=303,
        )

    account_id = settings.payanyway_account_id.strip()
    integrity_code = settings.payanyway_integrity_code
    if not account_id or not integrity_code:
        raise HTTPException(
            status_code=503,
            detail="PayAnyWay is not configured",
        )

    plan_config = PAYMENT_PLANS[plan]
    transaction_id = f"meanx-{uuid4().hex}"
    test_mode = "1" if settings.payanyway_test_mode else "0"
    base_url = _public_base_url(request)
    status_url = (
        f"{base_url}/payment/success?transaction_id={transaction_id}"
    )
    parameters = {
        "MNT_ID": account_id,
        "MNT_TRANSACTION_ID": transaction_id,
        "MNT_AMOUNT": str(plan_config["amount"]),
        "MNT_CURRENCY_CODE": "RUB",
        "MNT_SUBSCRIBER_ID": user.id,
        "MNT_TEST_MODE": test_mode,
        "MNT_DESCRIPTION": str(plan_config["description"]),
        "MNT_SUCCESS_URL": status_url,
        "MNT_INPROGRESS_URL": status_url,
        "MNT_FAIL_URL": f"{base_url}/app?payment=failed",
        "MNT_RETURN_URL": f"{base_url}/#pricing",
        "paymentSystem.unitId": "card",
    }
    parameters["MNT_SIGNATURE"] = payanyway_checkout_signature(
        parameters,
        integrity_code,
    )

    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO payment_orders (
                transaction_id, provider, user_id, plan, amount,
                currency, test_mode
            ) VALUES (?, 'payanyway', ?, ?, ?, 'RUB', ?)
            """,
            (
                transaction_id,
                user.id,
                plan,
                plan_config["amount"],
                int(test_mode),
            ),
        )
        await conn.commit()

    return RedirectResponse(
        url=f"{PAYANYWAY_ASSISTANT_URL}?{urlencode(parameters)}",
        status_code=303,
    )


@router.api_route(
    "/api/payments/payanyway/notify",
    methods=["GET", "POST"],
    response_class=PlainTextResponse,
)
async def payanyway_notify(request: Request):
    settings = get_settings()
    account_id = settings.payanyway_account_id.strip()
    integrity_code = settings.payanyway_integrity_code
    if (
        get_product_profile(settings).variant != "global"
        or not account_id
        or not integrity_code
    ):
        return PlainTextResponse("FAIL", status_code=503)

    parameters: dict[str, str] = {}
    try:
        parameters = await _request_parameters(request)
        required = (
            "MNT_ID",
            "MNT_TRANSACTION_ID",
            "MNT_AMOUNT",
            "MNT_CURRENCY_CODE",
            "MNT_SIGNATURE",
        )
        if any(not parameters.get(field) for field in required):
            raise ValueError("Missing payment parameter")
        if parameters["MNT_ID"] != account_id:
            raise ValueError("Unexpected payment account")

        expected_signature = payanyway_notification_signature(
            parameters,
            integrity_code,
        )
        if not compare_digest(
            expected_signature,
            parameters["MNT_SIGNATURE"].lower(),
        ):
            raise ValueError("Invalid payment signature")

        if parameters.get("MNT_COMMAND") == "CHECK":
            async with get_connection() as conn:
                order = await _load_order(
                    conn,
                    parameters["MNT_TRANSACTION_ID"],
                )
            amount = normalize_amount(parameters["MNT_AMOUNT"])
            test_mode = (
                1 if parameters.get("MNT_TEST_MODE") == "1" else 0
            )
            _validate_order_notification(
                order,
                parameters,
                amount,
                test_mode,
            )
            result_code = "200" if order["status"] == "paid" else "402"
            return _xml_response(parameters, result_code, integrity_code)

        operation_id = parameters.get("MNT_OPERATION_ID", "")
        if not operation_id:
            raise ValueError("Missing payment operation")
        amount = normalize_amount(parameters["MNT_AMOUNT"])
        test_mode = 1 if parameters.get("MNT_TEST_MODE") == "1" else 0
        async with get_connection() as conn:
            order = await _load_order(
                conn,
                parameters["MNT_TRANSACTION_ID"],
            )
            _validate_order_notification(
                order,
                parameters,
                amount,
                test_mode,
            )
            cursor = await conn.execute(
                """
                SELECT id
                FROM payment_notifications
                WHERE provider = 'payanyway' AND operation_id = ?
                LIMIT 1
                """,
                (operation_id,),
            )
            if await cursor.fetchone():
                return PlainTextResponse("SUCCESS")
            if order["status"] == "paid":
                raise ValueError("Payment order is already paid")

            await conn.execute(
                """
                INSERT INTO payment_notifications (
                    transaction_id, operation_id, account_id, amount,
                    currency, subscriber_id, test_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parameters["MNT_TRANSACTION_ID"],
                    operation_id,
                    account_id,
                    amount,
                    parameters["MNT_CURRENCY_CODE"],
                    parameters.get("MNT_SUBSCRIBER_ID") or None,
                    test_mode,
                ),
            )
            await _activate_subscription(conn, order, operation_id)
            await conn.commit()
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning(
            "PayAnyWay notification rejected transaction=%s operation=%s: %s",
            parameters.get("MNT_TRANSACTION_ID", ""),
            parameters.get("MNT_OPERATION_ID", ""),
            exc,
        )
        return PlainTextResponse("FAIL", status_code=400)

    return PlainTextResponse("SUCCESS")


@router.post("/api/payments/paypal/orders")
async def paypal_create_order(
    payload: PayPalOrderRequest,
    request: Request,
):
    settings = get_settings()
    profile = get_product_profile(settings)
    if profile.variant not in {"br", "id"}:
        raise HTTPException(status_code=404, detail="Payment is not available")
    settings = _require_paypal_settings()

    user = await get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )

    plan = payload.plan.strip().lower()
    plan_config = _paypal_plan(plan)
    invoice_id = f"meanx-{profile.variant}-{uuid4().hex}"
    remote_order = await _paypal_create_remote_order(
        plan=plan,
        amount=str(plan_config["amount"]),
        currency=str(plan_config["currency"]),
        user_id=user.id,
        invoice_id=invoice_id,
    )
    order_id = str(remote_order.get("id") or "")
    if not order_id or remote_order.get("status") != "CREATED":
        raise HTTPException(
            status_code=502,
            detail="PayPal did not create an order",
        )

    test_mode = 0 if settings.paypal_mode.strip().lower() == "live" else 1
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO payment_orders (
                transaction_id, provider, user_id, plan, amount,
                currency, test_mode
            ) VALUES (?, 'paypal', ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                user.id,
                plan,
                plan_config["amount"],
                plan_config["currency"],
                test_mode,
            ),
        )
        await conn.commit()

    return {
        "id": order_id,
        "status": remote_order.get("status"),
        "plan": plan,
        "amount": plan_config["amount"],
        "currency": plan_config["currency"],
    }


@router.post("/api/payments/paypal/orders/{order_id}/capture")
async def paypal_capture_order(
    order_id: str,
    request: Request,
):
    settings = get_settings()
    if get_product_profile(settings).variant not in {"br", "id"}:
        raise HTTPException(status_code=404, detail="Payment is not available")
    _require_paypal_settings()
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )

    async with get_connection() as conn:
        order = await _load_order(conn, order_id)
    if (
        not order
        or str(order["provider"]) != "paypal"
        or str(order["user_id"]) != user.id
    ):
        raise HTTPException(status_code=404, detail="Payment order not found")
    if str(order["status"]) == "paid":
        return {
            "status": "COMPLETED",
            "order_id": order_id,
            "capture_id": order["provider_operation_id"],
            "access_until": (
                await get_access_state(user)
            ).access_until,
        }

    remote_order = await _paypal_capture_remote_order(order_id)
    if (
        str(remote_order.get("id") or "") != order_id
        or remote_order.get("status") != "COMPLETED"
    ):
        raise HTTPException(
            status_code=502,
            detail="PayPal payment is not completed",
        )
    capture = _paypal_completed_capture(remote_order)
    if not capture:
        raise HTTPException(
            status_code=502,
            detail="PayPal capture is not completed",
        )

    capture_id = str(capture.get("id") or "")
    capture_amount = capture.get("amount") or {}
    try:
        amount = normalize_amount(str(capture_amount.get("value") or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="PayPal returned an invalid amount",
        ) from exc
    currency = str(capture_amount.get("currency_code") or "").upper()
    if (
        not capture_id
        or amount != str(order["amount"])
        or currency != str(order["currency"])
    ):
        raise HTTPException(
            status_code=502,
            detail="PayPal payment details do not match the order",
        )

    async with get_connection() as conn:
        current_order = await _load_order(conn, order_id)
        if current_order["status"] == "paid":
            access_until = (
                await get_access_state(user)
            ).access_until
        else:
            await conn.execute(
                """
                INSERT INTO payment_notifications (
                    provider, transaction_id, operation_id, account_id,
                    amount, currency, subscriber_id, test_mode
                ) VALUES (
                    'paypal', ?, ?, 'paypal', ?, ?, ?, ?
                )
                """,
                (
                    order_id,
                    capture_id,
                    amount,
                    currency,
                    user.id,
                    int(order["test_mode"]),
                ),
            )
            access_until = await _activate_subscription(
                conn,
                current_order,
                capture_id,
                provider="paypal",
            )
            await conn.commit()

    return {
        "status": "COMPLETED",
        "order_id": order_id,
        "capture_id": capture_id,
        "access_until": access_until,
    }


@router.get("/api/payments/status")
async def payment_status(request: Request):
    user = await get_current_user(request)
    if user is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )
    access = (await get_access_state(user)).as_dict()
    transaction_id = request.query_params.get("transaction_id", "").strip()
    if not transaction_id:
        return access

    async with get_connection() as conn:
        order = await _load_user_order_status(
            conn,
            transaction_id,
            user.id,
        )
    if not order:
        return JSONResponse(
            status_code=404,
            content={"detail": "Payment order not found"},
        )
    return {
        **access,
        "transaction_id": str(order["transaction_id"]),
        "plan": str(order["plan"]),
        "payment_status": str(order["status"]),
        "payment_confirmed": order["status"] == "paid",
        "subscription_access_until": order["access_until"],
    }


@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request):
    user = await get_current_user(request)
    access = (await get_access_state(user)).as_dict()
    transaction_id = (
        request.query_params.get("transaction_id", "")
        or request.query_params.get("MNT_TRANSACTION_ID", "")
        or request.query_params.get("paypal_order_id", "")
    )
    payment_confirmed = False
    if user is not None and transaction_id:
        async with get_connection() as conn:
            order = await _load_user_order_status(
                conn,
                transaction_id,
                user.id,
            )
        payment_confirmed = bool(order and order["status"] == "paid")
    return templates.TemplateResponse(
        request,
        "payment_success.html",
        {
            "request": request,
            "access": access,
            "payment_confirmed": payment_confirmed,
        },
    )
