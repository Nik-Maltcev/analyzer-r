"""Small Threads API client for image posts and status replies."""

from __future__ import annotations

import time

import httpx


class ThreadsPublisher:
    def __init__(
        self,
        access_token: str,
        api_version: str = "",
        timeout: float = 45.0,
        media_timeout: float = 60.0,
        media_poll_interval: float = 2.0,
    ) -> None:
        self.access_token = access_token.strip()
        self.api_version = api_version.strip().strip("/")
        self.timeout = timeout
        self.media_timeout = media_timeout
        self.media_poll_interval = media_poll_interval
        suffix = f"/{self.api_version}" if self.api_version else ""
        self.base_url = f"https://graph.threads.net{suffix}"

    @property
    def configured(self) -> bool:
        return bool(self.access_token)

    @staticmethod
    def _short_text(value: str, limit: int = 500) -> str:
        text = value.strip()
        if len(text) <= limit:
            return text
        shortened = text[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;: ")
        return f"{shortened}..."

    @staticmethod
    def _result(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.is_success or payload.get("error"):
            error = payload.get("error") or {}
            message = error.get("message") or "request failed without a JSON description"
            code = error.get("code")
            suffix = f" (code {code})" if code is not None else ""
            raise RuntimeError(
                f"Threads API error {response.status_code}{suffix}: {message}"
            )
        return payload

    def _post(self, path: str, data: dict[str, str]) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/{path.lstrip('/')}",
                params=data,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
        try:
            return self._result(response)
        except RuntimeError as exc:
            raise RuntimeError(f"{path}: {exc}") from None

    def _get(self, path: str, params: dict[str, str]) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/{path.lstrip('/')}",
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
        try:
            return self._result(response)
        except RuntimeError as exc:
            raise RuntimeError(f"{path}: {exc}") from None

    def _wait_for_media(self, container_id: str) -> None:
        deadline = time.monotonic() + self.media_timeout
        last_status = "IN_PROGRESS"
        while time.monotonic() < deadline:
            result = self._get(
                container_id,
                {"fields": "id,status,error_message"},
            )
            last_status = str(result.get("status") or "IN_PROGRESS").upper()
            if last_status == "FINISHED":
                return
            if last_status in {"ERROR", "EXPIRED"}:
                detail = str(result.get("error_message") or "no error description")
                raise RuntimeError(
                    f"Threads media container {last_status.lower()}: {detail}"
                )
            time.sleep(self.media_poll_interval)
        raise RuntimeError(
            f"Threads media container was not ready after {self.media_timeout:g}s "
            f"(last status: {last_status})"
        )

    def _publish_container(self, container_id: str) -> str:
        result = self._post("me/threads_publish", {"creation_id": container_id})
        post_id = str(result.get("id") or "")
        if not post_id:
            raise RuntimeError("Threads API did not return a post id")
        return post_id

    def send_image(
        self,
        image_url: str,
        text: str,
        alt_text: str,
        topic_tag: str = "",
        reply_to_id: str = "",
    ) -> str:
        if not self.configured:
            raise RuntimeError("Threads content channel is not configured")
        params = {
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": self._short_text(text),
            "alt_text": self._short_text(alt_text, 1000),
        }
        if topic_tag.strip():
            params["topic_tag"] = topic_tag.strip()
        if reply_to_id.strip():
            params["reply_to_id"] = reply_to_id.strip()
        container = self._post(
            "me/threads",
            params,
        )
        container_id = str(container.get("id") or "")
        if not container_id:
            raise RuntimeError("Threads API did not return a container id")
        self._wait_for_media(container_id)
        return self._publish_container(container_id)

    def send_reply(
        self,
        text: str,
        reply_to_id: str,
        topic_tag: str = "",
    ) -> str:
        if not self.configured:
            raise RuntimeError("Threads content channel is not configured")
        params = {
            "media_type": "TEXT",
            "text": self._short_text(text),
            "reply_to_id": str(reply_to_id),
        }
        if topic_tag.strip():
            params["topic_tag"] = topic_tag.strip()
        container = self._post(
            "me/threads",
            params,
        )
        container_id = str(container.get("id") or "")
        if not container_id:
            raise RuntimeError("Threads API did not return a reply container id")
        return self._publish_container(container_id)

    def send_text(self, text: str, topic_tag: str = "") -> str:
        if not self.configured:
            raise RuntimeError("Threads content channel is not configured")
        params = {
            "media_type": "TEXT",
            "text": self._short_text(text),
        }
        if topic_tag.strip():
            params["topic_tag"] = topic_tag.strip()
        container = self._post(
            "me/threads",
            params,
        )
        container_id = str(container.get("id") or "")
        if not container_id:
            raise RuntimeError("Threads API did not return a text container id")
        return self._publish_container(container_id)
