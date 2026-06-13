"""Live model provider seam."""

from __future__ import annotations

import os
from typing import Protocol

from ultron.ui.generator import LiveModelUnavailable


class ModelProvider(Protocol):
    def complete(self, prompt: str, schema_hint: str | None) -> str: ...


class HttpModelProvider:
    """OpenAI-compatible chat completions provider, configured entirely by env."""

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, model_name: str | None = None) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model_name = model_name

    @property
    def provider_id(self) -> str:
        return "http-openai-compatible"

    def complete(self, prompt: str, schema_hint: str | None) -> str:
        base_url = self.base_url or os.getenv("ULTRON_MODEL_BASE_URL")
        api_key = self.api_key or os.getenv("ULTRON_MODEL_API_KEY")
        model_name = self.model_name or os.getenv("ULTRON_MODEL_NAME")
        if not base_url or not api_key or not model_name:
            raise LiveModelUnavailable("live model provider requires ULTRON_MODEL_BASE_URL, ULTRON_MODEL_API_KEY, and ULTRON_MODEL_NAME")
        try:
            import httpx
        except ImportError as exc:
            raise LiveModelUnavailable("httpx is required for live model provider") from exc

        url = base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        payload = {
            "model": model_name,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Return only JSON matching the requested schema. Do not include markdown fences."},
                {"role": "user", "content": prompt if schema_hint is None else f"{prompt}\n\nSchema hint:\n{schema_hint}"},
            ],
        }
        response = httpx.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"]["content"])
