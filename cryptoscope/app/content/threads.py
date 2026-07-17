"""Small Threads API client for image posts and status replies."""

from __future__ import annotations

import httpx


class ThreadsPublisher:
    def __init__(
        self,
        access_token: str,
        api_version: str = "v1.0",
        timeout: float = 45.0,
    ) -> None:
        self.access_token = access_token.strip()
        self.api_version = api_version.strip().strip("/") or "v1.0"
        self.timeout = timeout
        self.base_url = f"https://graph.threads.net/{self.api_version}"

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
                data=data,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
        return self._result(response)

    def _publish_container(self, container_id: str) -> str:
        result = self._post("me/threads_publish", {"creation_id": container_id})
        post_id = str(result.get("id") or "")
        if not post_id:
            raise RuntimeError("Threads API did not return a post id")
        return post_id

    def send_image(self, image_url: str, text: str, alt_text: str) -> str:
        if not self.configured:
            raise RuntimeError("Threads content channel is not configured")
        container = self._post(
            "me/threads",
            {
                "media_type": "IMAGE",
                "image_url": image_url,
                "text": self._short_text(text),
                "alt_text": self._short_text(alt_text, 1000),
            },
        )
        container_id = str(container.get("id") or "")
        if not container_id:
            raise RuntimeError("Threads API did not return a container id")
        return self._publish_container(container_id)

    def send_reply(self, text: str, reply_to_id: str) -> str:
        if not self.configured:
            raise RuntimeError("Threads content channel is not configured")
        container = self._post(
            "me/threads",
            {
                "media_type": "TEXT",
                "text": self._short_text(text),
                "reply_to_id": str(reply_to_id),
            },
        )
        container_id = str(container.get("id") or "")
        if not container_id:
            raise RuntimeError("Threads API did not return a reply container id")
        return self._publish_container(container_id)
