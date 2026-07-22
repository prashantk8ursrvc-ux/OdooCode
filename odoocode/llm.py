# forge/llm.py
"""
OdooCode Multi-Provider LLM Client.
Supports Ollama, OpenAI, Anthropic, OpenRouter with model groups and streaming.
"""
import logging
import time
import os
from typing import Any, Dict, List, Optional, Iterator, Tuple
from rich.console import Console

from .providers.base import Message, ProviderResponse
from .providers.factory import create_provider, resolve_model
from .context import ContextManager, count_tokens

logger = logging.getLogger("ForgeOrchestrator.LLM")
console = Console()


class LLMClient:
    """
    Provider-agnostic LLM client with:
    - Multi-provider support (Ollama, OpenAI, Anthropic, OpenRouter)
    - Model groups (coder, planner, critic, embed)
    - Streaming output
    - Automatic retry with provider fallback
    - Token counting and context budget management
    - GPU-aware optimizations for local Ollama
    """

    def __init__(self, config, skill_retriever=None):
        self.config = config
        self.skill_retriever = skill_retriever
        self._model_groups = getattr(config, "model_groups", {})
        self._call_count = 0
        self._total_tokens = 0
        self._fallback_chain = getattr(config, "fallback_chain", [])
        self._hw_settings = None

        # Auto-detect hardware for Ollama optimization
        if getattr(config, 'auto_optimize_hardware', True):
            try:
                from .providers.ollama import HardwareInfo
                hw = HardwareInfo.detect()
                self._hw_settings = hw.get_recommended_settings()
                logger.info(f"Hardware optimization: {hw.gpu_name} ({hw.vram_gb:.1f}GB)")
                logger.info(f"Recommended settings: ctx={self._hw_settings['num_ctx']}, "
                           f"predict={self._hw_settings['num_predict']}")
            except Exception as e:
                logger.warning(f"Hardware detection failed: {e}")

    def _fetch_skills(self, semantic_query: str) -> str:
        if not self.skill_retriever:
            return ""
        try:
            result = self.skill_retriever.get_relevant_context(semantic_query)
            return f"\n\n--- ODOO 18 REFERENCE (RAG) ---\n{result}\n" if result else ""
        except Exception as exc:
            logger.warning(f"Skill retrieval failed: {exc}")
            return ""

    def _build_messages(self, system_prompt: str, user_prompt: str, context: str = "",
                        skill_query: str = "") -> List[Message]:
        skills = self._fetch_skills(skill_query or user_prompt[:300])
        if skills:
            system_prompt += skills

        msgs = []
        if system_prompt:
            msgs.append(Message(role="system", content=system_prompt))
        if context:
            msgs.append(Message(role="user", content=f"[TEAM CONTEXT]\n{context}"))
            msgs.append(Message(role="assistant", content="Team context received. Ready."))
        msgs.append(Message(role="user", content=user_prompt))
        return msgs

    def _resolve_model(self, model_ref: str) -> str:
        """Resolve a model reference (group name or literal) to actual provider/model."""
        provider_name, model_id = resolve_model(
            model_ref,
            model_groups=self._model_groups,
        )
        return model_id

    def _get_provider(self, model_ref: str):
        """Get the appropriate provider for a model reference."""
        provider_name, model_id = resolve_model(
            model_ref,
            model_groups=self._model_groups,
        )
        return create_provider(provider_name), model_id

    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        num_predict: int = -1,
        context: str = "",
        temperature: Optional[float] = None,
        skill_query: str = "",
    ) -> str:
        """Synchronous chat completion with retry and fallback."""
        msgs = self._build_messages(system_prompt, user_prompt, context, skill_query)
        temp = temperature if temperature is not None else self.config.temperature
        max_tokens = num_predict if num_predict > 0 else -1

        # Use hardware-optimized num_ctx if available
        num_ctx = self.config.num_ctx
        if self._hw_settings and self._hw_settings.get("num_ctx"):
            # Use the smaller of config and hardware recommendation
            num_ctx = min(num_ctx, self._hw_settings["num_ctx"])
        # Respect force overrides
        if hasattr(self.config, 'force_num_ctx') and self.config.force_num_ctx:
            num_ctx = self.config.force_num_ctx

        provider, model_id = self._get_provider(model)
        logger.info(f"LLM call provider={provider.name} model={model_id} "
                     f"sys={len(system_prompt)}ch usr={len(user_prompt)}ch ctx={num_ctx}")

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = provider.chat(
                    messages=msgs,
                    model=model_id,
                    temperature=temp,
                    max_tokens=max_tokens,
                    num_ctx=num_ctx,
                )
                self._call_count += 1
                self._total_tokens += resp.usage.get("total_tokens", 0)
                logger.info(f"LLM response: {len(resp.content)} chars, "
                            f"{resp.usage.get('total_tokens', '?')} tokens, "
                            f"{resp.latency_ms:.0f}ms")
                return resp.content
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(f"{provider.name} error attempt {attempt}: {exc}. Retry in {wait}s")
                if attempt < self.config.max_retries:
                    time.sleep(wait)

        # Try fallback providers
        for fallback_model in self._fallback_chain:
            if fallback_model == model:
                continue
            try:
                fb_provider, fb_model_id = self._get_provider(fallback_model)
                logger.info(f"Falling back to {fb_provider.name}/{fb_model_id}")
                resp = fb_provider.chat(
                    messages=msgs,
                    model=fb_model_id,
                    temperature=temp,
                    max_tokens=max_tokens,
                    num_ctx=self.config.num_ctx,
                )
                self._call_count += 1
                self._total_tokens += resp.usage.get("total_tokens", 0)
                return resp.content
            except Exception as fb_exc:
                logger.warning(f"Fallback {fb_provider.name} also failed: {fb_exc}")

        raise RuntimeError(f"All LLM providers failed. Last error: {last_exc}") from last_exc

    def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        num_predict: int = -1,
        context: str = "",
        temperature: Optional[float] = None,
        skill_query: str = "",
    ) -> Iterator[str]:
        """Streaming chat completion."""
        msgs = self._build_messages(system_prompt, user_prompt, context, skill_query)
        temp = temperature if temperature is not None else self.config.temperature
        max_tokens = num_predict if num_predict > 0 else -1

        provider, model_id = self._get_provider(model)
        logger.info(f"LLM stream provider={provider.name} model={model_id}")

        return provider.stream_chat(
            messages=msgs,
            model=model_id,
            temperature=temp,
            max_tokens=max_tokens,
            num_ctx=self.config.num_ctx,
        )

    def get_stats(self) -> dict:
        """Return usage statistics."""
        return {
            "total_calls": self._call_count,
            "total_tokens": self._total_tokens,
        }

    def call_with_context(
        self,
        context_manager: ContextManager,
        model: str,
        num_predict: int = -1,
        temperature: Optional[float] = None,
    ) -> Tuple[str, dict]:
        """
        Make an LLM call using a ContextManager for intelligent message building.
        Returns (response_content, context_summary).
        """
        # Build messages from context manager
        raw_msgs = context_manager.build_messages()

        # Convert to Message objects
        msgs = [Message(role=m["role"], content=m["content"]) for m in raw_msgs]

        temp = temperature if temperature is not None else self.config.temperature
        max_tokens = num_predict if num_predict > 0 else -1

        provider, model_id = self._get_provider(model)
        logger.info(f"LLM call (context-aware) provider={provider.name} model={model_id} "
                     f"sections={len(context_manager._sections)} "
                     f"utilization={context_manager.utilization:.1%}")

        last_exc = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = provider.chat(
                    messages=msgs,
                    model=model_id,
                    temperature=temp,
                    max_tokens=max_tokens,
                    num_ctx=self.config.num_ctx,
                )
                self._call_count += 1
                self._total_tokens += resp.usage.get("total_tokens", 0)

                summary = context_manager.get_summary()
                summary["response_tokens"] = resp.usage.get("completion_tokens", 0)
                summary["latency_ms"] = resp.latency_ms

                return resp.content, summary
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(f"{provider.name} error attempt {attempt}: {exc}. Retry in {wait}s")
                if attempt < self.config.max_retries:
                    time.sleep(wait)

        raise RuntimeError(f"LLM call failed after {self.config.max_retries} attempts: {last_exc}")

    def list_available_models(self) -> Dict[str, List[str]]:
        """List models from all configured providers."""
        result = {}
        for provider_name in ["ollama", "openai", "openrouter", "anthropic"]:
            try:
                p = create_provider(provider_name)
                if p.is_available():
                    result[provider_name] = p.list_models()
            except Exception:
                result[provider_name] = []
        return result
