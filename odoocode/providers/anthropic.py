# forge/providers/anthropic.py
"""Anthropic Claude provider."""
import os
import time
import json
import logging
from typing import List, Iterator
import httpx
from .base import Provider, ProviderResponse, Message

logger = logging.getLogger("Forge.providers.anthropic")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self):
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = httpx.Client(timeout=120.0)

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

    def _split_system(self, messages: List[Message]):
        """Anthropic requires system prompt as a separate parameter."""
        system = ""
        rest = []
        for m in messages:
            if m.role == "system":
                system = m.content
            else:
                rest.append({"role": m.role, "content": m.content})
        return system, rest

    def is_available(self) -> bool:
        if not self.api_key:
            logger.warning("Anthropic API key not set (ANTHROPIC_API_KEY)")
            return False
        # Anthropic doesn't have a models endpoint, just try a small request
        return True

    def _prepare_model(self, model: str) -> str:
        """Strip provider prefix if present (e.g., 'anthropic/claude-sonnet-4-20250514' -> 'claude-sonnet-4-20250514')."""
        if "/" in model:
            return model.split("/", 1)[1]
        return model

    def chat(
        self,
        messages: List[Message],
        model: str,
        temperature: float = 0.15,
        max_tokens: int = -1,
        num_ctx: int = 16384,
        **kwargs,
    ) -> ProviderResponse:
        system, msgs = self._split_system(messages)
        model = self._prepare_model(model)
        body = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens > 0 else 4096,
        }
        if system:
            body["system"] = system

        t0 = time.monotonic()
        resp = self._client.post(
            f"{ANTHROPIC_API_URL}/messages",
            headers=self._headers(),
            json=body,
        )
        latency = (time.monotonic() - t0) * 1000

        if resp.status_code != 200:
            raise RuntimeError(f"Anthropic API error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        content = "".join(
            block["text"] for block in data.get("content", []) if block.get("type") == "text"
        )
        usage = data.get("usage", {})

        return ProviderResponse(
            content=content,
            model=data.get("model", model),
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
            finish_reason=data.get("stop_reason", ""),
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
        system, msgs = self._split_system(messages)
        model = self._prepare_model(model)
        body = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens > 0 else 4096,
            "stream": True,
        }
        if system:
            body["system"] = system

        with self._client.stream(
            "POST",
            f"{ANTHROPIC_API_URL}/messages",
            headers=self._headers(),
            json=body,
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"Anthropic API error {resp.status_code}")

            for line in resp.iter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta["text"]
                except (json.JSONDecodeError, KeyError):
                    continue

    def list_models(self) -> List[str]:
        # Anthropic doesn't have a public models list endpoint
        return [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-3-5-haiku-20241022",
        ]
