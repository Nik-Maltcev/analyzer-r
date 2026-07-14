"""Small Telegram Bot API client for channel publications."""

from __future__ import annotations

import json
from pathlib import Path

import httpx


class TelegramPublisher:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 45.0) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    @staticmethod
    def _result(response: httpx.Response) -> dict:
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram rejected the request: {payload}")
        return payload["result"]

    def send_photo(self, image_path: str | Path, caption: str) -> int:
        if not self.configured:
            raise RuntimeError("Telegram content channel is not configured")
        with Path(image_path).open("rb") as image_file, httpx.Client(
            timeout=self.timeout
        ) as client:
            response = client.post(
                self._url("sendPhoto"),
                data={"chat_id": self.chat_id, "caption": caption[:1024]},
                files={"photo": (Path(image_path).name, image_file, "image/png")},
            )
        return int(self._result(response)["message_id"])

    def send_message(self, text: str, reply_to_message_id: int | None = None) -> int:
        if not self.configured:
            raise RuntimeError("Telegram content channel is not configured")
        data = {"chat_id": self.chat_id, "text": text[:4096]}
        if reply_to_message_id:
            data["reply_parameters"] = json.dumps({
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            })
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self._url("sendMessage"), data=data)
        return int(self._result(response)["message_id"])
