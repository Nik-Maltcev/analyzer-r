"""External content providers used by the publishing workflow."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        text_model: str = "",
        image_model: str = "",
        timeout: float = 90.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.text_model = text_model.strip()
        self.image_model = image_model.strip()
        self.timeout = timeout
        self.last_response: dict[str, Any] | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://www.meanx.pro",
            "X-Title": "MEANX Content Automation",
        }

    def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("OpenRouter API key is not configured")
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=self.headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        self.last_response = data
        return data

    def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        if not self.text_model:
            raise RuntimeError("OpenRouter text model is not configured")
        data = self._chat({
            "model": self.text_model,
            "temperature": 0.35,
            "max_tokens": 700,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        })
        content = data["choices"][0]["message"].get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        text = str(content).strip()
        if not text:
            raise RuntimeError("OpenRouter returned an empty text response")
        return text

    def generate_background(self, prompt: str) -> bytes | None:
        if not self.api_key or not self.image_model:
            return None
        data = self._chat({
            "model": self.image_model,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": prompt}],
        })
        message = data.get("choices", [{}])[0].get("message", {})
        candidates: list[Any] = []
        candidates.extend(message.get("images") or [])
        content = message.get("content")
        if isinstance(content, list):
            candidates.extend(content)

        for item in candidates:
            if isinstance(item, str):
                value = item
            elif isinstance(item, dict):
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    value = image_url.get("url", "")
                else:
                    value = image_url or item.get("url") or item.get("data") or ""
            else:
                continue
            if not isinstance(value, str) or not value:
                continue
            if value.startswith("data:image") and "," in value:
                return base64.b64decode(value.split(",", 1)[1])
            if value.startswith("http://") or value.startswith("https://"):
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(value)
                    response.raise_for_status()
                    return response.content
        return None

    def response_json(self) -> str | None:
        if self.last_response is None:
            return None
        return json.dumps(self.last_response, ensure_ascii=False)[:20000]
