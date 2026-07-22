# forge/utils.py
import re, json, csv as csv_module, io, ast, os
import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional, Dict, Any
from collections import deque, defaultdict
from pathlib import Path
from .models import BlueprintFile

def strip_code_fences(content: str) -> str:
    content = content.lstrip("\ufeff")
    # Strip markdown code fences: ```python ... ```
    fence_re = re.compile(r"^```[a-zA-Z0-9_+\-]*\n([\s\S]*?)```\s*$", re.MULTILINE)
    match = fence_re.search(content.strip())
    cleaned = match.group(1).strip() if match else content.strip()
    # Strip literal "python" at start (markdown artifact)
    if cleaned.startswith("python\n"):
        cleaned = cleaned[7:]
    lines = cleaned.split("\n")
    if lines and lines[0].strip().startswith(("--- FILE:", "## FILE:", "# FILE:")):
        lines = lines[1:]
    return "\n".join(lines).strip()

def strip_plan_code(analysis: str) -> str:
    if not analysis:
        return ""
    analysis = re.sub(
        r"```(?!markdown)[\w]*\n[\s\S]*?```",
        "[code block removed — see prose description above]",
        analysis)
    analysis = re.sub(
        r"(^[ \t]*(?:from |import |class |def |@api\.|<record|<field|<menuitem|<template|<odoo|<data)[^\n]+\n)+",
        "[code removed]\n",
        analysis, flags=re.MULTILINE)
    return analysis

def detect_lexer(filepath: str) -> str:
    return {".py": "python", ".xml": "xml", ".js": "javascript",
            ".ts": "typescript", ".csv": "text", ".json": "json",
            ".md": "markdown"}.get(Path(filepath).suffix.lower(), "text")

def safe_json_extract(text: str, fallback: Optional[dict] = None) -> Optional[dict]:
    for m in reversed(list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL))):
        try:
            return json.loads(m.group(0))
        except Exception:
            continue
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    return fallback

def _layer_priority(filepath: str) -> int:
    fp = filepath.replace("\\", "/").lower()
    if fp.startswith("models/") and fp.endswith(".py") and not fp.endswith("__init__.py"):
        return 0  # Layer 0: Python Models MUST be generated FIRST
    if fp.startswith("security/"):
        return 1  # Layer 1: Security XML & CSV generated SECOND
    if fp.startswith("data/"):
        return 2  # Layer 2: Sequences & Data XML generated THIRD
    if fp.startswith("controllers/") and not fp.endswith("__init__.py"):
        return 3  # Layer 3: Website Controllers (@http.route) generated FOURTH
    if fp.startswith("views/") and fp.endswith("_views.xml") and not fp.endswith("menu.xml") and not fp.endswith("dashboard_views.xml"):
        return 4  # Layer 4: Model Views generated FIFTH
    if fp.startswith("static/") or fp.endswith("dashboard_views.xml") or fp.endswith("templates.xml"):
        return 5  # Layer 5: Web Assets & Website QWeb Templates SIXTH
    if fp.endswith("menu.xml"):
        return 6  # Layer 6: Menu items generated SEVENTH
    if fp.endswith("__init__.py"):
        return 7  # Layer 7: Package inits generated EIGHTH
    if fp.endswith("__manifest__.py"):
        return 8  # Layer 8: Manifest generated LAST
    return 9

def topological_sort(blueprint: List[BlueprintFile]) -> List[BlueprintFile]:
    # Primary sort by strict architectural layer priority, preserving relative order within layers
    return sorted(blueprint, key=lambda b: (_layer_priority(b.filepath), b.filepath))

def read_codebase(path: str, per_file_cap: int = 8_000, total_cap: int = 60_000,
                   per_file_tokens: int = 2000, total_tokens: int = 15000) -> str:
    """
    Walk codebase and return a context string for the planner.
    Uses token-based budgeting when available, falls back to char-based.
    """
    from .context import count_tokens, truncate_to_tokens

    INCL = (".py", ".xml", ".csv", ".js", ".json")
    SKIP = {"__pycache__", ".git", "node_modules", ".venv", "venv",
            "migrations", "i18n", "unsloth_compiled_cache", "forge_output"}
    if not os.path.isdir(path):
        return ""
    collected = []
    total = 0
    count = 0
    truncated = False
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for fname in sorted(files):
            if not fname.endswith(INCL):
                continue
            full = os.path.join(root, fname)
            rel  = os.path.relpath(full, path).replace("\\", "/")
            try:
                raw = Path(full).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # Token-budget the file content
            raw = truncate_to_tokens(raw, per_file_tokens)
            entry = f"--- {rel} ---\n{raw}\n"
            entry_tokens = count_tokens(entry)
            if total + entry_tokens > total_tokens:
                truncated = True
                break
            collected.append(entry)
            total += entry_tokens
            count += 1
        if truncated:
            collected.append("...[codebase context truncated — too large]")
            break
    return f"EXISTING CODEBASE ({count} files from {path}):\n" + "\n".join(collected)


class Validator:
    """Comprehensive Odoo module validator with deep semantic checks."""

    @staticmethod
    def validate(filepath: str, content: str) -> Tuple[bool, str]:
        if filepath.endswith("__manifest__.py"):
            return Validator._validate_manifest(content)
        if filepath.endswith(".py") and not filepath.endswith("__init__.py"):
            return Validator._validate_python(content, filepath)
        if filepath.endswith(".py") and filepath.endswith("__init__.py"):
            return True, ""  # init files are simple imports
        if filepath.endswith(".xml"):
            return Validator._validate_xml(content)
        if filepath.endswith(".csv"):
            return Validator._validate_csv(content, filepath)
        if filepath.endswith(".json"):
            try:
                json.loads(content); return True, ""
            except json.JSONDecodeError as exc:
                return False, f"JSON ParseError: {exc}"
        return True, ""

    @staticmethod
    def _validate_manifest(content: str) -> Tuple[bool, str]:
        try:
            val = ast.literal_eval(content)
            if not isinstance(val, dict):
                return False, "Manifest does not evaluate to a Python dict."

            # Required keys for Odoo 18
            required = {"name", "version", "depends", "data"}
            missing = required - val.keys()
            if missing:
                return False, f"Manifest missing required keys: {missing}"

            # Version format check
            version = val.get("version", "")
            if version and not re.match(r"^\d+\.\d+\.\d+\.\d+\.\d+$", version):
                return False, f"Version '{version}' doesn't follow Odoo 18 format (e.g., '18.0.1.0.0')"

            # Check for license
            if "license" not in val:
                return False, "Manifest missing 'license' key (use 'LGPL-3' or 'OEEL-1')"

            # Check data list ordering
            data = val.get("data", [])
            if data:
                issues = Validator._check_data_ordering(data)
                if issues:
                    return False, f"Data ordering issues: {'; '.join(issues)}"

            return True, ""
        except Exception as exc:
            return False, f"Manifest SyntaxError: {exc}"

    @staticmethod
    def _check_data_ordering(data: list) -> list:
        """Check that data files are in correct order: security XML > security CSV > data > views > report > demo."""
        issues = []
        last_section = -1

        for filepath in data:
            current_section = 3  # default to views
            if filepath.endswith('.xml') and 'security' in filepath:
                current_section = 0  # Security XML (groups/rules) MUST be 0
            elif filepath.endswith('.csv') and 'security' in filepath:
                current_section = 1  # ir.model.access.csv MUST be 1
            elif filepath.startswith("data/"):
                current_section = 2
            elif filepath.startswith("views/"):
                current_section = 3
            elif filepath.startswith("report/"):
                current_section = 4
            elif filepath.startswith("demo/"):
                current_section = 5

            if current_section < last_section:
                if current_section == 0 and last_section == 1:
                    issues.append(f"Security XML file '{filepath}' MUST come BEFORE 'ir.model.access.csv' in manifest data")
                else:
                    issues.append(f"'{filepath}' is in wrong manifest data order (should come earlier)")
            last_section = max(last_section, current_section)

        return issues

    @staticmethod
    def _validate_python(content: str, filepath: str) -> Tuple[bool, str]:
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            return False, f"Python SyntaxError line {exc.lineno}: {exc.msg}\n  {exc.text}"

        # Deep Odoo-specific checks
        issues = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                # Check for Model classes
                class_body = ast.dump(node)
                is_model = ('models.Model' in class_body or
                           'models.TransientModel' in class_body or
                           'models.AbstractModel' in class_body)

                if is_model:
                    # Check _description
                    has_description = any(
                        isinstance(n, ast.Assign) and
                        any(isinstance(t, ast.Constant) and t.value == '_description'
                            for t in n.targets)
                        for n in node.body
                    )
                    if not has_description:
                        issues.append(f"Class '{node.name}' missing _description (REQUIRED by Odoo)")

                    # Check _name or _inherit
                    has_name = any(
                        isinstance(n, ast.Assign) and
                        any(isinstance(t, ast.Constant) and t.value in ('_name', '_inherit')
                            for t in n.targets)
                        for n in node.body
                    )
                    if not has_name:
                        issues.append(f"Class '{node.name}' missing _name or _inherit")

                    # Check _order
                    has_order = any(
                        isinstance(n, ast.Assign) and
                        any(isinstance(t, ast.Constant) and t.value == '_order'
                            for t in n.targets)
                        for n in node.body
                    )
                    if not has_order:
                        issues.append(f"Class '{node.name}' missing _order (recommended)")

        # Check for deprecated patterns
        deprecated_patterns = [
            (r'@api\.multi', "@api.multi removed since Odoo 14"),
            (r'@api\.one', "@api.one removed since Odoo 14"),
            (r'@api\.cr\b', "@api.cr removed — use self.env"),
            (r'@api\.uid\b', "@api.uid removed — use self.env.uid"),
            (r'group_operator\s*=', "group_operator deprecated — use aggregator="),
            (r'attrs\s*=\s*["\']', "attrs= removed in Odoo 17 — use direct expressions"),
            (r'states\s*=\s*["\']', "states= removed in Odoo 17 — use invisible="),
            (r'self\.env\._\(', "self.env._() is invalid — use _() with 'from odoo import _'"),
        ]

        # Check for missing imports
        has_user_error = 'UserError' in content and 'from odoo.exceptions' in content
        has_validation_error = 'ValidationError' in content and 'from odoo.exceptions' in content
        raises_user_error = 'raise UserError' in content
        raises_validation_error = 'raise ValidationError' in content

        if raises_user_error and not has_user_error:
            issues.append("Missing import: 'from odoo.exceptions import UserError'")
        if raises_validation_error and not has_validation_error:
            issues.append("Missing import: 'from odoo.exceptions import ValidationError'")

        # Check for wrong translation call
        if 'self.env._(' in content:
            issues.append("self.env._() is invalid — use _('text') with 'from odoo import _'")

        # Check for literal 'python' at start (markdown artifact)
        if content.strip().startswith('python\n') or content.strip() == 'python':
            issues.append("File starts with literal 'python' — remove this markdown artifact")

        # Check for unrelated content (hallucination detection)
        unrelated_keywords = ['training_course', 'training.course', 'training course',
                             'employee training', 'training_manager', 'training_user']
        for keyword in unrelated_keywords:
            if keyword.lower() in content.lower():
                # Only flag if the file is NOT about training
                if 'training' not in filepath.lower():
                    issues.append(f"File contains unrelated content: '{keyword}' — this appears to be hallucinated from a different module")

        for pattern, msg in deprecated_patterns:
            if re.search(pattern, content):
                issues.append(msg)

        if issues:
            return False, "; ".join(issues)
        return True, ""

    @staticmethod
    def _validate_xml(content: str) -> Tuple[bool, str]:
        try:
            # Strip BOM and XML declaration before wrapping — declaration must stay at document start
            stripped = content.lstrip("\ufeff").strip()
            stripped = re.sub(r"<\?xml[^?]*\?>", "", stripped, count=1).strip()
            ET.fromstring(f"<_root>{stripped}</_root>")
        except ET.ParseError as exc:
            return False, f"XML ParseError: {exc}"

        issues = []

        # Check for deprecated tree tag
        if '<tree' in content:
            issues.append("Use <list> NOT <tree> — <tree> deprecated in Odoo 18")

        # Check for attrs
        if re.search(r'attrs\s*=', content):
            issues.append("attrs= removed in Odoo 17 — use direct boolean expressions")

        # Check for states attribute
        if re.search(r'\bstates\s*=\s*["\']', content):
            issues.append("states= removed in Odoo 17 — use invisible=")

        # Check for view_mode tree
        if re.search(r"view_mode\s*=\s*['\"]tree", content):
            issues.append("view_mode='tree' invalid in Odoo 18 — use 'list'")

        # Check for kanban-box (deprecated in Odoo 18)
        if 'kanban-box' in content:
            issues.append("kanban-box deprecated in Odoo 18 — use t-name='card'")

        # Check button types and names
        for button_match in re.finditer(r'<button\s+([^>]+)>', content):
            attrs_str = button_match.group(1)
            # Match type attribute
            type_match = re.search(r'type=["\']([^"\']+)["\']', attrs_str)
            if not type_match:
                # Special Odoo buttons like close/special don't always require type
                if not re.search(r'special=["\']([^"\']+)["\']', attrs_str):
                    issues.append("<button> missing required type='object' or type='action' attribute")
            else:
                btype = type_match.group(1)
                if btype not in ("object", "action", "edit", "cancel"):
                    issues.append(f"<button> has invalid type='{btype}' (must be 'object' or 'action')")
                elif btype == "object":
                    name_match = re.search(r'name=["\']([^"\']+)["\']', attrs_str)
                    if not name_match:
                        issues.append("<button type='object'> missing required name attribute (method name)")
                    else:
                        method_name = name_match.group(1)
                        if not re.match(r'^[a-zA-Z_]\w*$', method_name):
                            issues.append(f"<button type='object'> has invalid method name '{method_name}'")

        if issues:
            return False, "; ".join(issues)
        return True, ""

    @staticmethod
    def _validate_csv(content: str, filepath: str) -> Tuple[bool, str]:
        try:
            reader = csv_module.reader(io.StringIO(content))
            rows = list(reader)
            if not rows:
                return False, "CSV file is empty"

            # Must have at least header + 1 data row
            if len(rows) < 2:
                return False, f"CSV has only {len(rows)} row(s), needs header + at least 1 data row"

            header = [h.strip().lower() for h in rows[0]]
            # Accept common header variations
            valid_headers = [
                ["id", "name", "model_id:id", "group_id:id", "perm_read", "perm_write", "perm_create", "perm_unlink"],
                ["id", "name", "model_id:id", "group_id:id", "perm_read", "perm_write", "perm_create", "perm_unlink"],
                ["id", "name", "model_id:id", "group_id:id", "perm_read", "perm_write", "perm_create", "perm_unlink"],
            ]
            # Normalize expected header for comparison
            expected_normalized = ["id", "name", "model_id:id", "group_id:id", "perm_read", "perm_write", "perm_create", "perm_unlink"]

            if header != expected_normalized:
                # Check if it's close enough (same number of columns, similar names)
                if len(header) != len(expected_normalized):
                    return False, f"CSV has {len(header)} columns, expected {len(expected_normalized)}"
                # Allow if columns match structurally
                for h, e in zip(header, expected_normalized):
                    h_clean = h.replace(" ", "").replace("_", "")
                    e_clean = e.replace(" ", "").replace("_", "")
                    if h_clean != e_clean and h != e:
                        return False, f"Column mismatch: '{h}' vs expected '{e}'"

            # Check each row
            for i, row in enumerate(rows[1:], 2):
                if len(row) != 8:
                    return False, f"Row {i} has {len(row)} columns, expected 8"

                # Check model_id format
                model_id = row[2].strip()
                if not model_id.startswith("model_"):
                    return False, f"Row {i}: model_id:id must start with 'model_'"

                # Check permissions are 0 or 1
                for j in range(4, 8):
                    if row[j] not in ("0", "1"):
                        return False, f"Row {i}: permission column {j} must be 0 or 1"

            return True, ""
        except Exception as exc:
            return False, f"CSV ParseError: {exc}"


class ModuleStructureValidator:
    """Validates that a generated module has all required files and structure."""

    REQUIRED_FILES = [
        "__manifest__.py",
        "__init__.py",
        "models/__init__.py",
    ]

    REQUIRED_SECURITY = [
        "security/ir.model.access.csv",
    ]

    def validate_structure(self, generated_files: Dict[str, str]) -> Tuple[bool, List[str]]:
        """Validate the complete module structure."""
        issues = []

        # Check required files
        for req_file in self.REQUIRED_FILES:
            if req_file not in generated_files:
                issues.append(f"Missing required file: {req_file}")

        # Check security files
        has_security_csv = any(
            fp.endswith("ir.model.access.csv")
            for fp in generated_files.keys()
        )
        if not has_security_csv:
            issues.append("Missing security/ir.model.access.csv")

        # Check for models without access rights
        models_found = set()
        for fp, content in generated_files.items():
            if fp.endswith('.py'):
                for match in re.finditer(r"_name\s*=\s*['\"]([^'\"]+)['\"]", content):
                    models_found.add(match.group(1))

        # Check CSV for each model
        csv_content = ""
        for fp, content in generated_files.items():
            if fp.endswith('ir.model.access.csv'):
                csv_content = content
                break

        if csv_content:
            for model in models_found:
                if model not in csv_content.replace('_', '.'):
                    issues.append(f"Model '{model}' missing from ir.model.access.csv")

        # Check manifest data references existing files
        manifest_content = generated_files.get("__manifest__.py", "")
        if manifest_content:
            try:
                manifest = ast.literal_eval(manifest_content)
                data_files = manifest.get("data", [])
                for data_file in data_files:
                    if data_file not in generated_files:
                        issues.append(f"Manifest references non-existent file: {data_file}")
            except Exception:
                issues.append("Could not parse manifest for data file validation")

        # Check init files only import existing model files
        for fp, content in generated_files.items():
            if fp.endswith("__init__.py"):
                dir_path = str(Path(fp).parent)
                for line in content.split('\n'):
                    line = line.strip()
                    if line.startswith('from . import '):
                        module_name = line.replace('from . import ', '').strip()
                        # Check if the imported module exists
                        expected_file = f"{dir_path}/{module_name}.py"
                        if expected_file not in generated_files and module_name != "models":
                            issues.append(f"Init file {fp} imports '{module_name}' but {expected_file} does not exist")

        # Collect model fields for XML-Python cross-validation
        model_fields = defaultdict(set)
        BUILTIN_FIELDS = {
            "id", "name", "display_name", "create_date", "write_date",
            "create_uid", "write_uid", "active", "sequence", "company_id",
            "currency_id", "state", "color", "notes", "description", "user_id",
            "order_line", "product_id", "product_uom_qty", "price_unit", "discount",
            "tax_id", "price_subtotal", "partner_id", "amount_total", "date_order",
            "invoice_date", "line_ids", "invoice_line_ids", "payment_reference",
            "move_type", "ref", "journal_id", "activity_ids", "message_follower_ids",
            "message_ids", "date", "maintenance_request_ids"
        }
        for fp, content in generated_files.items():
            if fp.endswith('.py') and not fp.endswith('__init__.py'):
                name_match = re.search(r"_name\s*=\s*['\"]([^'\"]+)['\"]", content)
                inherit_match = re.search(r"_inherit\s*=\s*['\"]([^'\"]+)['\"]", content)
                model_name = name_match.group(1) if name_match else (inherit_match.group(1) if inherit_match else "")
                if model_name:
                    for field_match in re.finditer(r'^\s+(\w+)\s*=\s*fields\.', content, re.MULTILINE):
                        model_fields[model_name].add(field_match.group(1))

        # Cross-reference XML view fields against model fields
        for fp, content in generated_files.items():
            if fp.endswith('.xml'):
                for record_match in re.finditer(r'<record\b[^>]*model=["\']ir\.ui\.view["\'][^>]*>(.*?)</record>', content, re.DOTALL):
                    record_body = record_match.group(1)
                    target_model_match = re.search(r'<field\s+name=["\']model["\']>([^<]+)</field>', record_body)
                    if target_model_match:
                        target_model = target_model_match.group(1).strip()
                        if target_model in model_fields:
                            declared_fields = model_fields[target_model] | BUILTIN_FIELDS
                            arch_match = re.search(r'<field\s+name=["\']arch["\'][^>]*>(.*?)</field>', record_body, re.DOTALL)
                            if arch_match:
                                arch_body = arch_match.group(1)
                                for view_field_match in re.finditer(r'<field\s+name=["\']([^"\']+)["\']', arch_body):
                                    vf_name = view_field_match.group(1)
                                    if vf_name not in declared_fields and not vf_name.startswith(('context', 'search_default')):
                                        issues.append(f"XML view '{fp}' references field '{vf_name}' not defined in Python model '{target_model}'")

        # Controller validation & website template cross-referencing
        xml_template_ids = set()
        for fp, content in generated_files.items():
            if fp.endswith('.xml'):
                for t_match in re.finditer(r'<template\s+id=["\']([^"\']+)["\']', content):
                    xml_template_ids.add(t_match.group(1))

        for fp, content in generated_files.items():
            if fp.startswith("controllers/") and fp.endswith(".py"):
                # Check forbidden direct model import
                if re.search(r"from\s+\.?\.*models\b", content):
                    issues.append(f"Controller '{fp}' directly imports Python model class instead of using request.env['model_name']")
                
                # Check request.render template references
                for render_match in re.finditer(r"request\.render\(['\"]([^'\"]+)['\"]", content):
                    template_ref = render_match.group(1) # e.g. barber_shop_saas.shop_register
                    template_short_id = template_ref.split(".")[-1]
                    if template_short_id not in xml_template_ids:
                        issues.append(f"Controller '{fp}' calls request.render('{template_ref}') but template '{template_short_id}' is not defined in XML templates")

        # Client Action Dashboard & JS Action Registry cross-referencing
        client_action_tags = set()
        for fp, content in generated_files.items():
            if fp.endswith('.xml'):
                for act_match in re.finditer(r'<record\b[^>]*model=["\']ir\.actions\.client["\'][^>]*>(.*?)</record>', content, re.DOTALL):
                    tag_match = re.search(r'<field\s+name=["\']tag["\']>([^<]+)</field>', act_match.group(1))
                    if tag_match:
                        client_action_tags.add(tag_match.group(1).strip())

        js_action_registrations = set()
        for fp, content in generated_files.items():
            if fp.startswith("static/src/js/") and fp.endswith((".js", ".ts")):
                for reg_match in re.finditer(r"registry\.category\(['\"]actions['\"]\)\.add\(['\"]([^'\"]+)['\"]", content):
                    js_action_registrations.add(reg_match.group(1).strip())

        for tag in client_action_tags:
            if tag not in js_action_registrations:
                issues.append(f"Client action tag '{tag}' in XML is not registered in JS action registry via registry.category('actions').add('{tag}', Component)")

        # Validate Manifest Assets
        if manifest_content:
            for js_fp in generated_files.keys():
                if js_fp.startswith("static/src/js/") and js_fp.endswith((".js", ".ts")):
                    if js_fp not in manifest_content and Path(js_fp).name not in manifest_content:
                        issues.append(f"Manifest assets dict is missing JS dashboard file: {js_fp}")
            for xml_fp in generated_files.keys():
                if xml_fp.startswith("static/src/xml/") and xml_fp.endswith(".xml"):
                    if xml_fp not in manifest_content and Path(xml_fp).name not in manifest_content:
                        issues.append(f"Manifest assets dict is missing QWeb XML template file: {xml_fp}")

        return len(issues) == 0, issues

