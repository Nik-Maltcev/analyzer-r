"""Payment callbacks and customer return pages."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import md5
from secrets import compare_digest
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.config import get_settings
from app.db.database import get_connection
from app.product import get_product_profile
from app.ui.templates import templates

router = APIRouter(tags=["payments"])


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
            return _xml_response(parameters, "402", integrity_code)

        operation_id = parameters.get("MNT_OPERATION_ID", "")
        if not operation_id:
            raise ValueError("Missing payment operation")
        amount = normalize_amount(parameters["MNT_AMOUNT"])
        test_mode = 1 if parameters.get("MNT_TEST_MODE") == "1" else 0
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO payment_notifications (
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
            await conn.commit()
    except (UnicodeDecodeError, ValueError):
        return PlainTextResponse("FAIL", status_code=400)

    return PlainTextResponse("SUCCESS")


@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request):
    return templates.TemplateResponse(
        request,
        "payment_success.html",
        {"request": request},
    )
