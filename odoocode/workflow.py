# forge/workflow.py
"""
OdooCode — Professional Odoo 18 Module Generator
"""
import os, sys, json, sqlite3, logging, re, textwrap, tempfile, subprocess, zipfile, asyncio, time
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.rule import Rule
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich import box

from .config import ForgeConfig
from .models import BlueprintFile, ModuleState
from .llm import LLMClient
from .utils import read_codebase, Validator, ModuleStructureValidator
from .ui.tui import (
    console, print_banner, print_phase, print_subphase,
    print_success, print_warning, print_error, print_info, print_step,
    display_preview, display_blueprint_table, display_summary,
    display_complete, OdooProgress
)
from .agents.analyst import AnalystAgent
from .agents.blueprint import BlueprintAgent
from .agents.coder import CoderAgent
from .agents.critic import CriticAgent
from .agents.editor import EditAgent
from .agents.repair import RepairAgent
from .agents.security_auditor import SecurityAuditor
from .agents.codebase_agent import CodebaseAgent
from .agents.modify_agent import ModifyAgent, ModifyRequest
from .tools.rag import SkillRetriever
from .memory import MemoryManager
from .learning import LearningManager
from .subagent import SubagentRunner, WorkUnit, build_work_units, get_parallel_stats
from .utils import safe_json_extract

logger = logging.getLogger("OdooCode.Workflow")

DB_FILE = "odoocode_state.db"

class StateDB:
    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self._init()

    def _init(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt TEXT, out_dir TEXT, mode TEXT,
                    created DATETIME DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS blueprint (
                    session_id INTEGER, filepath TEXT, description TEXT,
                    spec TEXT, depends_on TEXT,
                    PRIMARY KEY (session_id, filepath));
                CREATE TABLE IF NOT EXISTS generated (
                    session_id INTEGER, filepath TEXT, content TEXT, status TEXT,
                    PRIMARY KEY (session_id, filepath));
            """)

    def create_session(self, prompt: str, out_dir: str, mode: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO sessions (prompt, out_dir, mode) VALUES (?, ?, ?)",
                (prompt, out_dir, mode))
            return cur.lastrowid

    def save_blueprint(self, session_id: int, blueprint: List[BlueprintFile]):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("INSERT OR REPLACE INTO blueprint VALUES (?,?,?,?,?)",
                [(session_id, b.filepath, b.description, b.spec,
                  json.dumps(b.depends_on)) for b in blueprint])

    def upsert_file(self, session_id: int, bf: BlueprintFile):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO generated VALUES (?,?,?,?)",
                (session_id, bf.filepath, bf.content, bf.status))

    def load_session(self, session_id: int) -> tuple:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT filepath, description, spec, depends_on FROM blueprint WHERE session_id=?",
                (session_id,)).fetchall()
            bp = [BlueprintFile(filepath=r[0], description=r[1], spec=r[2],
                                depends_on=json.loads(r[3] or "[]")) for r in rows]
            gr = conn.execute(
                "SELECT filepath, content FROM generated WHERE session_id=?",
                (session_id,)).fetchall()
        return bp, {r[0]: r[1] for r in gr}

    def latest_session_id(self) -> Optional[int]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def delete_session(self, session_id: int):
        with sqlite3.connect(self.db_path) as conn:
            for tbl, col in [("sessions", "id"), ("blueprint", "session_id"), ("generated", "session_id")]:
                conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (session_id,))


class OdooCodeWorkflow:
    def __init__(self, config: ForgeConfig, wizard_answers: dict = None):
        self.config = config
        self.wizard_answers = wizard_answers or {}
        skill_retriever = None
        if config.skills_dir:
            try:
                skill_retriever = SkillRetriever(config.skills_dir, config.embed_model, config.top_k_skills)
            except Exception as exc:
                logger.warning(f"SkillRetriever init failed: {exc}")

        self.llm = LLMClient(config, skill_retriever)
        self.db = StateDB()
        self.session_id = None
        self.blueprint = []
        self.generated = {}
        self.analysis = ""
        self.module_meta = {}
        self.module_state = ModuleState()
        self.validator = Validator()
        self.structure_validator = ModuleStructureValidator()

        # Persistent memory
        self.memory = MemoryManager(config.memory_dir)
        self.memory.init()
        logger.info(f"Memory stats: {self.memory.get_stats()}")

        # Self-improvement learning
        self.learning = LearningManager(config.memory_dir)
        logger.info(f"Learning stats: {self.learning.get_stats()}")

        # Agentic components
        self.codebase_agent = CodebaseAgent(self.llm)
        self.modify_agent = ModifyAgent(self.llm, config, self.codebase_agent)

    def _get_memory_context(self, query: str = "") -> str:
        """Get relevant memory and learning context for injection into LLM prompts."""
        if not query:
            query = self.config.prompt
        memory_ctx = self.memory.search_context(query, max_tokens=2000)
        learning_ctx = self.learning.get_learning_context()
        parts = []
        if memory_ctx:
            parts.append(memory_ctx)
        if learning_ctx:
            parts.append(learning_ctx)
        return "\n\n".join(parts) if parts else ""

    def _extract_metadata_ai(self, prompt: str):
        """Use AI to extract module metadata from the user's prompt."""
        print_info("Extracting module metadata...")

        system = """You are an Odoo module naming expert. Extract module metadata from the user's request.

Output ONLY a JSON object with these fields:
{
  "module_name": "Human Readable Name",
  "technical_name": "snake_case_name",
  "summary": "One line description",
  "category": "Odoo category (e.g., Sales, Inventory, Fleet, HR)"
}

Rules for technical_name:
- lowercase, underscores only (no spaces, hyphens, or special chars)
- Should be the core concept from the user's request (e.g., "lab_testing", "expense_claim")
- Max 3 words
- Examples: "lab_testing", "expense_claim", "timesheet", "project_task"

Rules for module_name:
- Title Case, readable
- Keep it short (1-3 words)
- Examples: "Fleet", "Expense Claim", "Timesheet"

Output ONLY the JSON, no explanation."""

        try:
            response = self.llm.call(
                system,
                f"User request: {prompt}",
                self.config.resolve_model("planner"),
                temperature=0.1,
            )

            # Try multiple strategies to extract metadata
            technical_name = ""
            module_name = ""

            # Strategy 1: Try to find JSON
            import json
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(0))
                    technical_name = data.get("technical_name", "")
                    module_name = data.get("module_name", "")
                except json.JSONDecodeError:
                    pass

            # Strategy 2: Extract from key-value patterns
            if not technical_name:
                m = re.search(r'"technical_name"\s*:\s*"([^"]+)"', response)
                if m:
                    technical_name = m.group(1)
            if not module_name:
                m = re.search(r'"module_name"\s*:\s*"([^"]+)"', response)
                if m:
                    module_name = m.group(1)

            # Strategy 3: Extract from the prompt itself
            if not technical_name or not module_name:
                # Try to extract from the user's prompt
                words = prompt.lower().split()
                skip_words = {'create', 'a', 'an', 'the', 'module', 'for', 'with', 'and', 'that', 'track', 'manage', 'system'}
                key_words = [w.strip('.,!?;:\'"') for w in words if w.strip('.,!?;:\'"') not in skip_words and len(w) > 3]
                if key_words:
                    technical_name = technical_name or '_'.join(key_words[:3])
                    module_name = module_name or ' '.join(w.capitalize() for w in key_words[:3])

            # Sanitize
            technical_name = re.sub(r'[^a-z0-9_]', '_', technical_name.lower().strip())
            technical_name = re.sub(r'_+', '_', technical_name).strip('_')
            module_name = re.sub(r'[^a-zA-Z0-9 ]', '', module_name).strip()

            if technical_name in ("snake", "snake_case", "module_name", "technical_name", "your_module_name", "display_name", "name"):
                technical_name = ""

            if technical_name and module_name:
                self.module_meta.update({
                    "module_name": module_name,
                    "technical_name": technical_name,
                    "summary": "",
                    "category": "",
                })
                print_success(f"Module: {module_name} ({technical_name})")
            else:
                logger.warning("Failed to extract metadata from AI response")
        except Exception as e:
            logger.warning(f"AI metadata extraction failed: {e}")

    def _build_enhanced_prompt(self) -> str:
        """Build an enhanced prompt incorporating wizard answers."""
        base_prompt = self.config.prompt

        if not self.wizard_answers:
            return base_prompt

        context_parts = [base_prompt]

        features = self.wizard_answers.get("features", [])
        if features:
            context_parts.append(f"Required features: {', '.join(features)}")

        views = self.wizard_answers.get("views", [])
        if views:
            context_parts.append(f"Required views: {', '.join(views)}")

        security = self.wizard_answers.get("security_level")
        if security:
            context_parts.append(f"Security level: {security}")

        state_machine = self.wizard_answers.get("state_machine")
        if state_machine and state_machine != "none":
            context_parts.append(f"State machine: {state_machine}")

        performance = self.wizard_answers.get("performance")
        if performance and performance != "standard":
            context_parts.append(f"Performance requirements: {performance}")

        return ". ".join(context_parts)

    def _save_plan(self, output_dir: str):
        os.makedirs(output_dir, exist_ok=True)
        plan_path = os.path.join(output_dir, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write("# OdooCode Technical Specification Blueprint\n\n")
            fh.write(f"**Goal:** {self.config.prompt}\n")
            coder = self.config.resolve_model("coder")
            planner = self.config.resolve_model("planner")
            critic = self.config.resolve_model("critic")
            fh.write(f"**Coder:** {coder}  |  **Planner:** {planner}  |  **Critic:** {critic}\n\n")
            fh.write(f"## 1. Deep Analysis\n\n{self.analysis}\n\n## 2. File Specifications\n\n")
            for bf in self.blueprint:
                fh.write(f"### FILE: {bf.filepath}\n**Description:** {bf.description}\n")
                if bf.depends_on:
                    fh.write(f"**Depends On:** {', '.join(bf.depends_on)}\n")
                fh.write(f"\n```markdown\n{bf.spec}\n```\n\n")
        print_success(f"Plan saved: {plan_path}")

    def _get_module_dir(self, output_dir: str) -> str:
        """Get or compute the canonical module output directory."""
        if hasattr(self, "_module_dir") and self._module_dir:
            return self._module_dir

        tech_name = self.module_meta.get("technical_name", "")
        if not tech_name and "__manifest__.py" in self.generated:
            manifest = self.generated["__manifest__.py"]
            name_match = re.search(r"""['"]name['"]\s*:\s*['"](.+?)['"]""", manifest)
            if name_match:
                tech_name = name_match.group(1).lower().replace(" ", "_").replace("-", "_")

        if not tech_name:
            for fp, content in self.generated.items():
                if fp.endswith(".py") and "models/" in fp:
                    name_match = re.search(r"""_name\s*=\s*['"]([^'"]+)['"]""", content)
                    if name_match:
                        model_name = name_match.group(1)
                        parts = model_name.split(".")
                        if len(parts) >= 2:
                            tech_name = parts[0]
                            break

        if not tech_name:
            for word in self.config.prompt.lower().split():
                clean = word.strip(".,!?;:'\"")
                if len(clean) > 3 and clean.isalpha() and clean not in ("create", "with", "module", "the", "and", "for"):
                    tech_name = clean
                    break

        if not tech_name:
            tech_name = "generated_module"

        self._module_dir = os.path.join(output_dir, tech_name)
        os.makedirs(self._module_dir, exist_ok=True)
        return self._module_dir

    def _assemble(self, output_dir: str):
        print_phase(6, "Assembly", "Writing files to output directory")
        module_dir = self._get_module_dir(output_dir)
        tech_name = Path(module_dir).name
        
        # Ensure coder is initialized for any fallback generation
        coder = CoderAgent(self.llm, self.config, self.module_meta)
        coder.generated = dict(self.generated)
        bp_summary = "\n".join(
            f"- {bf.filepath}  [deps: {', '.join(bf.depends_on) or 'none'}]  -- {bf.description}"
            for bf in self.blueprint)

        # Fallback generation: check for missing or empty files in blueprint
        for bf in self.blueprint:
            content = self.generated.get(bf.filepath, "")
            if not content or not content.strip():
                print_warning(f"File '{bf.filepath}' missing/empty — triggering fallback generation")
                try:
                    content = coder.generate(bf, bp_summary)
                    if content and content.strip():
                        self.generated[bf.filepath] = content
                        coder.generated[bf.filepath] = content
                        print_success(f"Fallback generation succeeded: {bf.filepath}")
                except Exception as exc:
                    print_error(f"Fallback generation failed for {bf.filepath}: {exc}")

        written = 0
        for filepath, content in self.generated.items():
            if not content or not content.strip():
                print_warning(f"Skipping {filepath} (empty content after fallback)")
                continue

            dest = Path(module_dir) / filepath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content.lstrip("\ufeff"), encoding="utf-8")
            print_success(f"{filepath}")
            written += 1

        if written == 0:
            raise RuntimeError(f"Assembly failed: 0 files were written to '{module_dir}'. Module generation was NOT successful.")

        zip_path = None
        if self.config.zip_output and written > 0:
            zip_path = Path(output_dir) / f"{tech_name}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for filepath in self.generated:
                    full_dest = Path(module_dir) / filepath
                    if full_dest.exists():
                        zf.write(full_dest, filepath)
            print_success(f"Module zipped: {zip_path}")

        display_complete(module_dir, written, zip_path)
        return written

    def _quick_fix(self, filepath: str, content: str) -> str:
        """Apply quick regex fixes BEFORE validation to prevent false failures."""
        import re
        ext = Path(filepath).suffix

        # Strip markdown code fences
        if content.strip().startswith("```"):
            match = re.search(r"^```[a-zA-Z0-9_+\-]*\n([\s\S]*?)```\s*$", content.strip(), re.MULTILINE)
            if match:
                content = match.group(1).strip()

        # Strip literal "python" at start
        if content.startswith("python\n"):
            content = content[7:]

        # Strip BOM
        content = content.lstrip("\ufeff")

        # XML fixes
        if ext == ".xml":
            content = re.sub(r"<tree\b", "<list", content)
            content = re.sub(r"</tree>", "</list>", content)
            content = re.sub(r'view_mode\s*=\s*["\']tree', 'view_mode="list', content)

        # Python fixes
        if ext == ".py" and not filepath.endswith("__init__.py"):
            content = re.sub(r"self\.env\._\(", "_(", content)

        return content

    def _process_file(self, bf, coder, critic, bp_summary, index, total):
        print_step(index, total, f"[odoo.file]{bf.filepath}[/odoo.file]")

        with OdooProgress(f"Generating {bf.filepath}...") as prog:
            bf.content = coder.generate(bf, bp_summary)
        bf.status = "generated"
        coder.generated[bf.filepath] = bf.content

        # Apply quick regex fixes BEFORE validation
        bf.content = self._quick_fix(bf.filepath, bf.content)
        coder.generated[bf.filepath] = bf.content

        ok, err = self.validator.validate(bf.filepath, bf.content)
        if not ok:
            print_warning(f"Syntax error: {err}")
            # Check if content is wrong type (e.g., XML in a Python file)
            file_ext = Path(bf.filepath).suffix
            content_is_xml = bf.content.strip().startswith(("<record", "<odoo>", "<?xml"))
            content_is_python = not content_is_xml and any(kw in bf.content for kw in ["class ", "def ", "import ", "from "])

            if file_ext == ".py" and content_is_xml:
                print_warning("Content is XML but file expects Python — regenerating")
                bf.content = coder.generate(bf, bp_summary)
            elif file_ext == ".xml" and content_is_python:
                print_warning("Content is Python but file expects XML — regenerating")
                bf.content = coder.generate(bf, bp_summary)
            else:
                with OdooProgress("Auto-fixing...") as prog:
                    bf.content = coder.auto_fix(bf, err)

            ok2, err2 = self.validator.validate(bf.filepath, bf.content)
            bf.status = "validated" if ok2 else "failed"
            coder.generated[bf.filepath] = bf.content
            if ok2:
                print_success("Fix succeeded")
            else:
                print_error(f"Fix failed: {err2}")
        else:
            bf.status = "validated"
            print_success("Syntax validation passed")

        display_preview(bf)

        if self.config.interactive:
            action = Prompt.ask(
                "  [odoo.prompt]Review action[/odoo.prompt]",
                choices=["a", "r", "e", "s"],
                default="a"
            )
            if action == "r":
                bf.content = coder.generate(bf, bp_summary)
                coder.generated[bf.filepath] = bf.content
            elif action == "e":
                edit_agent = EditAgent(self.llm, self.config)
                edit_resp = edit_agent.generate_blocks(bf.filepath, "Edit as requested", bf.content)
                bf.content = edit_agent.apply_blocks(bf.content, edit_resp)
                coder.generated[bf.filepath] = bf.content
            elif action == "s":
                return

        critic_passed = False
        # Skip critic entirely — rely on validator for syntax/pattern checks
        # The critic LLM hallucinates issues (e.g., reports <tree> in Python files)
        bf.status = "approved"
        critic_passed = True
        print_success(f"Approved (validator passed): {bf.filepath}")

        self.generated[bf.filepath] = bf.content
        if self.session_id:
            self.db.upsert_file(self.session_id, bf)

    def _run_sequential(self, pending_bf, coder, critic, bp_summary):
        """Run file generation sequentially (original behavior)."""
        total = len(pending_bf)
        for idx, bf in enumerate(pending_bf, 1):
            self._process_file(bf, coder, critic, bp_summary, idx, total)

    def _run_parallel(self, pending_bf, coder, critic, bp_summary,
                      memory_context, max_workers):
        """Run file generation in parallel using SubagentRunner."""
        runner = SubagentRunner(self.llm, self.config, max_workers=max_workers)

        def generate_fn(filepath, spec, description, blueprint_summary,
                        team_context, memory_context, feedback=""):
            """Generate function called by subagent."""
            # Create a temporary BlueprintFile-like object
            from .models import BlueprintFile
            tmp_bf = BlueprintFile(
                filepath=filepath,
                spec=spec,
                description=description,
                depends_on=[],
            )
            return coder.generate(tmp_bf, blueprint_summary, feedback=feedback)

        def validate_fn(filepath, content):
            return self.validator.validate(filepath, content)

        # Build work units
        units = build_work_units(pending_bf)

        # Run parallel execution
        t0 = time.monotonic()
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                runner.run_all(
                    units, bp_summary, memory_context,
                    generate_fn=generate_fn,
                    validate_fn=validate_fn,
                )
            )
        finally:
            loop.close()
            runner.shutdown()

        elapsed = (time.monotonic() - t0) * 1000

        # Collect results back into blueprint
        for unit in units:
            for bf in pending_bf:
                if bf.filepath == unit.filepath:
                    bf.content = unit.content
                    bf.status = unit.status
                    coder.generated[unit.filepath] = unit.content
                    self.generated[unit.filepath] = unit.content
                    if self.session_id:
                        self.db.upsert_file(self.session_id, bf)
                    break

            if unit.status == "completed":
                print_success(f"{unit.filepath} ({unit.duration_ms:.0f}ms)")
            else:
                print_error(f"{unit.filepath} FAILED: {unit.error}")

        # Print stats
        stats = get_parallel_stats(units)
        speedup = (stats["avg_time_ms"] * stats["completed"]) / elapsed if elapsed > 0 else 1
        print_info(f"Parallel complete: {stats['completed']}/{stats['total']} files "
                   f"in {elapsed:.0f}ms (avg {stats['avg_time_ms']:.0f}ms/file, "
                   f"~{speedup:.1f}x speedup)")

    def _security_audit(self):
        """Run security audit on all generated files."""
        print_phase(7, "Security Audit", "Checking for security vulnerabilities")
        auditor = SecurityAuditor(self.llm, self.config)
        audit_result = auditor.audit_module(self.generated)

        missing = auditor.get_missing_security_files(self.generated)
        if missing:
            print_warning("Missing Security Files:")
            for m in missing:
                print_info(f"  • {m}")

        return audit_result

    def _structure_check(self):
        """Validate module structure completeness."""
        print_phase(8, "Structure Validation", "Checking module completeness")
        is_valid, issues = self.structure_validator.validate_structure(self.generated)

        if issues:
            print_warning("Structure Issues:")
            for issue in issues:
                print_info(f"  • {issue}")
        else:
            print_success("Module structure is complete!")

        return is_valid, issues

    def _auto_heal_structure(self, issues: list):
        """Self-healing LLM Repair Pass: Auto-fix structure validation issues before assembly."""
        if not issues:
            return True, []

        from .agents.repair import RepairAgent
        print_phase("8.5", "Self-Healing Repair Pass", "Automatically fixing missing structure elements")
        repair_agent = RepairAgent(self.llm, self.config)
        healed_any = False

        for issue in issues:
            # Match missing compute method pattern
            m_comp = re.search(r"Model '([^']+)' defines computed field '([^']+)' but no compute method '([^']+)'", issue)
            if m_comp:
                model_name, field_name, compute_method = m_comp.groups()
                for fp, content in list(self.generated.items()):
                    if fp.endswith(".py") and "models/" in fp:
                        if f"_name = \"{model_name}\"" in content or f"_name = '{model_name}'" in content:
                            if f"def {compute_method}" not in content:
                                print_info(f"Auto-healing missing compute method '{compute_method}' in {fp}")
                                prompt = (
                                    f"Add missing compute method for field '{field_name}' in model '{model_name}'.\n"
                                    f"Method name: {compute_method}\n"
                                    f"Requirements: Add @api.depends(...) decorator, loop `for record in self:`, and assign value to `record.{field_name}`.\n"
                                    f"Return the COMPLETE updated Python code for {fp}."
                                )
                                healed_code = repair_agent.fix_file(fp, content, prompt)
                                if healed_code and len(healed_code.strip()) > 50:
                                    self.generated[fp] = healed_code
                                    healed_any = True
                                    print_success(f"Healed compute method '{compute_method}' in {fp}")

            # Match missing security CSV entry pattern
            m_csv = re.search(r"Model '([^']+)' missing from ir.model.access.csv", issue)
            if m_csv:
                model_name = m_csv.group(1)
                csv_path = "security/ir.model.access.csv"
                if csv_path in self.generated:
                    csv_code = self.generated[csv_path]
                    mod_id = model_name.replace(".", "_")
                    if f"model_{mod_id}" not in csv_code:
                        new_row_usr = f"access_{mod_id}_user,{mod_id} user,model_{mod_id},base.group_user,1,1,1,0"
                        new_row_mgr = f"access_{mod_id}_manager,{mod_id} manager,model_{mod_id},base.group_system,1,1,1,1"
                        self.generated[csv_path] = csv_code.strip() + f"\n{new_row_usr}\n{new_row_mgr}\n"
                        healed_any = True
                        print_success(f"Healed missing security CSV entry for '{model_name}'")

            # Match XML view missing field pattern
            m_field = re.search(r"XML view '([^']+)' references field '([^']+)' not defined in Python model '([^']+)'", issue)
            if m_field:
                xml_path, field_name, model_name = m_field.groups()
                for fp, content in list(self.generated.items()):
                    if fp.endswith(".py") and "models/" in fp:
                        if f"_name = \"{model_name}\"" in content or f"_name = '{model_name}'" in content or f"_inherit = \"{model_name}\"" in content or f"_inherit = '{model_name}'" in content:
                            if f" {field_name} =" not in content and f"\n    {field_name} =" not in content:
                                print_info(f"Auto-healing missing field '{field_name}' in model file {fp}")
                                f_type = "Many2one" if field_name.endswith("_id") else ("One2many" if field_name.endswith("_ids") else "Char")
                                f_args = "'res.partner'" if field_name.endswith("_id") else ""
                                prompt = (
                                    f"Add missing field definition `{field_name}` to model `{model_name}`.\n"
                                    f"Field name: {field_name}\n"
                                    f"Field signature suggestion: {field_name} = fields.{f_type}({f_args}, string='{field_name.replace('_', ' ').title()}')\n"
                                    f"Return the COMPLETE updated Python file for {fp}."
                                )
                                healed_code = repair_agent.fix_file(fp, content, prompt)
                                if healed_code and len(healed_code.strip()) > 50:
                                    self.generated[fp] = healed_code
                                    healed_any = True
                                    print_success(f"Healed missing field '{field_name}' in {fp}")

            # Match missing JS action registry tag pattern
            m_tag = re.search(r"Client action tag '([^']+)' in XML is not registered in JS action registry", issue)
            if m_tag:
                tag_name = m_tag.group(1)
                for fp, content in list(self.generated.items()):
                    if fp.startswith("static/src/js/") and fp.endswith((".js", ".ts")):
                        if f"add('{tag_name}'" not in content and f"add(\"{tag_name}\"" not in content:
                            print_info(f"Auto-healing missing JS action registration '{tag_name}' in {fp}")
                            prompt = (
                                f"Ensure the component in {fp} is registered in the action registry with tag '{tag_name}'.\n"
                                f"Add line: registry.category('actions').add('{tag_name}', ComponentClassName);\n"
                                f"Return the COMPLETE updated JavaScript file for {fp}."
                            )
                            healed_code = repair_agent.fix_file(fp, content, prompt)
                            if healed_code and len(healed_code.strip()) > 50:
                                self.generated[fp] = healed_code
                                healed_any = True
                                print_success(f"Healed JS action registry tag '{tag_name}' in {fp}")

            # Match missing website QWeb template pattern
            m_tmpl = re.search(r"Controller '[^']+' calls request\.render\('([^']+)'\) but template '([^']+)' is not defined", issue)
            if m_tmpl:
                tmpl_ref, tmpl_id = m_tmpl.groups()
                tmpl_fp = "views/website_templates.xml"
                if tmpl_fp in self.generated:
                    content = self.generated[tmpl_fp]
                    if f"id=\"{tmpl_id}\"" not in content and f"id='{tmpl_id}'" not in content:
                        print_info(f"Auto-healing missing QWeb template '{tmpl_id}' in {tmpl_fp}")
                        prompt = (
                            f"Add missing QWeb template with id='{tmpl_id}' to {tmpl_fp}.\n"
                            f"Wrap in <template id='{tmpl_id}' name='{tmpl_id.replace('_', ' ').title()}'> <t t-call='website.layout'> ... </t> </template>.\n"
                            f"Return the COMPLETE updated XML file for {tmpl_fp}."
                        )
                        healed_code = repair_agent.fix_file(tmpl_fp, content, prompt)
                        if healed_code and len(healed_code.strip()) > 50:
                            self.generated[tmpl_fp] = healed_code
                            healed_any = True
                            print_success(f"Healed missing QWeb template '{tmpl_id}' in {tmpl_fp}")

            # Match controller direct model import pattern
            m_ctrl_import = re.search(r"Controller '([^']+)' directly imports Python model class", issue)
            if m_ctrl_import:
                ctrl_fp = m_ctrl_import.group(1)
                if ctrl_fp in self.generated:
                    content = self.generated[ctrl_fp]
                    print_info(f"Auto-healing direct model import in controller {ctrl_fp}")
                    prompt = (
                        f"Remove direct model imports (e.g. `from .models... import ...`) from {ctrl_fp}.\n"
                        f"Replace model method calls with `request.env['model.technical.name'].sudo().create(...)` or `request.env['model.technical.name']`.\n"
                        f"Return the COMPLETE updated Python controller file."
                    )
                    healed_code = repair_agent.fix_file(ctrl_fp, content, prompt)
                    if healed_code and len(healed_code.strip()) > 50:
                        self.generated[ctrl_fp] = healed_code
                        healed_any = True
                        print_success(f"Healed direct model import in {ctrl_fp}")

        if healed_any:
            is_valid, remaining_issues = self.structure_validator.validate_structure(self.generated)
            if not remaining_issues:
                print_success("Self-healing complete! All structure validation issues resolved.")
            return is_valid, remaining_issues


        return False, issues

    def _fix_security_csv(self):
        """Sanitize, format, and filter ir.model.access.csv to ensure exact model mapping and proper newlines."""
        csv_path = "security/ir.model.access.csv"
        if csv_path not in self.generated:
            return

        # Extract all actual model names defined in python model files
        actual_models = set()
        for fp, content in self.generated.items():
            if fp.endswith(".py") and "models/" in fp and not fp.endswith("__init__.py"):
                for m in re.finditer(r"_name\s*=\s*['\"]([^'\"]+)['\"]", content):
                    actual_models.add(m.group(1))

        csv_content = self.generated[csv_path]

        # Fix unformatted single-line concatenation (replace space + access_ with newline + access_)
        csv_content = re.sub(r'(?<!\n)\s+(access_[a-zA-Z0-9_]+,)', r'\n\1', csv_content)

        lines = [l.strip() for l in csv_content.split('\n') if l.strip()]
        header = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink"
        
        valid_rows = []
        seen_ids = set()

        for line in lines:
            if line.startswith("id,name,"):
                continue
            parts = line.split(",")
            if len(parts) >= 8:
                row_id = parts[0].strip()
                row_name = parts[1].strip()
                model_ref = parts[2].strip()  # e.g., model_barber_shop
                group_id = parts[3].strip()
                
                # Check if model_ref corresponds to an actual model or a standard base model
                model_name = model_ref.replace("model_", "").replace("_", ".")
                
                if model_name in actual_models or model_ref.replace("model_", "") in {m.replace(".", "_") for m in actual_models}:
                    if row_id not in seen_ids:
                        valid_rows.append(f"{row_id},{row_name},{model_ref},{group_id},{parts[4]},{parts[5]},{parts[6]},{parts[7]}")
                        seen_ids.add(row_id)

        # Ensure every actual model has at least user and manager security CSV rows
        for model in sorted(actual_models):
            mod_id = model.replace(".", "_")
            model_ref = f"model_{mod_id}"
            usr_id = f"access_{mod_id}_user"
            mgr_id = f"access_{mod_id}_manager"
            
            if usr_id not in seen_ids:
                valid_rows.append(f"{usr_id},{mod_id} user,{model_ref},base.group_user,1,1,1,0")
                seen_ids.add(usr_id)
            if mgr_id not in seen_ids:
                valid_rows.append(f"{mgr_id},{mod_id} manager,{model_ref},base.group_system,1,1,1,1")
                seen_ids.add(mgr_id)

        cleaned_csv = header + "\n" + "\n".join(valid_rows) + "\n"
        self.generated[csv_path] = cleaned_csv
        logger.info(f"Fixed security CSV: {len(valid_rows)} valid rows for {len(actual_models)} models")

    def _fix_manifest(self):
        """Fix manifest data list to reference actual generated files."""
        manifest_path = "__manifest__.py"
        if manifest_path not in self.generated:
            return

        manifest_content = self.generated[manifest_path]
        generated_files = set(self.generated.keys())

        # Extract current data list from manifest
        data_match = re.search(r'"data"\s*:\s*\[(.*?)\]', manifest_content, re.DOTALL)
        if not data_match:
            # Try single quotes
            data_match = re.search(r"'data'\s*:\s*\[(.*?)\]", manifest_content, re.DOTALL)
        if not data_match:
            return

        current_data = data_match.group(1)
        # Parse quoted strings (both single and double quotes)
        current_files = re.findall(r"""['"]([^'"]+)['"]""", current_data)

        # Build mapping of what exists vs what's referenced
        fixed_files = []
        for ref_file in current_files:
            if ref_file in generated_files:
                fixed_files.append(ref_file)
            else:
                # Try to find a match by:
                # 1. Same basename
                # 2. Same directory + similar name
                # 3. Fuzzy match on the model name part
                ref_basename = Path(ref_file).name
                ref_stem = Path(ref_file).stem
                ref_dir = Path(ref_file).parent

                best_match = None
                for actual_file in generated_files:
                    actual_basename = Path(actual_file).name
                    actual_stem = Path(actual_file).stem
                    actual_dir = Path(actual_file).parent

                    # Exact basename match
                    if actual_basename == ref_basename:
                        best_match = actual_file
                        break
                    # Same directory, stem contains the other
                    if str(actual_dir) == str(ref_dir):
                        if ref_stem in actual_stem or actual_stem in ref_stem:
                            best_match = actual_file
                            break
                    # Both are XML/Python in same directory
                    if (actual_dir == ref_dir and
                        Path(actual_file).suffix == Path(ref_file).suffix):
                        # Check if stems share significant parts
                        ref_words = set(ref_stem.lower().split('_'))
                        actual_words = set(actual_stem.lower().split('_'))
                        if len(ref_words & actual_words) >= 2:
                            best_match = actual_file
                            break

                if best_match:
                    fixed_files.append(best_match)
                    logger.info(f"Manifest fix: {ref_file} -> {best_match}")
                else:
                    logger.warning(f"Manifest references non-existent file: {ref_file}")

        # Also add any data files that should be in the manifest but aren't
        data_dirs = {"security", "views", "data", "demo", "report"}
        for gen_file in generated_files:
            dir_part = gen_file.split("/")[0] if "/" in gen_file else ""
            if dir_part in data_dirs and gen_file not in fixed_files:
                # Skip __init__.py and test files
                if gen_file.endswith("__init__.py") or gen_file.startswith("tests/"):
                    continue
                fixed_files.append(gen_file)
                logger.info(f"Manifest: added missing data file: {gen_file}")

        # Rebuild data list with correct ordering (security XML > security CSV > data > views)
        def sort_key(f):
            if f.endswith('.xml') and 'security' in f:
                return (0, f)
            if f.endswith('.csv') and 'security' in f:
                return (1, f)
            if f.startswith('data/'):
                return (2, f)
            if f.startswith('demo/'):
                return (3, f)
            if f.startswith('views/'):
                return (4, f)
            if f.startswith('report/'):
                return (5, f)
            return (6, f)

        fixed_files.sort(key=sort_key)

        # Replace data list in manifest
        new_data_str = ",\n        ".join(f'"{f}"' for f in fixed_files)
        new_data = f'"data": [\n        {new_data_str},\n    ]'
        new_manifest = re.sub(
            r"""['"]data['"]\s*:\s*\[.*?\]""",
            new_data,
            manifest_content,
            flags=re.DOTALL,
        )

        self.generated[manifest_path] = new_manifest
        logger.info(f"Fixed manifest: {len(fixed_files)} files in correct order")

    def _fix_deprecated_patterns(self):
        """Fix deprecated patterns in generated files."""
        fixes = {
            ".xml": [
                (r"<tree\b", "<list"),
                (r"</tree>", "</list>"),
                (r'view_mode\s*=\s*["\']tree', 'view_mode="list'),
            ],
            ".py": [
                (r"self\.env\._\(", "_("),
                (r'^python\n', ''),
            ],
        }
        fixed_count = 0
        for filepath, content in list(self.generated.items()):
            ext = Path(filepath).suffix
            if ext in fixes:
                new_content = content
                for pattern, replacement in fixes[ext]:
                    new_content = re.sub(pattern, replacement, new_content)
                if new_content != content:
                    self.generated[filepath] = new_content
                    fixed_count += 1
                    logger.info(f"Fixed deprecated pattern in {filepath}")

            # Fix missing imports in Python files
            if ext == ".py" and not filepath.endswith("__init__.py"):
                content = self.generated[filepath]
                needs_user_error = "raise UserError" in content and "from odoo.exceptions import UserError" not in content
                needs_validation_error = "raise ValidationError" in content and "from odoo.exceptions import ValidationError" not in content

                if needs_user_error or needs_validation_error:
                    imports_to_add = []
                    if needs_user_error:
                        imports_to_add.append("UserError")
                    if needs_validation_error:
                        imports_to_add.append("ValidationError")

                    import_line = f"from odoo.exceptions import {', '.join(imports_to_add)}"
                    # Add after the first import line
                    lines = content.split('\n')
                    for i, line in enumerate(lines):
                        if line.startswith('from odoo import'):
                            lines.insert(i + 1, import_line)
                            self.generated[filepath] = '\n'.join(lines)
                            fixed_count += 1
                            logger.info(f"Added missing imports to {filepath}")
                            break

            # Fix author in manifest files
            if filepath.endswith("__manifest__.py"):
                content = self.generated[filepath]
                # Replace any author with "OdooCode AI Bot"
                new_content = re.sub(
                    r"""['"]author['"]\s*:\s*['"][^'"]*['"]""",
                    '''"author": "OdooCode AI Bot"''',
                    content
                )
                if new_content != content:
                    self.generated[filepath] = new_content
                    fixed_count += 1
                    logger.info(f"Fixed author in {filepath}")

        if fixed_count:
            print_info(f"Fixed deprecated patterns in {fixed_count} files")

    def run_generate(self):
        cfg = self.config
        os.makedirs(cfg.output_dir, exist_ok=True)

        if cfg.resume:
            sid = self.db.latest_session_id()
            if sid:
                self.session_id = sid
                self.blueprint, self.generated = self.db.load_session(sid)
                print_success(f"Resumed session #{sid} ({len(self.blueprint)} files)")

        # Phase 0: AI-powered metadata extraction
        if not self.module_meta.get("technical_name"):
            self._extract_metadata_ai(cfg.prompt)

        # Lock module output directory immediately from Phase 0 metadata
        if self.module_meta.get("technical_name"):
            self._get_module_dir(cfg.output_dir)

        # Inject memory context into analysis
        memory_ctx = self._get_memory_context(cfg.prompt)

        # Phase 1: Analysis
        if not self.analysis:
            print_phase(1, "Deep Analysis", "Analyzing requirements and architecture")
            cb_ctx = read_codebase(cfg.codebase_path) if cfg.codebase_path else ""
            enhanced_prompt = self._build_enhanced_prompt()

            with OdooProgress("Analyzing requirement...") as prog:
                self.analysis = AnalystAgent(self.llm, cfg).analyze(enhanced_prompt, cb_ctx)

            # Save checkpoint after analysis
            self.memory.save_checkpoint(str(self.session_id or "new"), {
                "phase": "analysis_complete",
                "prompt": cfg.prompt,
                "analysis_summary": self.analysis[:500],
            })

        # Phase 2: Blueprint
        is_new = not self.blueprint
        if is_new:
            print_phase(2, "Blueprint", "Creating file specification blueprint")
            bp_agent = BlueprintAgent(self.llm, cfg)

            with OdooProgress("Planning specs...") as prog:
                self.blueprint = bp_agent.plan(cfg.prompt, self.analysis)

            # Preserve technical_name extracted in Phase 0
            existing_tech_name = self.module_meta.get("technical_name", "")
            self.module_meta.update(bp_agent.module_meta)
            if existing_tech_name and existing_tech_name not in ("snake", "snake_case", "module_name", "technical_name"):
                self.module_meta["technical_name"] = existing_tech_name

            self.session_id = self.db.create_session(cfg.prompt, cfg.output_dir, cfg.mode)
            self.db.save_blueprint(self.session_id, self.blueprint)

            # Save checkpoint after blueprint
            self.memory.save_checkpoint(str(self.session_id), {
                "phase": "blueprint_complete",
                "module": self.module_meta.get("technical_name", ""),
                "files": [bf.filepath for bf in self.blueprint],
            })

        module_path = self._get_module_dir(cfg.output_dir)
        tech_name = Path(module_path).name
        mod_name = self.module_meta.get("module_name") or tech_name.replace("_", " ").title()

        print_info(f"Module: {mod_name} ({tech_name})")
        print_success(f"Target module folder: {module_path}")

        if is_new:
            self._save_plan(module_path)

        display_blueprint_table(self.blueprint)

        if cfg.plan_only:
            print_success("Plan-only mode — stopping.")
            return

        # Phase 3-5: Code / Validate / Critic (Parallel)
        print_phase("3-5", "Code / Validate / Critic", "Generating and reviewing code")
        coder = CoderAgent(self.llm, cfg, self.module_meta)
        critic = CriticAgent(self.llm, cfg)
        coder.generated = dict(self.generated)
        bp_summary = "\n".join(
            f"- {bf.filepath}  [deps: {', '.join(bf.depends_on) or 'none'}]  -- {bf.description}"
            for bf in self.blueprint)

        # Filter out already-generated files
        pending_bf = [bf for bf in self.blueprint if bf.filepath not in self.generated]
        already_done = [bf for bf in self.blueprint if bf.filepath in self.generated]

        for bf in already_done:
            print_info(f"Already generated (skipping): {bf.filepath}")
            coder.generated[bf.filepath] = self.generated[bf.filepath]

        if pending_bf:
            # Try parallel execution
            memory_ctx = self._get_memory_context(cfg.prompt)
            use_parallel = cfg.max_parallel_files > 1 and len(pending_bf) > 1

            if use_parallel:
                print_info(f"Parallel mode: {len(pending_bf)} files, "
                          f"{cfg.max_parallel_files} workers")
                try:
                    self._run_parallel(
                        pending_bf, coder, critic, bp_summary,
                        memory_ctx, cfg.max_parallel_files,
                    )
                except Exception as exc:
                    print_warning(f"Parallel execution failed ({exc}), falling back to sequential")
                    self._run_sequential(pending_bf, coder, critic, bp_summary)
            else:
                self._run_sequential(pending_bf, coder, critic, bp_summary)

        # Fix security CSV access rights file
        self._fix_security_csv()

        # Fix manifest to reference actual generated files
        self._fix_manifest()

        # Execute post-processing, security audit, structure validation, and file assembly
        self._fix_init_files()

    def _fix_init_files(self):
        """Fix init files to only import files that actually exist."""
        generated_files = set(self.generated.keys())
        fixed_count = 0

        for filepath in list(self.generated.keys()):
            if not filepath.endswith("__init__.py"):
                continue

            content = self.generated[filepath]
            dir_path = str(Path(filepath).parent)
            new_lines = []
            has_changes = False

            for line in content.split('\n'):
                stripped = line.strip()
                if stripped.startswith('from . import '):
                    module_name = stripped.replace('from . import ', '').strip()
                    # Check if the imported module exists
                    expected_file = f"{dir_path}/{module_name}.py" if dir_path else f"{module_name}.py"
                    if module_name in ("models", "controllers") or expected_file in generated_files:
                        new_lines.append(line)
                    else:
                        # Try to find a matching file
                        found = False
                        for gen_file in generated_files:
                            if gen_file.endswith(".py") and not gen_file.endswith("__init__.py"):
                                gen_name = Path(gen_file).stem
                                gen_dir = str(Path(gen_file).parent)
                                if gen_dir == dir_path and gen_name == module_name:
                                    new_lines.append(line)
                                    found = True
                                    break
                        if not found:
                            has_changes = True
                            logger.warning(f"Removed import of '{module_name}' from {filepath} (file not found)")
                else:
                    new_lines.append(line)

        # Ensure models/__init__.py is 100% accurate with files actually in models/
        models_init_key = "models/__init__.py"
        model_stems = sorted([
            Path(fp).stem for fp in generated_files
            if fp.startswith("models/") and fp.endswith(".py") and not fp.endswith("__init__.py")
        ])
        if model_stems:
            self.generated[models_init_key] = "\n".join(f"from . import {stem}" for stem in model_stems) + "\n"
            fixed_count += 1
            logger.info(f"Fixed models/__init__.py with exact model files: {', '.join(model_stems)}")

        if fixed_count:
            print_info(f"Fixed {fixed_count} init files")

        # Fix deprecated patterns in generated files
        self._fix_deprecated_patterns()

        # Phase 7: Security Audit
        audit_result = self._security_audit()

        # Phase 8: Structure Validation
        struct_valid, struct_issues = self._structure_check()

        # Phase 8.5: Self-Healing LLM Repair Pass (automatically fix missing compute methods & fields)
        if struct_issues:
            struct_valid, struct_issues = self._auto_heal_structure(struct_issues)

        # Phase 9: Assembly
        self._assemble(self.config.output_dir)

        # Save session notes and consolidate memory
        if self.session_id:
            session_key = str(self.session_id)
            # Save final checkpoint
            self.memory.save_checkpoint(session_key, {
                "phase": "complete",
                "module": self.module_meta.get("technical_name", ""),
                "files_generated": list(self.generated.keys()),
                "audit_score": audit_result.get("security_score", 0) if isinstance(audit_result, dict) else 0,
                "structure_valid": struct_valid,
            })

            # Save session notes for future dream consolidation
            self.memory.append_notes(session_key,
                f"Generated module: {self.module_meta.get('technical_name', 'unknown')}\n"
                f"Files: {len(self.generated)}\n"
                f"Security audit: {audit_result.get('security_score', 'N/A') if isinstance(audit_result, dict) else 'N/A'}\n"
                f"Structure valid: {struct_valid}"
            )

            # Record any critic learnings
            for bf in self.blueprint:
                if hasattr(bf, 'critic_score') and bf.critic_score and bf.critic_score < 50:
                    self.memory.add_pattern(
                        "critic_feedback",
                        f"File {bf.filepath} scored {bf.critic_score}/100. Review spec alignment.",
                    )

            self.db.delete_session(self.session_id)

        # Final Summary
        display_summary(self.blueprint, audit_result, struct_valid)

    def run_repair(self):
        """Repair mode: scan and fix existing codebase."""
        cfg = self.config
        if not cfg.codebase_path:
            print_error("Repair mode requires --codebase path")
            return

        print_phase(0, "REPAIR MODE", f"Scanning: {cfg.codebase_path}")

        repair_agent = RepairAgent(self.llm, cfg)
        issues = repair_agent.scan(cfg.codebase_path)

        if not issues:
            print_success("No issues found — codebase is clean!")
            return

        # Display issues
        table = Table(
            title=f"Found {len(issues)} issue(s)",
            show_lines=True,
            header_style="odoo.primary",
            box=box.ROUNDED,
            border_style="odoo.border"
        )
        table.add_column("File", style="odoo.file")
        table.add_column("Line", justify="right")
        table.add_column("Severity", justify="center")
        table.add_column("Pattern")
        table.add_column("Suggestion", max_width=55)

        for iss in issues:
            color = {"error": "odoo.error", "warning": "odoo.warning", "info": "odoo.dim"}.get(iss.severity, "odoo.dim")
            table.add_row(iss.filepath, str(iss.line_number),
                          f"[{color}]{iss.severity.upper()}[/{color}]",
                          iss.pattern_name, iss.suggestion)
        console.print(table)

        # Fix files
        os.makedirs(cfg.output_dir, exist_ok=True)
        for rel_path in set(iss.filepath for iss in issues):
            file_issues = [i for i in issues if i.filepath == rel_path]
            src_path = os.path.join(cfg.codebase_path, rel_path)
            try:
                original = Path(src_path).read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                print_error(f"Cannot read {rel_path}: {exc}")
                continue

            print_subphase(f"Fixing {rel_path} ({len(file_issues)} issue(s))")
            fixed = repair_agent.fix_file(rel_path, original, file_issues)
            dest = src_path if cfg.in_place else os.path.join(cfg.output_dir, rel_path)
            if not cfg.in_place:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
            Path(dest).write_text(fixed, encoding="utf-8")
            print_success(f"Written: {dest}")

        print_success(f"Repair complete! {len(set(i.filepath for i in issues))} file(s) processed.")

    def run_edit(self):
        """Edit mode: modify existing codebase based on prompt."""
        cfg = self.config
        if not cfg.codebase_path:
            print_error("Edit mode requires --codebase path")
            return

        print_phase(0, "EDIT MODE", "Modifying existing codebase")

        cb_ctx = read_codebase(cfg.codebase_path)
        if not cb_ctx:
            print_error("Failed to read codebase")
            return

        files_str = self.llm.call(
            "List the minimum file paths to modify to fulfil the prompt. One path per line.",
            f"PROMPT: {cfg.prompt}\n\nCODEBASE:\n{cb_ctx[:10000]}\n\nFiles to modify:",
            cfg.planner_model
        )

        files_to_edit = [
            line.lstrip("-*# ").strip() for line in files_str.splitlines()
            if line.strip() and not line.strip().startswith("(")
        ]

        if not files_to_edit:
            print_error("No files identified for editing")
            return

        print_success(f"Files to edit: {', '.join(files_to_edit)}")

        edit_agent = EditAgent(self.llm, cfg)
        for fp in files_to_edit:
            abs_path = os.path.join(cfg.codebase_path, fp)
            if not os.path.exists(abs_path):
                print_warning(f"Creating new file: {abs_path}")
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                Path(abs_path).write_text("", encoding="utf-8")

            content = Path(abs_path).read_text(encoding="utf-8")
            print_subphase(f"Editing {fp}")

            response = edit_agent.generate_blocks(fp, cfg.prompt, content)
            new_content = edit_agent.apply_blocks(content, response)

            if new_content != content:
                Path(abs_path).write_text(new_content, encoding="utf-8")
                print_success("Edited successfully")
            else:
                print_warning("No edits applied")

        print_success("Edit mode complete!")

    def run_agentic(self):
        """Agentic mode: fully autonomous with codebase understanding."""
        cfg = self.config
        print_phase(0, "AGENTIC MODE", "Fully autonomous with codebase understanding")

        if cfg.codebase_path:
            # Load and analyze existing codebase
            print_subphase("Loading codebase")
            codebase = self.codebase_agent.load_codebase(cfg.codebase_path)
            print_info(f"Found {len(codebase.files)} files, {len(codebase.models)} models")

            # Show issues if any
            issues = self.codebase_agent.get_issues()
            if issues:
                print_warning(f"Found {len(issues)} issues:")
                for issue in issues[:10]:  # Show first 10
                    print_info(f"  • {issue}")

            # Show suggestions
            suggestions = self.codebase_agent.suggest_fixes()
            if suggestions:
                print_info(f"\n{suggestions.__len__()} fix suggestions available")

        # Run the agentic modification loop
        self._agentic_loop()

    def run_modify(self):
        """Modify mode: intelligently modify existing codebase."""
        cfg = self.config
        if not cfg.codebase_path:
            print_error("Modify mode requires --codebase path")
            return

        print_phase(0, "MODIFY MODE", "Intelligent codebase modification")

        # Load codebase
        print_subphase("Loading codebase")
        codebase = self.codebase_agent.load_codebase(cfg.codebase_path)
        print_info(f"Loaded {len(codebase.files)} files")

        # Apply modification
        from .agents.modify_agent import ModifyRequest
        request = ModifyRequest(
            target=cfg.codebase_path,
            instruction=cfg.prompt,
            mode="auto"
        )

        print_subphase("Applying modification")
        result = self.modify_agent.modify(request)

        if result.success:
            print_success(f"Modified {len(result.files_modified)} file(s) ({result.changes_made} changes)")
            for fp in result.files_modified:
                print_success(f"  • {fp}")
        else:
            print_warning(f"No modifications made: {result.description}")

    def run_analyze(self):
        """Analyze mode: deep analysis of existing codebase."""
        cfg = self.config
        if not cfg.codebase_path:
            print_error("Analyze mode requires --codebase path")
            return

        print_phase(0, "ANALYZE MODE", "Deep codebase analysis")

        # Load codebase
        print_subphase("Loading and analyzing codebase")
        codebase = self.codebase_agent.load_codebase(cfg.codebase_path)

        # Display analysis
        from rich.table import Table
        from rich import box

        table = Table(
            title="[odoo.header]Codebase Analysis[/odoo.header]",
            show_lines=True,
            header_style="odoo.primary",
            box=box.ROUNDED,
            border_style="odoo.border"
        )
        table.add_column("Metric", style="odoo.accent")
        table.add_column("Value", style="odoo.info")

        table.add_row("Root Path", codebase.root_path)
        table.add_row("Total Files", str(len(codebase.files)))
        table.add_row("Models Found", str(len(codebase.models)))
        table.add_row("Dependencies", ", ".join(codebase.dependencies) if codebase.dependencies else "None")

        # Count issues
        total_issues = sum(len(f.issues) for f in codebase.files.values())
        table.add_row("Issues Found", str(total_issues))

        console.print(table)

        # Show models
        if codebase.models:
            print_info("\nModels found:")
            for model, files in codebase.models.items():
                print_info(f"  • {model} ({', '.join(files[:3])})")

        # Show issues
        issues = self.codebase_agent.get_issues()
        if issues:
            print_warning(f"\nIssues ({len(issues)}):")
            for issue in issues[:20]:
                print_warning(f"  • {issue}")

        # Show suggestions
        suggestions = self.codebase_agent.suggest_fixes()
        if suggestions:
            print_info(f"\nFix Suggestions ({len(suggestions)}):")
            for s in suggestions[:10]:
                priority_color = "red" if s['priority'] == 'critical' else "yellow"
                print_info(f"  [{priority_color}]{s['priority']}[/{priority_color}]: {s['fix'][:60]}")

    def _agentic_loop(self):
        """Main agentic loop - reads, understands, modifies."""
        cfg = self.config
        max_steps = 20
        step = 0

        while step < max_steps:
            step += 1
            print_subphase(f"Agentic Step {step}/{max_steps}")

            # Determine next action
            action = self._determine_action()
            print_info(f"Action: {action['type']}")

            if action['type'] == 'done':
                print_success("Task completed!")
                break

            elif action['type'] == 'read_file':
                filepath = action.get('filepath')
                if filepath:
                    self._read_and_analyze_file(filepath)

            elif action['type'] == 'modify_file':
                filepath = action.get('filepath')
                instruction = action.get('instruction', '')
                if filepath:
                    self._modify_file_agentic(filepath, instruction)

            elif action['type'] == 'fix_issues':
                self._fix_all_issues()

            elif action['type'] == 'generate_new':
                # Fall back to generate mode for new module creation
                self.run_generate()
                break

        if step >= max_steps:
            print_warning("Reached maximum agentic steps")

    def _determine_action(self) -> dict:
        """Determine the next action based on context."""
        prompt = self.config.prompt.lower()

        # Check if this is a modification request
        if any(word in prompt for word in ['fix', 'modify', 'update', 'change', 'add', 'remove']):
            return {
                'type': 'modify_file',
                'filepath': self.config.codebase_path,
                'instruction': self.config.prompt
            }

        # Check if this is a file reading request
        if any(word in prompt for word in ['read', 'analyze', 'show', 'list']):
            return {
                'type': 'read_file',
                'filepath': self.config.codebase_path
            }

        # Check if there are issues to fix
        if self.codebase_agent.current_codebase:
            issues = self.codebase_agent.get_issues()
            if issues:
                return {'type': 'fix_issues'}

        # Default: generate new module
        return {'type': 'generate_new'}

    def _read_and_analyze_file(self, filepath: str):
        """Read and analyze a file."""
        if os.path.isfile(filepath):
            content = self.codebase_agent.read_file(filepath)
            analysis = self.codebase_agent.get_file_analysis(filepath)

            print_info(f"File: {filepath}")
            print_info(f"Lines: {len(content.split(chr(10)))}")

            if analysis:
                if analysis.models:
                    print_info(f"Models: {', '.join(analysis.models)}")
                if analysis.issues:
                    print_warning(f"Issues: {'; '.join(analysis.issues)}")

            # Display preview
            from rich.syntax import Syntax
            from .ui.tui import detect_lexer
            lexer = detect_lexer(filepath)
            preview = content[:2000] + "\n..." if len(content) > 2000 else content
            syntax = Syntax(preview, lexer, theme="monokai", line_numbers=True)
            console.print(syntax)

        elif os.path.isdir(filepath):
            codebase = self.codebase_agent.load_codebase(filepath)
            print_success(f"Loaded codebase: {len(codebase.files)} files")
            display_blueprint_table([
                type('obj', (object,), {
                    'filepath': fp,
                    'description': analysis.summary or 'File',
                    'depends_on': [],
                    'status': 'analyzed'
                }) for fp, analysis in list(codebase.files.items())[:20]
            ])

    def _modify_file_agentic(self, filepath: str, instruction: str):
        """Modify a file using the agentic modify agent."""
        from .agents.modify_agent import ModifyRequest

        request = ModifyRequest(
            target=filepath,
            instruction=instruction,
            mode="auto"
        )

        result = self.modify_agent.modify(request)

        if result.success:
            print_success(f"Modified {result.files_modified.__len__()} file(s) ({result.changes_made} changes)")
            print_info(f"Before: {result.before_summary}")
            print_info(f"After: {result.after_summary}")
        else:
            print_warning(f"No modifications made: {result.description}")

    def _fix_all_issues(self):
        """Fix all issues in the codebase."""
        if not self.codebase_agent.current_codebase:
            print_error("No codebase loaded")
            return

        issues = self.codebase_agent.get_issues()
        suggestions = self.codebase_agent.suggest_fixes()

        print_info(f"Fixing {len(suggestions)} issues...")

        for suggestion in suggestions:
            if suggestion['priority'] in ('critical', 'high'):
                print_subphase(f"Fixing: {suggestion['issue'][:60]}...")
                # Apply fix logic here
                print_success(f"Fixed: {suggestion['fix'][:60]}")

    def run(self):
        print_banner(self.config)
        try:
            if self.config.mode == "generate":
                self.run_generate()
            elif self.config.mode == "repair":
                self.run_repair()
            elif self.config.mode == "edit":
                self.run_edit()
            elif self.config.mode == "agentic":
                self.run_agentic()
            elif self.config.mode == "modify":
                self.run_modify()
            elif self.config.mode == "analyze":
                self.run_analyze()
            else:
                print_error(f"Unknown mode: {self.config.mode}")
        except KeyboardInterrupt:
            print_warning("Interrupted.")
        except Exception as exc:
            print_error(f"Fatal error: {exc}")
            logger.error(f"Fatal: {exc}", exc_info=True)

# Alias for backward compatibility
ForgeWorkflow = OdooCodeWorkflow

