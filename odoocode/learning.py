# forge/learning.py
"""
OdooCode Self-Improvement System.
Pattern learning from critic feedback, skill evolution, and feedback tracking.
Inspired by MiMoCode's /dream and /distill commands.
"""
import os
import re
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger("Forge.learning")


@dataclass
class Pattern:
    """A learned pattern from critic feedback or successful fixes."""
    pattern_type: str  # e.g., "xml_validation", "model_structure", "security"
    description: str
    trigger: str  # What condition triggers this pattern
    fix: str  # How to fix it
    confidence: float = 0.5  # 0.0 to 1.0
    occurrences: int = 1
    last_seen: str = ""
    examples: List[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"### [{self.pattern_type}] {self.description}",
            f"**Trigger:** {self.trigger}",
            f"**Fix:** {self.fix}",
            f"**Confidence:** {self.confidence:.0%} ({self.occurrences} occurrences)",
        ]
        if self.examples:
            lines.append("**Examples:**")
            for ex in self.examples[:3]:
                lines.append(f"```\n{ex[:200]}\n```")
        return "\n".join(lines)


@dataclass
class Skill:
    """An evolved skill from successful fix cycles."""
    name: str
    description: str
    trigger: str  # When to use this skill
    prompt_template: str  # LLM prompt template
    examples: List[str] = field(default_factory=list)
    success_rate: float = 0.0
    uses: int = 0

    def to_skill_md(self) -> str:
        return f"""# Skill: {self.name}

## Description
{self.description}

## When to Use
{self.trigger}

## Prompt Template
{self.prompt_template}

## Examples
{chr(10).join(f'- {ex[:100]}' for ex in self.examples[:5])}

## Stats
- Uses: {self.uses}
- Success Rate: {self.success_rate:.0%}
"""


class PatternLearner:
    """
    Extracts and stores patterns from critic feedback and fix cycles.
    """

    def __init__(self, memory_dir: str = ".odoo_memory"):
        self.patterns_file = Path(memory_dir) / "knowledge" / "patterns.md"
        self.patterns_file.parent.mkdir(parents=True, exist_ok=True)
        self._patterns: Dict[str, Pattern] = {}
        self._load_patterns()

    def _load_patterns(self):
        """Load patterns from file."""
        if not self.patterns_file.exists():
            return

        content = self.patterns_file.read_text(encoding="utf-8")
        current_type = None
        current_data = {}

        for line in content.split('\n'):
            if line.startswith('### ['):
                # Save previous pattern
                if current_type and current_data:
                    key = f"{current_type}:{current_data.get('description', '')}"
                    self._patterns[key] = Pattern(**current_data)

                # Parse new pattern header
                match = re.match(r'### \[(\w+)\]\s+(.+)', line)
                if match:
                    current_type = match.group(1)
                    current_data = {
                        'pattern_type': current_type,
                        'description': match.group(2),
                        'trigger': '',
                        'fix': '',
                        'confidence': 0.5,
                        'occurrences': 1,
                        'examples': [],
                    }
            elif current_data and line.startswith('**Trigger:**'):
                current_data['trigger'] = line[len('**Trigger:**'):].strip()
            elif current_data and line.startswith('**Fix:**'):
                current_data['fix'] = line[len('**Fix:**'):].strip()
            elif current_data and line.startswith('**Confidence:**'):
                match = re.search(r'(\d+)%.*?(\d+) occurrences', line)
                if match:
                    current_data['confidence'] = int(match.group(1)) / 100
                    current_data['occurrences'] = int(match.group(2))

        # Save last pattern
        if current_type and current_data:
            key = f"{current_type}:{current_data.get('description', '')}"
            self._patterns[key] = Pattern(**current_data)

    def record_critic_feedback(self, filepath: str, score: float,
                                reasoning: str, edit_instructions: str = ""):
        """Record critic feedback as a pattern."""
        if score >= 80:
            return  # Good score, no pattern needed

        # Extract pattern from feedback
        pattern_type = self._classify_feedback(reasoning)
        trigger = self._extract_trigger(filepath, reasoning)
        fix = edit_instructions or reasoning

        key = f"{pattern_type}:{trigger[:100]}"
        if key in self._patterns:
            # Update existing pattern
            p = self._patterns[key]
            p.occurrences += 1
            p.confidence = min(1.0, p.confidence + 0.1)
            p.last_seen = datetime.now().isoformat()
        else:
            # Create new pattern
            self._patterns[key] = Pattern(
                pattern_type=pattern_type,
                description=trigger[:200],
                trigger=trigger,
                fix=fix[:500],
                confidence=0.3,
                occurrences=1,
                last_seen=datetime.now().isoformat(),
                examples=[reasoning[:300]],
            )

        self._save_patterns()
        logger.info(f"Recorded pattern: {pattern_type} (confidence={self._patterns[key].confidence:.0%})")

    def _classify_feedback(self, reasoning: str) -> str:
        """Classify feedback into a pattern type."""
        reasoning_lower = reasoning.lower()
        if any(w in reasoning_lower for w in ['xml', 'tree', 'list', 'view']):
            return 'xml_validation'
        if any(w in reasoning_lower for w in ['_description', '_name', '_inherit', 'model']):
            return 'model_structure'
        if any(w in reasoning_lower for w in ['security', 'access', 'ir.rule', 'ir.model']):
            return 'security'
        if any(w in reasoning_lower for w in ['manifest', 'depends', 'data']):
            return 'manifest'
        if any(w in reasoning_lower for w in ['field', 'compute', 'api']):
            return 'field_definition'
        return 'general'

    def _extract_trigger(self, filepath: str, reasoning: str) -> str:
        """Extract a trigger condition from feedback."""
        ext = Path(filepath).suffix
        if ext == '.xml':
            return f"XML file {filepath}: {reasoning[:150]}"
        elif ext == '.py':
            return f"Python file {filepath}: {reasoning[:150]}"
        return f"{filepath}: {reasoning[:150]}"

    def get_relevant_patterns(self, filepath: str, context: str = "") -> List[Pattern]:
        """Get patterns relevant to a file or context."""
        ext = Path(filepath).suffix
        relevant = []

        for pattern in self._patterns.values():
            # Match by file extension
            if ext == '.xml' and pattern.pattern_type == 'xml_validation':
                relevant.append(pattern)
            elif ext == '.py' and pattern.pattern_type in ('model_structure', 'field_definition'):
                relevant.append(pattern)
            elif pattern.pattern_type == 'security' and 'security' in filepath:
                relevant.append(pattern)

            # Match by context keywords
            if context:
                context_lower = context.lower()
                if any(word in context_lower for word in pattern.trigger.lower().split()[:3]):
                    relevant.append(pattern)

        # Sort by confidence and occurrences
        relevant.sort(key=lambda p: p.confidence * p.occurrences, reverse=True)
        return relevant[:5]

    def get_patterns_as_context(self, filepath: str = "") -> str:
        """Get patterns formatted as context for LLM injection."""
        patterns = self.get_relevant_patterns(filepath)
        if not patterns:
            return ""

        lines = ["[LEARNED PATTERNS — from previous critic feedback]\n"
                 "Apply these patterns to avoid common mistakes:\n"]
        for p in patterns:
            lines.append(f"- [{p.pattern_type}] {p.description}")
            lines.append(f"  Fix: {p.fix[:200]}")
        return "\n".join(lines)

    def _save_patterns(self):
        """Save patterns to file."""
        lines = ["# Learned Patterns\n",
                 "_Patterns extracted from successful critic reviews and fixes._\n"]

        # Group by type
        by_type = defaultdict(list)
        for pattern in self._patterns.values():
            by_type[pattern.pattern_type].append(pattern)

        for pattern_type, patterns in sorted(by_type.items()):
            lines.append(f"\n## {pattern_type.replace('_', ' ').title()}\n")
            # Sort by confidence * occurrences
            patterns.sort(key=lambda p: p.confidence * p.occurrences, reverse=True)
            for p in patterns[:10]:  # Keep top 10 per type
                lines.append(p.to_markdown())
                lines.append("")

        self.patterns_file.write_text("\n".join(lines), encoding="utf-8")

    def get_stats(self) -> dict:
        return {
            "total_patterns": len(self._patterns),
            "by_type": dict(defaultdict(int, {
                t: len([p for p in self._patterns.values() if p.pattern_type == t])
                for t in set(p.pattern_type for p in self._patterns.values())
            })),
        }


class SkillEvolver:
    """
    Evolves skills from successful fix cycles.
    When the same fix pattern succeeds multiple times, it becomes a reusable skill.
    """

    def __init__(self, memory_dir: str = ".odoo_memory"):
        self.skills_dir = Path(memory_dir) / "knowledge" / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._skills: Dict[str, Skill] = {}
        self._load_skills()

    def _load_skills(self):
        """Load skills from directory."""
        for skill_file in self.skills_dir.glob("*.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
                name = skill_file.stem
                self._skills[name] = Skill(
                    name=name,
                    description=self._extract_section(content, "Description"),
                    trigger=self._extract_section(content, "When to Use"),
                    prompt_template=self._extract_section(content, "Prompt Template"),
                    examples=[l.strip('- ') for l in self._extract_section(content, "Examples").split('\n') if l.strip().startswith('-')],
                )
            except Exception as e:
                logger.warning(f"Failed to load skill {skill_file}: {e}")

    def _extract_section(self, content: str, section_name: str) -> str:
        """Extract a section from markdown content."""
        match = re.search(rf'## {section_name}\n(.*?)(?=\n## |\Z)', content, re.DOTALL)
        return match.group(1).strip() if match else ""

    def record_successful_fix(self, filepath: str, pattern_type: str,
                               fix_description: str, original_error: str):
        """Record a successful fix that could become a skill."""
        skill_name = f"{pattern_type}_{Path(filepath).suffix[1:]}"

        if skill_name in self._skills:
            skill = self._skills[skill_name]
            skill.uses += 1
            skill.success_rate = min(1.0, skill.success_rate + 0.1)
            if fix_description not in skill.examples:
                skill.examples.append(fix_description[:200])
        else:
            self._skills[skill_name] = Skill(
                name=skill_name,
                description=f"Auto-evolved skill for {pattern_type} in {Path(filepath).suffix} files",
                trigger=f"When {pattern_type} issues are detected in {Path(filepath).suffix} files",
                prompt_template=f"Fix {pattern_type} issues. Original error: {original_error[:200]}",
                examples=[fix_description[:200]],
                success_rate=0.5,
                uses=1,
            )

        # Save skill if it's been used enough
        if self._skills[skill_name].uses >= 3:
            self._save_skill(skill_name)

    def _save_skill(self, skill_name: str):
        """Save a skill to file."""
        skill = self._skills.get(skill_name)
        if not skill:
            return
        skill_file = self.skills_dir / f"{skill_name}.md"
        skill_file.write_text(skill.to_skill_md(), encoding="utf-8")
        logger.info(f"Evolved skill: {skill_name} (uses={skill.uses}, rate={skill.success_rate:.0%})")

    def get_skill_context(self, filepath: str = "") -> str:
        """Get relevant skills formatted as context."""
        ext = Path(filepath).suffix if filepath else ""
        relevant = [s for s in self._skills.values()
                    if ext and ext[1:] in s.name or not ext]

        if not relevant:
            return ""

        lines = ["[EVOLVED SKILLS — learned from successful fixes]\n"]
        for s in sorted(relevant, key=lambda x: x.uses * x.success_rate, reverse=True)[:3]:
            lines.append(f"- {s.name}: {s.description}")
            lines.append(f"  Trigger: {s.trigger}")
        return "\n".join(lines)

    def get_stats(self) -> dict:
        return {
            "total_skills": len(self._skills),
            "by_type": dict(defaultdict(int, {
                s.name.split('_')[0]: 1 for s in self._skills.values()
            })),
        }


class FeedbackTracker:
    """
    Tracks feedback scores over time to measure improvement.
    """

    def __init__(self, memory_dir: str = ".odoo_memory"):
        self.feedback_file = Path(memory_dir) / "knowledge" / "feedback.json"
        self.feedback_file.parent.mkdir(parents=True, exist_ok=True)
        self._history: List[dict] = []
        self._load()

    def _load(self):
        if self.feedback_file.exists():
            try:
                self._history = json.loads(self.feedback_file.read_text(encoding="utf-8"))
            except Exception:
                self._history = []

    def _save(self):
        self.feedback_file.write_text(
            json.dumps(self._history[-100:], indent=2),  # Keep last 100 entries
            encoding="utf-8",
        )

    def record(self, filepath: str, score: float, phase: str = "critic"):
        """Record a feedback score."""
        self._history.append({
            "timestamp": datetime.now().isoformat(),
            "filepath": filepath,
            "score": score,
            "phase": phase,
        })
        self._save()

    def get_trend(self, window: int = 10) -> dict:
        """Get the trend of scores over time."""
        if not self._history:
            return {"avg": 0, "trend": "stable", "data_points": 0}

        recent = self._history[-window:]
        scores = [h["score"] for h in recent]
        avg = sum(scores) / len(scores)

        # Simple trend: compare first half to second half
        if len(scores) >= 4:
            first_half = sum(scores[:len(scores)//2]) / (len(scores)//2)
            second_half = sum(scores[len(scores)//2:]) / (len(scores)//2)
            if second_half > first_half + 5:
                trend = "improving"
            elif second_half < first_half - 5:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"

        return {
            "avg": avg,
            "trend": trend,
            "data_points": len(self._history),
            "recent_scores": scores[-5:],
        }

    def get_stats(self) -> dict:
        return {
            "total_records": len(self._history),
            "trend": self.get_trend(),
        }


class LearningManager:
    """
    Unified learning manager that coordinates pattern learning,
    skill evolution, and feedback tracking.
    """

    def __init__(self, memory_dir: str = ".odoo_memory"):
        self.pattern_learner = PatternLearner(memory_dir)
        self.skill_evolver = SkillEvolver(memory_dir)
        self.feedback_tracker = FeedbackTracker(memory_dir)

    def on_critic_review(self, filepath: str, score: float,
                          reasoning: str, edit_instructions: str = ""):
        """Called after each critic review to learn from the feedback."""
        self.pattern_learner.record_critic_feedback(filepath, score, reasoning, edit_instructions)
        self.feedback_tracker.record(filepath, score, "critic")

    def on_successful_fix(self, filepath: str, pattern_type: str,
                           fix_description: str, original_error: str):
        """Called after a successful fix to evolve skills."""
        self.skill_evolver.record_successful_fix(filepath, pattern_type, fix_description, original_error)
        self.feedback_tracker.record(filepath, 100.0, "fix_success")

    def get_learning_context(self, filepath: str = "") -> str:
        """Get all learning context for injection into LLM prompts."""
        parts = []
        patterns_ctx = self.pattern_learner.get_patterns_as_context(filepath)
        if patterns_ctx:
            parts.append(patterns_ctx)
        skills_ctx = self.skill_evolver.get_skill_context(filepath)
        if skills_ctx:
            parts.append(skills_ctx)
        return "\n\n".join(parts) if parts else ""

    def dream(self) -> str:
        """Consolidate learnings (similar to MiMoCode's /dream)."""
        trend = self.feedback_tracker.get_trend()
        pattern_stats = self.pattern_learner.get_stats()
        skill_stats = self.skill_evolver.get_stats()

        summary = [
            "# Learning Dream Summary",
            f"_Generated: {datetime.now().isoformat()}_\n",
            f"## Performance Trend",
            f"- Average score: {trend['avg']:.1f}/100",
            f"- Trend: {trend['trend']}",
            f"- Data points: {trend['data_points']}\n",
            f"## Patterns Learned",
            f"- Total patterns: {pattern_stats['total_patterns']}",
        ]
        for ptype, count in pattern_stats.get('by_type', {}).items():
            summary.append(f"  - {ptype}: {count}")

        summary.append(f"\n## Skills Evolved")
        summary.append(f"- Total skills: {skill_stats['total_skills']}")

        return "\n".join(summary)

    def get_stats(self) -> dict:
        return {
            "patterns": self.pattern_learner.get_stats(),
            "skills": self.skill_evolver.get_stats(),
            "feedback": self.feedback_tracker.get_stats(),
        }
