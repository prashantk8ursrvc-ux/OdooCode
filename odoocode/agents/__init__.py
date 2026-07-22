# forge/agents/__init__.py
"""
OdooCode — Agent System
"""
from .analyst import AnalystAgent
from .blueprint import BlueprintAgent
from .coder import CoderAgent
from .critic import CriticAgent
from .editor import EditAgent
from .repair import RepairAgent
from .security_auditor import SecurityAuditor
from .codebase_agent import CodebaseAgent, FileReader, CodebaseAnalyzer, OdooSpecialist
from .modify_agent import ModifyAgent, ModifyRequest, ModifyResult

__all__ = [
    "AnalystAgent", "BlueprintAgent", "CoderAgent", "CriticAgent",
    "EditAgent", "RepairAgent", "SecurityAuditor",
    "CodebaseAgent", "FileReader", "CodebaseAnalyzer", "OdooSpecialist",
    "ModifyAgent", "ModifyRequest", "ModifyResult"
]
