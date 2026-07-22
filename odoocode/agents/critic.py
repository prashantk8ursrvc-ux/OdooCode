# forge/agents/critic.py
from pathlib import Path
from typing import Tuple, Dict
from .base import BaseAgent
from ..utils import safe_json_extract
from ..prompts import PromptLibrary
from ..ui.tui import console
from rich.panel import Panel

class CriticAgent(BaseAgent):
    def __init__(self, llm, config):
        super().__init__(llm, config)
        self.reviewed_registry: Dict[str, str] = {}

    def _build_team_context(self, bf, bp_summary: str) -> str:
        ctx = f"MODULE BLUEPRINT OVERVIEW:\n{bp_summary}\n\n"
        if self.reviewed_registry:
            ctx += (
                "ALREADY REVIEWED FILES (check consistency against these):\n"
                + "\n".join(f"  - {fp}: {summary}" for fp, summary in self.reviewed_registry.items())
                + "\n\n"
            )
        return ctx

    def review(self, bf, bp_summary: str = "") -> Tuple[bool, str, str]:
        """
        Review a file and return (passed, reasoning, edit_instructions).
        The edit_instructions contain SPECIFIC issues to fix, not vague descriptions.
        """
        if self.config.interactive:
            return True, "Interactive — critic skipped", ""
        cross_ctx = self._build_team_context(bf, bp_summary)
        skill_query = f"Odoo 18 best practices code review {Path(bf.filepath).suffix.strip('.')} {bf.description}"
        user_prompt = (
            f"{cross_ctx}"
            f"FILE BEING REVIEWED: {bf.filepath}\n\n"
            f"SPEC (what this file should do, written by Architect):\n{bf.spec or '(none)'}\n\n"
            f"GENERATED CODE:\n{bf.content}\n\n"
            "Cross-check this code against: (1) the SPEC above, (2) the module blueprint, "
            "(3) all already-reviewed files for field name / XML ID consistency.\n"
            "List EVERY specific issue found. Output ONLY valid JSON."
        )
        resp = self.llm.call(
            PromptLibrary.critic_system(bf.filepath),
            user_prompt,
            self.config.resolve_model("critic"), temperature=0.05,
            skill_query=skill_query)
        data = safe_json_extract(resp)
        if data:
            status    = str(data.get("status", "fail")).lower()
            score     = int(data.get("score", 0))
            issues    = data.get("issues", [])
            fix_ins   = data.get("fix_instructions", "")
            passed    = (status == "pass")

            # Build reasoning from issues list
            if issues:
                reasoning_parts = []
                for issue in issues:
                    if isinstance(issue, dict):
                        reasoning_parts.append(f"[{issue.get('type', '?')}] {issue.get('detail', '')}")
                    else:
                        reasoning_parts.append(str(issue))
                reasoning = "\n".join(reasoning_parts)
            else:
                reasoning = str(data.get("reasoning", ""))

            # Use fix_instructions if available, otherwise format issues
            edit_ins = fix_ins
            if not edit_ins and issues:
                edit_ins = "\n".join(
                    f"- {i.get('detail', str(i))}" if isinstance(i, dict) else f"- {i}"
                    for i in issues
                )

            if not passed and not edit_ins.strip():
                edit_ins = reasoning

            bf.critic_score = float(score)
            self.reviewed_registry[bf.filepath] = (
                f"score={score}, status={status}, issues={len(issues)}"
            )
            color = "green" if passed else "red"
            console.print(Panel(
                f"Score: {score}/100\nIssues: {len(issues)}\n{reasoning[:500]}",
                title=f"[{color}]Critic {'PASS' if passed else 'FAIL'}: {bf.filepath}[/{color}]",
                border_style=color))
            return passed, reasoning, edit_ins

        # Fallback for non-JSON response
        up = resp.upper()
        passed = up.startswith("PASS") or ("PASS" in up[:20])
        edit_ins = ""
        reasoning = resp[:200]
        if not passed:
            idx = up.find("FAIL"); colon = resp.find(":", idx) if idx >= 0 else -1
            edit_ins = resp[colon + 1:].strip() if colon != -1 else resp[max(0, idx + 4):].strip()
            if not edit_ins.strip():
                edit_ins = resp.strip()
        self.reviewed_registry[bf.filepath] = f"status={'pass' if passed else 'fail'}"
        return passed, reasoning, edit_ins
