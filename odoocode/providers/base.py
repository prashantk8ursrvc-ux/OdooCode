# forge/providers/base.py
"""Abstract provider interface for multi-LLM support."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Iterator


@dataclass
class Message:
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class ProviderResponse:
    content: str
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)  # prompt_tokens, completion_tokens, total_tokens
    finish_reason: str = ""
    latency_ms: float = 0.0


class Provider(ABC):
    """Base class for LLM providers."""

    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: List[Message],
        model: str,
        temperature: float = 0.15,
        max_tokens: int = -1,
        num_ctx: int = 16384,
        **kwargs,
    ) -> ProviderResponse:
        """Send a chat completion request."""
        ...

    @abstractmethod
    def stream_chat(
        self,
        messages: List[Message],
        model: str,
        temperature: float = 0.15,
        max_tokens: int = -1,
        num_ctx: int = 16384,
        **kwargs,
    ) -> Iterator[str]:
        """Stream chat completion tokens."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is reachable."""
        ...

    def list_models(self) -> List[str]:
        """List available models (optional)."""
        return []
