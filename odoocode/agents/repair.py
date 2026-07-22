# forge/agents/repair.py
import os, re
from pathlib import Path
from typing import List, Any
from .base import BaseAgent
from ..models import Issue
from ..utils import strip_code_fences
from ..prompts import PromptLibrary

REPAIR_PATTERNS = [
    (re.compile(r"<tree\b"),                  "error",   "deprecated_tree_tag",
     "Replace <tree> with <list> — tree tag deprecated in Odoo 18"),
    (re.compile(r"@api\.multi"),              "error",   "removed_api_multi",
     "@api.multi removed in Odoo 16. Methods are multi-record by default."),
    (re.compile(r"@api\.one"),                "error",   "removed_api_one",
     "@api.one removed in Odoo 16. Iterate self manually."),
    (re.compile(r"@api\.returns"),            "warning", "deprecated_api_returns",
     "@api.returns deprecated. Remove and adjust return values."),
    (re.compile(r'attrs\s*=\s*[\'"]'),        "error",   "removed_attrs",
     "attrs= removed in Odoo 17. Use direct boolean expressions."),
    (re.compile(r'states\s*=\s*[\'"]'),       "error",   "removed_states_attr",
     "states= removed in Odoo 17. Use invisible=/required= directly."),
    (re.compile(r"group_operator\s*="),       "warning", "deprecated_group_operator",
     "group_operator= deprecated since Odoo 17. Use aggregator=."),
    (re.compile(r"\.sudo\(\)(?!\s*#)"),       "warning", "unreviewed_sudo",
     "sudo() without justification comment."),
    (re.compile(r"env\[.res\.users.\]\.browse\(.*uid.*\)"), "warning", "old_user_browse",
     "Use self.env.user instead of env['res.users'].browse(uid)."),
    (re.compile(r"http\.request\.session\[.uid.\]"), "error", "old_session_uid",
     "Use request.env.uid instead of request.session['uid']."),
    (re.compile(r"class\s+\w+.*models\.Model.*:\s*$"), "info", "missing_description_check",
     "Ensure class has _description attribute."),
    (re.compile(r"fields\.(Char|Text|Html)\(.*size\s*=\s*\d"), "info", "char_with_size",
     "size= on Char/Text ignored in PostgreSQL."),
    (re.compile(r'view_mode\s*=\s*[\'"]tree'), "error", "view_mode_tree",
     "view_mode='tree,...' invalid in Odoo 18. Use 'list,...'"),
]

class RepairAgent(BaseAgent):
    def deterministic_fix(self, filepath: str, content: str) -> str:
        """Perform fast, non-LLM regex transformations for common Odoo 18 deprecation patterns."""
        if filepath.endswith(".xml"):
            content = re.sub(r'<tree\b', '<list', content)
            content = re.sub(r'</tree>', '</list>', content)
            content = re.sub(r'view_mode=["\']tree', 'view_mode="list', content)
            content = re.sub(r'kanban-box', 't-name="card"', content)
        elif filepath.endswith(".py"):
            content = re.sub(r'@api\.multi\s*\n', '', content)
            content = re.sub(r'group_operator\s*=', 'aggregator=', content)
        return content

    def scan(self, codebase_path: str) -> List[Issue]:
        SKIP = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
        issues: List[Issue] = []
        for root, dirs, files in os.walk(codebase_path):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for fname in sorted(files):
                if not fname.endswith((".py", ".xml")):
                    continue
                full = os.path.join(root, fname)
                rel  = os.path.relpath(full, codebase_path).replace("\\", "/")
                try:
                    lines = Path(full).read_text(encoding="utf-8", errors="replace").splitlines(True)
                except Exception:
                    continue
                for lineno, line in enumerate(lines, 1):
                    for pat, sev, name, sug in REPAIR_PATTERNS:
                        if pat.search(line):
                            issues.append(Issue(filepath=rel, line_number=lineno,
                                                pattern_name=name, severity=sev,
                                                current_line=line.rstrip(), suggestion=sug))
        return issues

    def fix_file(self, filepath: str, content: str, file_issues: Any) -> str:
        # First attempt fast deterministic fix
        content = self.deterministic_fix(filepath, content)
        
        if isinstance(file_issues, str):
            issues_text = file_issues
        elif isinstance(file_issues, list):
            issues_lines = []
            for i in file_issues:
                if isinstance(i, str):
                    issues_lines.append(f"  • {i}")
                elif hasattr(i, "line_number"):
                    issues_lines.append(
                        f"  Line {getattr(i, 'line_number', 0)} [{getattr(i, 'severity', 'error').upper()}] {getattr(i, 'pattern_name', 'issue')}: {getattr(i, 'suggestion', '')}\n"
                        f"    Current: {getattr(i, 'current_line', '')}"
                    )
                else:
                    issues_lines.append(f"  • {str(i)}")
            issues_text = "\n".join(issues_lines)
        else:
            issues_text = str(file_issues)

        return strip_code_fences(self.llm.call(
            PromptLibrary.repair_system(),
            f"FILE: {filepath}\n\nISSUES / INSTRUCTIONS:\n{issues_text}\n\nCONTENT:\n{content}\n",
            self.config.resolve_model("coder")))
