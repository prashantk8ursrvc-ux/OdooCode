# forge/providers/factory.py
"""Provider factory and model resolution."""
import os
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple
from .base import Provider

logger = logging.getLogger("Forge.providers.factory")

# Cache loaded config
_config_cache: Optional[dict] = None


def _load_config() -> dict:
    """Load config from odoocode_config.json or forge_config.json."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    search_paths = [
        Path("odoocode_config.json"),
        Path("forge_config.json"),
        Path.home() / ".config" / "odoocode" / "odoocode_config.json",
    ]

    for p in search_paths:
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8")
                # Strip JSONC comments
                import re
                text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
                text = re.sub(r"/\*[\s\S]*?\*/", "", text)
                _config_cache = json.loads(text)
                logger.info(f"Loaded config from {p}")
                return _config_cache
            except Exception as e:
                logger.warning(f"Failed to load {p}: {e}")

    _config_cache = {}
    return _config_cache


def _detect_provider(model: str) -> str:
    """Detect provider from model string format."""
    if "/" in model:
        prefix = model.split("/")[0].lower()
        known = {"openai", "openrouter", "anthropic", "ollama", "google", "azure"}
        if prefix in known:
            return prefix
        # openrouter format: provider/model (e.g., "anthropic/claude-3-opus")
        # If first segment isn't a known provider, assume openrouter
        return "openrouter"
    # No slash = local Ollama model
    return "ollama"


def resolve_model(
    model_ref: str,
    model_groups: Optional[Dict] = None,
    provider_override: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Resolve a model reference to (provider_name, actual_model_id).

    model_ref can be:
      - "provider/model" (literal) -> returns as-is
      - "coder" (group name) -> resolves via model_groups config
      - "ollama_model_name" (no slash) -> defaults to ollama provider
    """
    config = _load_config()
    groups = model_groups or config.get("model_groups", {})

    # If it's a group name, resolve to actual model
    if "/" not in model_ref and model_ref in groups:
        group_val = groups[model_ref]
        if isinstance(group_val, str):
            model_ref = group_val
        elif isinstance(group_val, dict):
            model_ref = group_val.get("default", model_ref)

    # Detect provider
    provider = provider_override or _detect_provider(model_ref)

    # For ollama, strip provider prefix if present
    if provider == "ollama" and model_ref.startswith("ollama/"):
        model_ref = model_ref[len("ollama/"):]

    return provider, model_ref


# Provider instances cache
_providers: Dict[str, Provider] = {}


def create_provider(provider_name: str, auto_optimize: bool = True) -> Provider:
    """Create or return cached provider instance."""
    cache_key = f"{provider_name}:{auto_optimize}"
    if cache_key in _providers:
        return _providers[cache_key]

    if provider_name == "ollama":
        from .ollama import OllamaProvider
        p = OllamaProvider(auto_optimize=auto_optimize)
    elif provider_name == "openai":
        from .openai import OpenAIProvider
        p = OpenAIProvider(provider_type="openai")
    elif provider_name == "openrouter":
        from .openai import OpenAIProvider
        p = OpenAIProvider(provider_type="openrouter")
    elif provider_name == "anthropic":
        from .anthropic import AnthropicProvider
        p = AnthropicProvider()
    else:
        raise ValueError(f"Unknown provider: {provider_name}")

    _providers[cache_key] = p
    return p
