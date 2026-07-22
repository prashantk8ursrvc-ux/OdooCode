# forge/__init__.py
"""
OdooCode v6.0 — Multi-Provider Agentic Odoo 18 Generator
Like MiMoCode but specialized for Odoo module development.
"""
from .main import main
from .agent_mode import OdooCodeAgent, run_agent
from .interactive import OdooCodeWizard, run_wizard, quick_build
from .config import ForgeConfig
from .workflow import ForgeWorkflow
from .llm import LLMClient
from .memory import MemoryManager
from .learning import LearningManager
from .context import ContextManager, count_tokens, truncate_to_tokens
from .providers import Provider, ProviderResponse, create_provider, resolve_model
from .tools import ShellRunner, GitClient, SkillRetriever

__version__ = "6.0.0"
__app_name__ = "OdooCode"

__all__ = [
    "main", "OdooCodeAgent", "run_agent",
    "OdooCodeWizard", "run_wizard", "quick_build",
    "ForgeConfig", "ForgeWorkflow", "LLMClient", "MemoryManager", "LearningManager",
    "ContextManager", "count_tokens", "truncate_to_tokens",
    "Provider", "ProviderResponse", "create_provider", "resolve_model",
    "ShellRunner", "GitClient", "SkillRetriever",
]
