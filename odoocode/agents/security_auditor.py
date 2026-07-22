# forge/agents/security_auditor.py
import re
from typing import Dict, List, Tuple
from .base import BaseAgent
from ..utils import safe_json_extract
from ..prompts import PromptLibrary
from ..ui.tui import console
from rich.panel import Panel

class SecurityAuditor(BaseAgent):
    def __init__(self, llm, config):
        super().__init__(llm, config)
        self.audit_results: Dict[str, dict] = {}

    def audit_module(self, generated_files: Dict[str, str]) -> dict:
        """Audit the entire module for security issues."""
        # Collect all Python and XML content
        py_files = {k: v for k, v in generated_files.items() if k.endswith('.py')}
        xml_files = {k: v for k, v in generated_files.items() if k.endswith('.xml')}
        csv_files = {k: v for k, v in generated_files.items() if k.endswith('.csv')}

        # Build context for security review
        context_parts = []
        for fp, content in generated_files.items():
            snippet = content[:3000] if len(content) > 3000 else content
            context_parts.append(f"--- {fp} ---\n{snippet}\n")

        full_context = "\n".join(context_parts)

        user_prompt = (
            "SECURITY AUDIT OF ODOO 18 MODULE:\n\n"
            f"{full_context}\n\n"
            "Perform a comprehensive security audit. Check for:\n"
            "1. Missing ir.model.access.csv for any model\n"
            "2. Missing ir.rule XML for multi-company isolation\n"
            "3. Overly permissive access rights\n"
            "4. Missing company_id on company-scoped models\n"
            "5. SQL injection risks\n"
            "6. Missing input validation\n"
            "7. Hardcoded IDs\n"
            "8. Missing record ownership restrictions\n\n"
            "Output ONLY valid JSON."
        )

        resp = self.llm.call(
            PromptLibrary.security_review_system(),
            user_prompt,
            self.config.resolve_model("critic"),
            temperature=0.05
        )

        data = safe_json_extract(resp)
        if data:
            self.audit_results = data
            score = data.get('security_score', 0)
            critical = data.get('critical_issues', [])
            warnings = data.get('warnings', [])

            color = "green" if score >= 80 else "yellow" if score >= 60 else "red"
            console.print(Panel(
                f"Security Score: {score}/100\n"
                f"Critical Issues: {len(critical)}\n"
                f"Warnings: {len(warnings)}",
                title=f"[{color}]Security Audit Result[/{color}]",
                border_style=color
            ))

            if critical:
                console.print("[forge.error]Critical Security Issues:[/forge.error]")
                for issue in critical:
                    console.print(f"  [forge.error]* {issue}[/forge.error]")

            if warnings:
                console.print("[forge.warn]Security Warnings:[/forge.warn]")
                for warn in warnings:
                    console.print(f"  [forge.warn]* {warn}[/forge.warn]")

        return data

    def get_missing_security_files(self, generated_files: Dict[str, str]) -> List[str]:
        """Check which security files are missing."""
        missing = []

        # Find all model definitions
        models_found = set()
        for fp, content in generated_files.items():
            if fp.endswith('.py'):
                # Simple regex to find _name = '...'
                for match in re.finditer(r"_name\s*=\s*['\"]([^'\"]+)['\"]", content):
                    models_found.add(match.group(1))
                # Also check _inherit models that might need access rights
                for match in re.finditer(r"_inherit\s*=\s*\[([^\]]+)\]", content):
                    for m in re.finditer(r"['\"]([^'\"]+)['\"]", match.group(1)):
                        models_found.add(m.group(1))

        # Check ir.model.access.csv
        csv_content = ""
        for fp, content in generated_files.items():
            if fp.endswith('ir.model.access.csv'):
                csv_content = content
                break

        # Check which models have access rights
        models_with_access = set()
        if csv_content:
            for match in re.finditer(r"model_([a-z_]+)", csv_content):
                model_name = match.group(1).replace('_', '.')
                models_with_access.add(model_name)

        # Mixin models that do NOT require ir.model.access.csv entries
        ignored_mixins = {
            'mail.thread', 'mail.activity.mixin', 'mail.alias.mixin',
            'rating.mixin', 'utm.mixin', 'image.mixin', 'avatar.mixin',
            'portal.mixin', 'analytic.mixin', 'ir.module.module', 'ir.model',
            'res.company', 'res.partner', 'res.users', 'sale.order', 'account.move'
        }

        # Find missing access rights
        for model in models_found:
            if model not in models_with_access and model not in ignored_mixins and not model.startswith(('mail.', 'rating.', 'utm.')):
                missing.append(f"ir.model.access.csv entry for model '{model}'")

        # Check ir.rule XML
        has_company_rule = False
        for fp, content in generated_files.items():
            if fp.endswith('.xml') and 'ir.rule' in content and 'company_id' in content:
                has_company_rule = True
                break

        # Check for company_id models without rules
        for fp, content in generated_files.items():
            if fp.endswith('.py'):
                if 'company_id' in content and 'fields.Many2one' in content:
                    if not has_company_rule:
                        missing.append(f"ir.rule XML for multi-company isolation in {fp}")

        return missing
