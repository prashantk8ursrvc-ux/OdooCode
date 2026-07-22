# forge/models.py
from dataclasses import dataclass, field
from typing import List, Dict, Set

@dataclass
class BlueprintFile:
    filepath: str
    description: str
    spec: str = ""
    depends_on: List[str] = field(default_factory=list)
    content: str = ""
    status: str = "pending"   # pending|generated|validated|approved|failed
    critic_score: float = 0.0
    retry_count: int = 0

@dataclass
class Issue:
    filepath: str
    line_number: int
    pattern_name: str
    severity: str
    current_line: str
    suggestion: str

@dataclass
class ModuleState:
    models_created: Dict[str, List[str]] = field(default_factory=dict)
    data_dependencies: Set[str] = field(default_factory=set)
    security_groups: Set[str] = field(default_factory=set)
    view_dependencies: Set[str] = field(default_factory=set)
    generated_files: List[str] = field(default_factory=list)
    key_points: List[str] = field(default_factory=list)
