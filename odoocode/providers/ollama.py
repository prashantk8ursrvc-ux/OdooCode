# forge/providers/ollama.py
"""
Ollama local LLM provider with GPU-aware optimizations.
Optimized for RTX 5050 8GB VRAM + fine-tuned models.
"""
import time
import logging
import subprocess
import json
from typing import List, Iterator, Optional, Dict, Any
from .base import Provider, ProviderResponse, Message

logger = logging.getLogger("Forge.providers.ollama")


class HardwareInfo:
    """Detect GPU hardware and VRAM for optimal configuration."""

    _instance = None
    _vram_gb: float = 0
    _gpu_name: str = ""
    _detected: bool = False

    @classmethod
    def detect(cls) -> "HardwareInfo":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        if self._detected:
            return
        self._detect_gpu()
        self._detected = True

    def _detect_gpu(self):
        """Detect GPU via nvidia-smi."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(", ")
                if len(parts) >= 2:
                    self._gpu_name = parts[0].strip()
                    self._vram_gb = float(parts[1].strip()) / 1024
                    logger.info(f"Detected GPU: {self._gpu_name} ({self._vram_gb:.1f} GB VRAM)")
                    return
        except Exception:
            pass

        # Fallback: try ollama ps to see what's loaded
        try:
            result = subprocess.run(
                ["ollama", "ps"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                logger.info("GPU detection via nvidia-smi failed, using Ollama defaults")
        except Exception:
            pass

        self._gpu_name = "Unknown GPU"
        self._vram_gb = 8.0  # Conservative default

    @property
    def vram_gb(self) -> float:
        return self._vram_gb

    @property
    def gpu_name(self) -> str:
        return self._gpu_name

    @property
    def is_low_vram(self) -> bool:
        return self._vram_gb <= 8.0

    @property
    def is_medium_vram(self) -> bool:
        return 8.0 < self._vram_gb <= 16.0

    @property
    def is_high_vram(self) -> bool:
        return self._vram_gb > 16.0

    def get_optimal_num_ctx(self, model_size_estimate_gb: float = 4.0) -> int:
        """Calculate optimal context window based on available VRAM."""
        # Reserve VRAM for model weights and overhead
        usable_vram = self._vram_gb - model_size_estimate_gb - 1.0  # 1GB overhead
        if usable_vram <= 0:
            return 2048  # Minimum usable context
        # Rough estimate: ~1K context per 0.5GB VRAM
        optimal = int(usable_vram * 1024 * 2)
        # Clamp to reasonable bounds
        return max(2048, min(optimal, 32768))

    def get_recommended_settings(self) -> Dict[str, Any]:
        """Get recommended Ollama settings for this hardware."""
        if self.is_low_vram:
            return {
                "num_ctx": 16384,
                "num_predict": 2048,
                "num_gpu": 99,  # Offload all layers to GPU
                "num_thread": 4,
                "repeat_penalty": 1.1,
                "top_k": 40,
                "top_p": 0.9,
                "batch_size": 256,  # Reduced to fit in 8GB VRAM
            }
        elif self.is_medium_vram:
            return {
                "num_ctx": 16384,
                "num_predict": 4096,
                "num_gpu": 99,
                "num_thread": 8,
                "repeat_penalty": 1.1,
                "top_k": 40,
                "top_p": 0.9,
                "batch_size": 1024,
            }
        else:  # High VRAM
            return {
                "num_ctx": 32768,
                "num_predict": 8192,
                "num_gpu": 99,
                "num_thread": 16,
                "repeat_penalty": 1.1,
                "top_k": 40,
                "top_p": 0.9,
                "batch_size": 2048,
            }


class OllamaProvider(Provider):
    """Ollama local LLM provider with GPU-aware optimizations."""

    name = "ollama"

    def __init__(self, auto_optimize: bool = True):
        try:
            import ollama as _ollama
            self._client = _ollama
        except ImportError:
            self._client = None

        self._hw = HardwareInfo.detect()
        self._auto_optimize = auto_optimize
        self._model_loaded: Optional[str] = None

        if auto_optimize:
            logger.info(f"Ollama provider initialized with hardware optimization "
                       f"({self._hw.gpu_name}, {self._hw.vram_gb:.1f}GB VRAM)")

    def is_available(self) -> bool:
        if not self._client:
            return False
        try:
            self._client.list()
            return True
        except Exception:
            return False

    def _get_optimized_opts(self, model: str, temperature: float,
                            max_tokens: int, num_ctx: int) -> dict:
        """Build optimized options based on hardware and model."""
        hw_settings = self._hw.get_recommended_settings()

        opts = {
            "temperature": temperature,
            "num_ctx": min(num_ctx, hw_settings["num_ctx"]),
            "num_predict": max_tokens if max_tokens > 0 else hw_settings["num_predict"],
            "num_gpu": hw_settings["num_gpu"],
            "num_thread": hw_settings["num_thread"],
            "repeat_penalty": hw_settings["repeat_penalty"],
            "top_k": hw_settings["top_k"],
            "top_p": hw_settings["top_p"],
        }

        # For fine-tuned models, be more conservative with sampling
        if "finetuned" in model.lower() or "coder" in model.lower():
            opts["temperature"] = min(temperature, 0.15)  # Lower temp for code
            opts["top_k"] = 20  # More focused sampling
            opts["repeat_penalty"] = 1.05  # Less aggressive repetition penalty

        return opts

    def _check_model_loaded(self, model: str) -> bool:
        """Check if a model is already loaded in VRAM."""
        try:
            resp = self._client.ps()
            for m in resp.get("models", []):
                # Handle both Model objects and dicts
                name = m.model if hasattr(m, 'model') else m.get("name", "")
                if name.startswith(model.split(":")[0]):
                    self._model_loaded = model
                    return True
        except Exception:
            pass
        return False

    def _preload_model(self, model: str):
        """Pre-load model into VRAM if not already loaded."""
        if self._check_model_loaded(model):
            logger.info(f"Model {model} already loaded in VRAM")
            return

        # Send a minimal request to load the model
        try:
            logger.info(f"Pre-loading model {model} into VRAM...")
            self._client.chat(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                options={"num_predict": 1},
            )
            self._model_loaded = model
            logger.info(f"Model {model} loaded successfully")
        except Exception as e:
            logger.warning(f"Failed to pre-load model {model}: {e}")

    def chat(
        self,
        messages: List[Message],
        model: str,
        temperature: float = 0.15,
        max_tokens: int = -1,
        num_ctx: int = 16384,
        **kwargs,
    ) -> ProviderResponse:
        opts = self._get_optimized_opts(model, temperature, max_tokens, num_ctx)

        # Pre-load model if auto-optimization enabled
        if self._auto_optimize:
            self._preload_model(model)

        t0 = time.monotonic()
        resp = self._client.chat(
            model=model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            options=opts,
        )
        latency = (time.monotonic() - t0) * 1000

        content = resp["message"]["content"]
        return ProviderResponse(
            content=content,
            model=model,
            latency_ms=latency,
            finish_reason="stop",
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
        opts = self._get_optimized_opts(model, temperature, max_tokens, num_ctx)

        # Pre-load model if auto-optimization enabled
        if self._auto_optimize:
            self._preload_model(model)

        stream = self._client.chat(
            model=model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            options=opts,
            stream=True,
        )
        for chunk in stream:
            if "message" in chunk and "content" in chunk["message"]:
                yield chunk["message"]["content"]

    def list_models(self) -> List[str]:
        try:
            resp = self._client.list()
            models = resp.get("models", [])
            # Ollama returns Model objects with .model attribute, not dicts
            return [m.model if hasattr(m, 'model') else m.get("name", str(m)) for m in models]
        except Exception:
            return []

    def get_hardware_info(self) -> dict:
        """Get detected hardware information."""
        return {
            "gpu_name": self._hw.gpu_name,
            "vram_gb": self._hw.vram_gb,
            "is_low_vram": self._hw.is_low_vram,
            "optimal_num_ctx": self._hw.get_optimal_num_ctx(),
            "recommended_settings": self._hw.get_recommended_settings(),
        }

    def unload_model(self, model: str = None):
        """Unload model from VRAM to free memory."""
        try:
            # Ollama doesn't have a direct unload API, but we can generate with 0 tokens
            # to trigger garbage collection
            if model:
                self._client.generate(
                    model=model,
                    prompt="",
                    options={"num_predict": 0},
                )
            self._model_loaded = None
            logger.info("Model unloaded from VRAM")
        except Exception as e:
            logger.warning(f"Failed to unload model: {e}")
