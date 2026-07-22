# odoocode/config.py
"""
OdooCode Configuration with model groups and provider support.
Compatible with CLI args, odoocode_config.json, or environment variables.
"""
import os
import json
import re
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, field_validator

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# Default model groups — override via config file or CLI
# Optimized for RTX 5050 8GB: fine-tuned model for all Odoo tasks
DEFAULT_MODEL_GROUPS = {
    "coder": "odoo18-coder-v3:latest",       # Fine-tuned for Odoo code generation
    "planner": "odoo18-coder-v3:latest",     # Fine-tuned knows Odoo structure
    "critic": "odoo18-coder-v3:latest",      # Fine-tuned knows Odoo patterns
    "embed": "nomic-embed-text:latest",      # Lightweight embedding model
    "lite": "odoo18-coder-v3:latest",        # Fast tasks
    "standard": "odoo18-coder-v3:latest",    # General purpose
    "ultra": "odoo18-coder-v3:latest",       # Complex analysis
}


def _load_config_file() -> dict:
    """Load config from odoocode_config.json or forge_config.json."""
    search_paths = [
        Path("odoocode_config.json"),
        Path("forge_config.json"),
        Path.home() / ".config" / "odoocode" / "odoocode_config.json",
    ]
    for p in search_paths:
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8")
                text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
                text = re.sub(r"/\*[\s\S]*?\*/", "", text)
                return json.loads(text)
            except Exception:
                continue
    return {}


class OdooCodeConfig(BaseModel):
    """Validated orchestrator configuration with multi-provider support."""

    # === Generation ===
    prompt: str = ""
    output_dir: str = "./odoocode_output"
    mode: str = "generate"
    codebase_path: str = ""
    in_place: bool = False
    zip_output: bool = True
    resume: bool = False
    plan_only: bool = False
    interactive: bool = False

    # === Model Configuration ===
    # Legacy per-role models (kept for backward compatibility)
    coder_model: str = "odoo18-coder-v3:latest"
    planner_model: str = "odoo18-coder-v3:latest"
    critic_model: str = "odoo18-coder-v3:latest"
    embed_model: str = "nomic-embed-text:latest"

    # Model groups — maps role/tier names to provider/model strings
    # Examples: "coder": "openrouter/xiaomi/mimo-v2.5"
    #           "critic": "anthropic/claude-sonnet-4-20250514"
    model_groups: Dict[str, str] = DEFAULT_MODEL_GROUPS.copy()

    # Provider fallback chain — try these models if primary fails
    fallback_chain: List[str] = []

    # === LLM Parameters ===
    max_retries: int = 3
    num_ctx: int = 16384
    temperature: float = 0.15

    # === RAG / Skills ===
    skills_dir: str = "./odoo-18.0-skills"
    top_k_skills: int = 5

    # === Context Management ===
    max_context_tokens: int = 8192  # Optimized for 8GB VRAM
    reserved_output_tokens: int = 2048
    checkpoint_interval: float = 0.4  # checkpoint at 40% context fill

    # === Parallel Execution ===
    max_parallel_files: int = 1  # Conservative for 8GB VRAM, avoids Ollama rate limits

    # === Memory ===
    memory_dir: str = ".odoo_memory"
    auto_checkpoint: bool = True

    # === Hardware Optimization ===
    # Auto-detect GPU and optimize settings
    auto_optimize_hardware: bool = True
    # Force specific VRAM settings (overrides detection)
    force_num_ctx: Optional[int] = None
    force_num_gpu: Optional[int] = None

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"generate", "repair", "edit", "agentic", "modify", "analyze"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}")
        return v

    def get_optimal_settings(self) -> dict:
        """Get optimal settings based on detected hardware."""
        try:
            from .providers.ollama import HardwareInfo
            hw = HardwareInfo.detect()
            return hw.get_recommended_settings()
        except Exception:
            # Fallback for 8GB VRAM
            return {
                "num_ctx": 8192,
                "num_predict": 2048,
                "num_gpu": 99,
                "num_thread": 4,
            }

    def resolve_model(self, role: str) -> str:
        """Resolve a role name to its model string via model_groups."""
        # Try exact role match in model_groups
        if role in self.model_groups:
            return self.model_groups[role]
        # Fall back to legacy per-role fields
        legacy = {
            "coder": self.coder_model,
            "planner": self.planner_model,
            "critic": self.critic_model,
            "embed": self.embed_model,
        }
        return legacy.get(role, self.coder_model)

    def get_available_tokens(self) -> int:
        """Tokens available for content after system overhead."""
        return self.max_context_tokens - self.reserved_output_tokens

    @classmethod
    def from_file(cls, config_path: str = None) -> "OdooCodeConfig":
        """Load config from file, merging with defaults."""
        if config_path:
            path = Path(config_path)
        else:
            path = Path("odoocode_config.json")
            if not path.exists():
                path = Path("forge_config.json")

        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
                text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
                text = re.sub(r"/\*[\s\S]*?\*/", "", text)
                data = json.loads(text)
                # Merge model_groups with defaults
                groups = dict(DEFAULT_MODEL_GROUPS)
                groups.update(data.get("model_groups", {}))
                data["model_groups"] = groups
                return cls(**{k: v for k, v in data.items() if k in cls.model_fields})
            except Exception:
                pass

        return cls(model_groups=dict(DEFAULT_MODEL_GROUPS))


# Alias for backward compatibility
ForgeConfig = OdooCodeConfig

