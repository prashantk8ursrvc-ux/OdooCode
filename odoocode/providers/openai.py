# forge/providers/openai.py
"""OpenAI-compatible provider (OpenAI, OpenRouter, vLLM, etc.)."""
import os
import time
import json
import logging
from typing import List, Iterator, Optional
import httpx
from .base import Provider, ProviderResponse, Message

logger = logging.getLogger("Forge.providers.openai")

# Provider-specific defaults
PROVIDER_DEFAULTS = {
    "openai": {"base_url": "https://api.openai.com/v1", "env_key": "OPENAI_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY"},
}


class OpenAIProvider(Provider):
    """Works with OpenAI, OpenRouter, and any OpenAI-compatible API."""

    name = "openai"

    def __init__(self, provider_type: str = "openai"):
        defaults = PROVIDER_DEFAULTS.get(provider_type, PROVIDER_DEFAULTS["openai"])
        self.base_url = os.environ.get(f"{provider_type.upper()}_BASE_URL", defaults["base_url"])
        self.api_key = os.environ.get(defaults["env_key"], os.environ.get("OPENAI_API_KEY", ""))
        self._client = httpx.Client(timeout=120.0)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _convert_messages(self, messages: List[Message]) -> List[dict]:
        converted = []
        for m in messages:
            converted.append({"role": m.role, "content": m.content})
        return converted

    def is_available(self) -> bool:
        if not self.api_key:
            logger.warning("OpenAI/OpenRouter API key not set")
            return False
        try:
            resp = self._client.get(
                f"{self.base_url}/models",
                headers=self._headers(),
            )
            return resp.status_code in (200, 401)  # 401 means key needed but API is reachable
        except Exception:
            return False

    def chat(
        self,
        messages: List[Message],
        model: str,
        temperature: float = 0.15,
        max_tokens: int = -1,
        num_ctx: int = 16384,
        **kwargs,
    ) -> ProviderResponse:
        body = {
            "model": model,
            "messages": self._convert_messages(messages),
            "temperature": temperature,
        }
        if max_tokens > 0:
            body["max_tokens"] = max_tokens

        t0 = time.monotonic()
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=body,
        )
        latency = (time.monotonic() - t0) * 1000

        if resp.status_code != 200:
            raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        return ProviderResponse(
            content=content,
            model=data.get("model", model),
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            finish_reason=data["choices"][0].get("finish_reason", ""),
            latency_ms=latency,
        )

    def stream_chat(
        self,
        messages: List[Message],
        model: str,
        temperature: float = 0.15,
        max_tokens: int = -1,
        num_ctx: int = 16384,
        **kwargs,
    ) -> Iterator[str]:
        body = {
            "model": model,
            "messages": self._convert_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens > 0:
            body["max_tokens"] = max_tokens

        with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=body,
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"OpenAI API error {resp.status_code}")

            for line in resp.iter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0].get("delta", {})
                    if "content" in delta:
                        yield delta["content"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    def list_models(self) -> List[str]:
        try:
            resp = self._client.get(
                f"{self.base_url}/models",
                headers=self._headers(),
            )
            if resp.status_code == 200:
                data = resp.json()
                return [m["id"] for m in data.get("data", [])]
        except Exception:
            pass
        return []
