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


def test_threads_waits_until_image_container_is_ready(monkeypatch):
    publisher = ThreadsPublisher(
        "token",
        media_timeout=1,
        media_poll_interval=0,
    )
    responses = iter(
        [
            {"id": "container-123", "status": "IN_PROGRESS"},
            {"id": "container-123", "status": "FINISHED"},
        ]
    )
    monkeypatch.setattr(publisher, "_get", lambda *_args, **_kwargs: next(responses))

    publisher._wait_for_media("container-123")


def test_threads_reports_image_processing_error(monkeypatch):
    publisher = ThreadsPublisher("token", media_timeout=1, media_poll_interval=0)
    monkeypatch.setattr(
        publisher,
        "_get",
        lambda *_args, **_kwargs: {
            "id": "container-123",
            "status": "ERROR",
            "error_message": "Image URL returned HTTP 404",
        },
    )

    with pytest.raises(RuntimeError, match="Image URL returned HTTP 404"):
        publisher._wait_for_media("container-123")


def test_threads_image_container_includes_topic(monkeypatch):
    publisher = ThreadsPublisher("token")
    calls = []

    def fake_post(path, params):
        calls.append((path, params))
        return {"id": "post-123" if path.endswith("threads_publish") else "container-123"}

    monkeypatch.setattr(publisher, "_post", fake_post)
    monkeypatch.setattr(publisher, "_wait_for_media", lambda *_args: None)

    post_id = publisher.send_image(
        "https://example.com/card.jpg",
        "Signal text",
        "Signal card",
        "Криптовалюты",
    )

    assert post_id == "post-123"
    assert calls[0][1]["topic_tag"] == "Криптовалюты"


def test_threads_image_can_reply_to_original_post(monkeypatch):
    publisher = ThreadsPublisher("token")
    calls = []

    def fake_post(path, params):
        calls.append((path, params))
        return {"id": "post-123" if path.endswith("threads_publish") else "container-123"}

    monkeypatch.setattr(publisher, "_post", fake_post)
    monkeypatch.setattr(publisher, "_wait_for_media", lambda *_args: None)

    publisher.send_image(
        "https://example.com/update.jpg",
        "Update text",
        "Update card",
        "Криптовалюты",
        "original-post-456",
    )

    assert calls[0][1]["reply_to_id"] == "original-post-456"
