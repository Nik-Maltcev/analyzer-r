import httpx
import pytest

from app.content.threads import ThreadsPublisher


def test_threads_error_does_not_expose_access_token():
    token = "TH-super-secret-token"
    request = httpx.Request(
        "POST",
        "https://graph.threads.net/v1.0/me/threads",
        content=f"access_token={token}",
    )
    response = httpx.Response(
        400,
        json={"error": {"message": "Invalid OAuth access token", "code": 190}},
        request=request,
    )

    with pytest.raises(RuntimeError) as exc_info:
        ThreadsPublisher._result(response)

    message = str(exc_info.value)
    assert "Invalid OAuth access token" in message
    assert "code 190" in message
    assert token not in message


def test_threads_text_is_limited_without_cutting_last_word():
    result = ThreadsPublisher._short_text("word " * 120)

    assert len(result) <= 500
    assert result.endswith("...")


def test_threads_success_returns_payload():
    request = httpx.Request("POST", "https://graph.threads.net/v1.0/me/threads")
    response = httpx.Response(200, json={"id": "container-123"}, request=request)

    assert ThreadsPublisher._result(response) == {"id": "container-123"}
