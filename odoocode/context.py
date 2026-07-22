# forge/context.py
"""
OdooCode Context Management.
Token counting, budget allocation, intelligent truncation, and context rebuild.
"""
import re
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("Forge.context")

# Try tiktoken, fall back to rough estimation
try:
    import tiktoken
    _ENCODING = tiktoken.get_encoding("cl100k_base")
    _HAS_TIKTOKEN = True
except ImportError:
    _HAS_TIKTOKEN = False
    _ENCODING = None


def count_tokens(text: str) -> int:
    """Count tokens in text."""
    if _HAS_TIKTOKEN and _ENCODING:
        return len(_ENCODING.encode(text))
    # Rough estimation: ~4 chars per token for English
    return len(text) // 4


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within token limit."""
    if _HAS_TIKTOKEN and _ENCODING:
        tokens = _ENCODING.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return _ENCODING.decode(tokens[:max_tokens]) + "\n...[truncated]"
    # Rough estimation
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


@dataclass
class ContextSection:
    """A section of the context with token budget."""
    name: str
    content: str
    priority: int  # Higher = more important (keep first)
    max_tokens: int = 0  # 0 = auto-calculate
    token_count: int = 0

    def __post_init__(self):
        self.token_count = count_tokens(self.content)


@dataclass
class ContextBudget:
    """Token budget for a context build."""
    total_tokens: int = 16384
    reserved_output: int = 4096
    system_overhead: int = 500  # tokens for system prompt structure

    @property
    def available_tokens(self) -> int:
        return self.total_tokens - self.reserved_output - self.system_overhead

    def allocate(self, sections: List[ContextSection]) -> Dict[str, int]:
        """Allocate token budgets to sections based on priority."""
        total_priority = sum(s.priority for s in sections)
        if total_priority == 0:
            total_priority = len(sections)

        available = self.available_tokens
        allocations = {}

        # Sort by priority (highest first)
        sorted_sections = sorted(sections, key=lambda s: s.priority, reverse=True)

        # First pass: allocate proportional to priority
        for section in sorted_sections:
            proportion = section.priority / total_priority
            allocated = int(available * proportion)
            # Respect max_tokens if set
            if section.max_tokens > 0:
                allocated = min(allocated, section.max_tokens)
            allocations[section.name] = allocated

        # Second pass: distribute remaining budget to high-priority sections
        total_allocated = sum(allocations.values())
        remaining = available - total_allocated
        if remaining > 0 and sorted_sections:
            # Give remaining to highest priority section
            top = sorted_sections[0]
            allocations[top.name] += remaining

        return allocations


class ContextManager:
    """
    Manages context window with token budgeting and intelligent truncation.

    Usage:
        ctx = ContextManager(total_tokens=16384, reserved_output=4096)
        ctx.add_section("system", system_prompt, priority=10)
        ctx.add_section("blueprint", bp_summary, priority=8)
        ctx.add_section("team_memory", team_ctx, priority=6)
        ctx.add_section("memory", memory_ctx, priority=4)
        ctx.add_section("user_prompt", user_prompt, priority=10)
        messages = ctx.build_messages()
    """

    def __init__(self, total_tokens: int = 16384, reserved_output: int = 4096):
        self.budget = ContextBudget(total_tokens=total_tokens, reserved_output=reserved_output)
        self._sections: List[ContextSection] = []
        self._total_input_tokens: int = 0

    def add_section(self, name: str, content: str, priority: int = 5,
                    max_tokens: int = 0, role: str = "user"):
        """Add a content section to the context."""
        section = ContextSection(
            name=name,
            content=content,
            priority=priority,
            max_tokens=max_tokens,
        )
        self._sections.append(section)
        self._total_input_tokens += section.token_count

    def get_section(self, name: str) -> Optional[ContextSection]:
        """Get a section by name."""
        for s in self._sections:
            if s.name == name:
                return s
        return None

    def remove_section(self, name: str):
        """Remove a section by name."""
        self._sections = [s for s in self._sections if s.name != name]
        self._recalculate()

    def _recalculate(self):
        """Recalculate token counts."""
        self._total_input_tokens = sum(s.token_count for s in self._sections)

    @property
    def total_input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def is_over_budget(self) -> bool:
        return self._total_input_tokens > self.budget.available_tokens

    @property
    def utilization(self) -> float:
        """Context utilization as a fraction (0.0 to 1.0+)."""
        return self._total_input_tokens / self.budget.available_tokens if self.budget.available_tokens > 0 else 1.0

    def build_messages(self) -> List[dict]:
        """
        Build the final message list, truncating sections that exceed budget.
        Returns list of {role, content} dicts ready for LLM.
        """
        available = self.budget.available_tokens
        allocations = self.budget.allocate(self._sections)

        messages = []
        system_content = []
        used_tokens = 0

        # Sort sections: system first, then by priority
        system_sections = [s for s in self._sections if s.name.startswith("system")]
        other_sections = [s for s in self._sections if not s.name.startswith("system")]

        # Build system message from system sections
        for section in system_sections:
            max_toks = allocations.get(section.name, section.token_count)
            truncated = truncate_to_tokens(section.content, max_toks)
            system_content.append(truncated)
            used_tokens += count_tokens(truncated)

        if system_content:
            messages.append({
                "role": "system",
                "content": "\n\n".join(system_content),
            })

        # Add non-system sections in priority order
        for section in sorted(other_sections, key=lambda s: s.priority, reverse=True):
            max_toks = allocations.get(section.name, section.token_count)
            remaining = available - used_tokens
            if remaining <= 0:
                logger.warning(f"Context budget exhausted, dropping section: {section.name}")
                continue
            max_toks = min(max_toks, remaining)
            truncated = truncate_to_tokens(section.content, max_toks)
            used_tokens += count_tokens(truncated)
            messages.append({
                "role": "user",
                "content": truncated,
            })

        return messages

    def get_truncated_content(self, name: str, max_tokens: int = 0) -> str:
        """Get truncated content for a section."""
        section = self.get_section(name)
        if not section:
            return ""
        if max_tokens <= 0:
            allocations = self.budget.allocate(self._sections)
            max_tokens = allocations.get(name, section.token_count)
        return truncate_to_tokens(section.content, max_tokens)

    def get_summary(self) -> dict:
        """Get a summary of context usage."""
        section_summary = []
        for s in self._sections:
            section_summary.append({
                "name": s.name,
                "tokens": s.token_count,
                "priority": s.priority,
            })

        return {
            "total_tokens": self.budget.total_tokens,
            "available_tokens": self.budget.available_tokens,
            "used_tokens": self._total_input_tokens,
            "utilization": f"{self.utilization:.1%}",
            "sections": section_summary,
        }


class ContextRebuilder:
    """
    Handles context rebuild when approaching token limits.
    Similar to MiMoCode's checkpoint-based context reconstruction.
    """

    def __init__(self, context_manager: ContextManager):
        self.ctx = context_manager

    def should_rebuild(self, threshold: float = 0.8) -> bool:
        """Check if context should be rebuilt."""
        return self.ctx.utilization > threshold

    def rebuild_with_checkpoint(self, checkpoint_content: str,
                                 recent_messages: List[dict],
                                 max_checkpoint_tokens: int = 4000) -> List[dict]:
        """
        Rebuild context using a checkpoint summary.
        Drops older messages, keeps recent ones verbatim.
        """
        # Truncate checkpoint
        truncated_checkpoint = truncate_to_tokens(checkpoint_content, max_checkpoint_tokens)

        # Build new message list
        messages = [
            {
                "role": "system",
                "content": f"[SESSION CHECKPOINT — earlier context summarized]\n\n{truncated_checkpoint}",
            }
        ]

        # Add recent messages verbatim
        available = self.ctx.budget.available_tokens - count_tokens(truncated_checkpoint) - 200
        for msg in reversed(recent_messages):
            msg_tokens = count_tokens(msg.get("content", ""))
            if available - msg_tokens < 0:
                break
            messages.insert(1, msg)  # Insert after system message
            available -= msg_tokens

        return messages


def optimize_codebase_context(codebase_path: str, per_file_tokens: int = 2000,
                               total_tokens: int = 4000) -> str:
    """
    Read a codebase with token-budgeted chunking.
    Replacement for utils.read_codebase() with proper token limits.
    """
    import os
    from pathlib import Path

    SKIP = {"__pycache__", ".git", "node_modules", ".venv", "venv",
            "migrations", "i18n", "unsloth_compiled_cache", "forge_output"}
    INCL = (".py", ".xml", ".csv", ".js", ".json")

    if not os.path.isdir(codebase_path):
        return ""

    collected = []
    total = 0
    count = 0

    for root, dirs, files in os.walk(codebase_path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for fname in sorted(files):
            if not fname.endswith(INCL):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, codebase_path).replace("\\", "/")
            try:
                raw = Path(full).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            # Token-budget the file content
            file_tokens = count_tokens(raw)
            if file_tokens > per_file_tokens:
                raw = truncate_to_tokens(raw, per_file_tokens)

            entry_tokens = count_tokens(f"--- {rel} ---\n{raw}\n")
            if total + entry_tokens > total_tokens:
                collected.append("...[codebase context truncated — too large]")
                break

            collected.append(f"--- {rel} ---\n{raw}\n")
            total += entry_tokens
            count += 1

    return f"EXISTING CODEBASE ({count} files from {codebase_path}):\n" + "\n".join(collected)
