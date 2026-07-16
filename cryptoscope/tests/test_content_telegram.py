import httpx
import pytest

from app.content.telegram import TelegramPublisher


def test_telegram_error_does_not_expose_bot_token():
    token = "123456:super-secret-token"
    request = httpx.Request(
        "POST",
        f"https://api.telegram.org/bot{token}/sendPhoto",
    )
    response = httpx.Response(
        403,
        json={
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot is not a member of the channel chat",
        },
        request=request,
    )

    with pytest.raises(RuntimeError) as exc_info:
        TelegramPublisher._result(response)

    message = str(exc_info.value)
    assert "bot is not a member" in message
    assert "administrator" in message
    assert token not in message


def test_telegram_success_returns_result():
    request = httpx.Request("POST", "https://api.telegram.org/botredacted/sendMessage")
    response = httpx.Response(
        200,
        json={"ok": True, "result": {"message_id": 77}},
        request=request,
    )

    assert TelegramPublisher._result(response) == {"message_id": 77}
