"""Payment callbacks and customer return pages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import md5
from secrets import compare_digest
from urllib.parse import parse_qsl, urlencode
from uuid import uuid4
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from app.access import get_access_state
from app.auth import get_current_user
from app.config import get_settings
from app.db.database import get_connection
from app.product import get_product_profile
from app.ui.templates import templates

router = APIRouter(tags=["payments"])
PAYANYWAY_ASSISTANT_URL = "https://www.payanyway.ru/assistant.htm"
PAYMENT_PLANS = {
    "month": {"amount": "990.00", "days": 30},
    "year": {"amount": "7900.00", "days": 365},
}


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
        SELECT transaction_id, user_id, plan, amount, currency,
               status, provider_operation_id, test_mode
        FROM payment_orders
        WHERE transaction_id = ?
        LIMIT 1
        """,
        (transaction_id,),
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
    if amount != str(order["amount"]):
        raise ValueError("Unexpected payment amount")
    if parameters["MNT_CURRENCY_CODE"] != str(order["currency"]):
        raise ValueError("Unexpected payment currency")
    if parameters.get("MNT_SUBSCRIBER_ID", "") != str(order["user_id"]):
        raise ValueError("Unexpected payment subscriber")
    if test_mode != int(order["test_mode"]):
        raise ValueError("Unexpected payment mode")


async def _activate_subscription(
    conn,
    order,
    operation_id: str,
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
    base = max(
        value
        for value in (now, current_until, trial_until)
        if value is not None
    )
    access_until = base + timedelta(days=int(plan_config["days"]))

    await conn.execute(
        """
        INSERT INTO user_subscriptions (
            user_id, plan, status, access_until, provider,
            last_transaction_id, updated_at
        ) VALUES (?, ?, 'active', ?, 'payanyway', ?, datetime('now'))
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


@router.get("/api/payments/payanyway/checkout")
async def payanyway_checkout(request: Request, plan: str):
    settings = get_settings()
    if get_product_profile(settings).variant != "global":
        raise HTTPException(status_code=404, detail="Payment is not available")
    if plan not in PAYMENT_PLANS:
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
    parameters = {
        "MNT_ID": account_id,
        "MNT_TRANSACTION_ID": transaction_id,
        "MNT_AMOUNT": str(plan_config["amount"]),
        "MNT_CURRENCY_CODE": "RUB",
        "MNT_SUBSCRIBER_ID": user.id,
        "MNT_TEST_MODE": test_mode,
        "MNT_DESCRIPTION": (
            "MEANX subscription: 1 month"
            if plan == "month"
            else "MEANX subscription: 1 year"
        ),
        "MNT_SUCCESS_URL": f"{base_url}/payment/success",
        "MNT_FAIL_URL": f"{base_url}/app?payment=failed",
        "MNT_RETURN_URL": f"{base_url}/#pricing",
    }
    parameters["MNT_SIGNATURE"] = payanyway_checkout_signature(
        parameters,
        integrity_code,
    )

    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO payment_orders (
                transaction_id, user_id, plan, amount, currency, test_mode
            ) VALUES (?, ?, ?, ?, 'RUB', ?)
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
    except (UnicodeDecodeError, ValueError):
        return PlainTextResponse("FAIL", status_code=400)

    return PlainTextResponse("SUCCESS")


@router.get("/api/payments/status")
async def payment_status(request: Request):
    user = await get_current_user(request)
    if user is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )
    return (await get_access_state(user)).as_dict()


@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request):
    user = await get_current_user(request)
    access = (await get_access_state(user)).as_dict()
    transaction_id = request.query_params.get("MNT_TRANSACTION_ID", "")
    return templates.TemplateResponse(
        request,
        "payment_success.html",
        {
            "request": request,
            "access": access,
            "payment_confirmed": bool(
                transaction_id
                and access.get("last_transaction_id") == transaction_id
            ),
        },
    )
