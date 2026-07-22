# forge/agents/blueprint.py
import re
import logging
from pathlib import Path
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, MofNCompleteColumn
from rich.rule import Rule
from .base import BaseAgent
from ..models import BlueprintFile
from ..utils import topological_sort
from ..prompts import PromptLibrary
from ..ui.tui import console, print_info, print_warning

logger = logging.getLogger("OdooCode.Blueprint")

class BlueprintAgent(BaseAgent):
    def __init__(self, llm, config):
        super().__init__(llm, config)
        self.module_meta: dict = {
            "module_name": "", "technical_name": "", "depends": "", "summary": ""}

    def _parse_meta(self, response: str):
        # Strategy 1: Look for [TAG]: value patterns
        for tag, key in {"MODULE_NAME": "module_name", "TECHNICAL_NAME": "technical_name",
                         "DEPENDS": "depends", "SUMMARY": "summary"}.items():
            m = re.search(rf"\[{tag}\]:\s*(.+)", response)
            if m:
                self.module_meta[key] = m.group(1).strip()

        # Strategy 2: Look for markdown-style headers
        if not self.module_meta["technical_name"]:
            m = re.search(r"(?:Technical name|technical_name)[:\s]+(\S+)", response, re.IGNORECASE)
            if m:
                self.module_meta["technical_name"] = m.group(1).strip()

        if not self.module_meta["module_name"]:
            m = re.search(r"(?:Module name|Display name|module_name)[:\s]+(.+?)(?:\n|$)", response, re.IGNORECASE)
            if m:
                self.module_meta["module_name"] = m.group(1).strip()

        # Strategy 3: Derive technical_name from module_name
        if not self.module_meta["technical_name"] and self.module_meta["module_name"]:
            self.module_meta["technical_name"] = (
                self.module_meta["module_name"].lower().replace(" ", "_").replace("-", "_"))

        # Strategy 4: Extract from FILEPATH entries (look for __manifest__.py content)
        if not self.module_meta["technical_name"]:
            # Find name in manifest-like content
            m = re.search(r"""['"]name['"]\s*:\s*['"](.+?)['"]""", response)
            if m:
                self.module_meta["module_name"] = m.group(1)
                self.module_meta["technical_name"] = m.group(1).lower().replace(" ", "_").replace("-", "_")

        # Sanitize technical_name: only lowercase letters, numbers, underscores
        if self.module_meta["technical_name"]:
            tn = self.module_meta["technical_name"]
            # Remove everything after | or ### (do NOT split on underscores!)
            tn = re.split(r'\||###', tn)[0]
            tn = re.sub(r'[^a-z0-9_]', '_', tn.lower().strip())
            tn = re.sub(r'_+', '_', tn).strip('_')
            self.module_meta["technical_name"] = tn

        # Sanitize module_name
        if self.module_meta["module_name"]:
            mn = self.module_meta["module_name"]
            mn = re.split(r'\||###', mn)[0]
            mn = re.sub(r'[^a-zA-Z0-9 ]', '', mn).strip()
            self.module_meta["module_name"] = mn

    def _parse_blueprint(self, response: str, analysis: str = "") -> list:
        self._parse_meta(response)
        
        result = []
        
        # Try multiple parsing strategies
        
        # Strategy 1: Look for [FILEPATH]: patterns
        filepath_pattern = re.compile(
            r'\[FILEPATH\]:\s*([^\n]+)',
            re.IGNORECASE
        )
        
        desc_pattern = re.compile(
            r'\[DESCRIPTION\]:\s*([^\n]+)',
            re.IGNORECASE
        )
        
        deps_pattern = re.compile(
            r'\[DEPENDS_ON\]:\s*([^\n]*)',
            re.IGNORECASE
        )
        
        # Find all FILEPATH entries
        filepath_matches = list(filepath_pattern.finditer(response))
        
        valid_extensions = ('.py', '.xml', '.csv', '.json', '.js', '.ts', '.css', '.scss')

        for i, fp_match in enumerate(filepath_matches):
            raw_path = fp_match.group(1).strip().replace("\\", "/").strip('"`\'():;,.#*')
            
            # Skip invalid path fragments or non-file strings
            if not raw_path or not (raw_path.endswith(valid_extensions) or raw_path in ("__manifest__.py", "__init__.py")):
                logger.warning(f"Discarded invalid blueprint filepath: {fp_match.group(1)}")
                continue

            # Look for description after this filepath
            desc = ""
            desc_match = desc_pattern.search(response, fp_match.end())
            if desc_match and (not filepath_matches or desc_match.start() < filepath_matches[i+1].start() if i+1 < len(filepath_matches) else True):
                desc = desc_match.group(1).strip()
            
            # Look for depends_on after this filepath
            deps = []
            deps_match = deps_pattern.search(response, fp_match.end())
            if deps_match and (not filepath_matches or deps_match.start() < filepath_matches[i+1].start() if i+1 < len(filepath_matches) else True):
                deps_str = deps_match.group(1).strip()
                if deps_str:
                    deps = [d.strip() for d in deps_str.split(",") if d.strip()]
            
            # Clean up path
            parts = raw_path.split("/")
            if len(parts) > 2 and parts[0] in ("addons", "custom_addons", "custom-addons"):
                raw_path = "/".join(parts[2:])
            elif len(parts) > 1 and parts[0] in (
                    self.module_meta.get("technical_name", ""), "module"):
                raw_path = "/".join(parts[1:])
            
            result.append(BlueprintFile(
                filepath=raw_path,
                description=desc,
                depends_on=deps,
            ))
        
        # Strategy 2: If no results, try to parse common Odoo module structure from Deep Analysis
        if not result:
            tech_name = self.module_meta.get("technical_name", "my_module")
            model_names = []
            source_text = analysis + "\n" + response
            for match in re.finditer(r'(?:model[:\s]+|_name\s*=\s*[\'"]?|class\s+)([a-z0-9_]+(?:\.[a-z0-9_]+)+)', source_text, re.IGNORECASE):
                mod_name = match.group(1).strip().lower()
                if mod_name not in model_names and not mod_name.startswith(('mail.', 'res.', 'ir.', 'sale.', 'account.')):
                    model_names.append(mod_name)
            
            # If no domain models found, use technical name
            if not model_names:
                model_names = [f"{tech_name}.{tech_name}"]
            
            common_files = [
                ("__manifest__.py", "Module manifest"),
                ("__init__.py", "Top-level package init"),
                ("models/__init__.py", "Models package init"),
            ]
            
            # Add model files based on domain models
            for model in model_names:
                file_name = model.split('.')[-1]  # Get last part
                common_files.append((f"models/{file_name}.py", f"{file_name.replace('_', ' ').title()} model"))
            
            # Add standard files
            common_files.extend([
                ("views/menu.xml", "Menu items and actions"),
                ("security/ir.model.access.csv", "Access rights"),
                ("security/security.xml", "Security groups and record rules"),
                ("data/sequence_data.xml", "Sequence data"),
            ])
            
            for filepath, desc in common_files:
                result.append(BlueprintFile(
                    filepath=filepath,
                    description=desc,
                ))
        
        return result

    def _validate_blueprint(self, blueprint: list, analysis: str = "") -> list:
        """Validate blueprint has all mandatory files. Add missing ones."""
        existing = {bf.filepath for bf in blueprint}
        missing = []

        # Check mandatory files
        mandatory = [
            ("__manifest__.py", "Module manifest"),
            ("__init__.py", "Top-level package init"),
            ("models/__init__.py", "Models package init"),
            ("security/ir.model.access.csv", "Access rights CSV"),
        ]

        for filepath, desc in mandatory:
            if filepath not in existing:
                missing.append(BlueprintFile(filepath=filepath, description=desc))
                console.print(f"[forge.warn]Missing mandatory file: {filepath}[/forge.warn]")

        # Check for views directory
        has_views = any(f.startswith("views/") for f in existing)
        if not has_views:
            missing.append(BlueprintFile(
                filepath="views/menu.xml",
                description="Menu items and window actions"))
            console.print("[forge.warn]Missing views/ directory[/forge.warn]")

        # Check for security XML
        has_sec_xml = any(f.startswith("security/") and f.endswith(".xml") for f in existing)
        if not has_sec_xml:
            missing.append(BlueprintFile(
                filepath="security/security.xml",
                description="Security groups and record rules"))
            console.print("[forge.warn]Missing security XML[/forge.warn]")

        # Extract models from analysis text if provided
        if analysis:
            extracted_stems = set()
            for match in re.finditer(r'(?:_name|Model|class)[:\s]+[\'"]?([a-z0-9_]+(?:\.[a-z0-9_]+)+)', analysis, re.IGNORECASE):
                full_model = match.group(1).lower()
                stem = full_model.split(".")[-1]
                if len(stem) > 2:
                    extracted_stems.add(stem)

            for stem in extracted_stems:
                py_file = f"models/{stem}.py"
                if py_file not in existing and py_file not in {m.filepath for m in missing}:
                    missing.append(BlueprintFile(filepath=py_file, description=f"Model definition for {stem}"))
                    console.print(f"[forge.warn]Missing model file: {py_file}[/forge.warn]")

                view_file = f"views/{stem}_views.xml"
                if view_file not in existing and view_file not in {m.filepath for m in missing}:
                    missing.append(BlueprintFile(filepath=view_file, description=f"Views for {stem}"))
                    console.print(f"[forge.warn]Missing view file: {view_file}[/forge.warn]")

        # Check for Frontend & Dashboard web assets (OWL 2 JS, QWeb XML, SCSS) ONLY if explicitly requested by prompt/analysis
        analysis_lower = (analysis or "").lower()
        if any(w in analysis_lower for w in ["dashboard", "analytics panel", "kpi board", "reporting widget", "interactive dashboard"]):
            dash_js = "static/src/js/dashboard.js"
            dash_xml = "static/src/xml/dashboard.xml"
            dash_scss = "static/src/scss/dashboard.scss"
            dash_view = "views/dashboard_views.xml"

            if dash_js not in existing and dash_js not in {m.filepath for m in missing}:
                missing.append(BlueprintFile(filepath=dash_js, description="OWL 2 JS Dashboard component"))
            if dash_xml not in existing and dash_xml not in {m.filepath for m in missing}:
                missing.append(BlueprintFile(filepath=dash_xml, description="QWeb Dashboard templates"))
            if dash_scss not in existing and dash_scss not in {m.filepath for m in missing}:
                missing.append(BlueprintFile(filepath=dash_scss, description="Custom SCSS styling"))
            if dash_view not in existing and dash_view not in {m.filepath for m in missing}:
                missing.append(BlueprintFile(filepath=dash_view, description="Dashboard Client Action definition"))

        # Check for Website, Controllers & Portal Templates whenever requested
        if any(w in analysis_lower for w in ["website", "portal", "controller", "registration page", "booking portal", "online booking", "url slug", "public page", "web route", "http route", "saas"]):
            ctrl_main = "controllers/main.py"
            ctrl_init = "controllers/__init__.py"
            web_template = "views/website_templates.xml"

            if ctrl_main not in existing and ctrl_main not in {m.filepath for m in missing}:
                missing.append(BlueprintFile(filepath=ctrl_main, description="Website Controllers and HTTP Routes (@http.route)"))
            if ctrl_init not in existing and ctrl_init not in {m.filepath for m in missing}:
                missing.append(BlueprintFile(filepath=ctrl_init, description="Controllers package init"))
            if web_template not in existing and web_template not in {m.filepath for m in missing}:
                missing.append(BlueprintFile(filepath=web_template, description="Frontend QWeb Website and Portal HTML Templates"))

        # Check each model file in existing/missing has a view file
        all_files = existing.union({m.filepath for m in missing})
        models = [f for f in all_files if f.startswith("models/") and f.endswith(".py")
                  and not f.endswith("__init__.py")]
        for model_file in models:
            model_name = Path(model_file).stem
            view_file = f"views/{model_name}_views.xml"
            if view_file not in all_files:
                missing.append(BlueprintFile(
                    filepath=view_file,
                    description=f"Views for {model_name}"))
                console.print(f"[forge.warn]Missing view file: {view_file}[/forge.warn]")

        if missing:
            blueprint.extend(missing)
            console.print(f"[forge.info]Added {len(missing)} missing mandatory files[/forge.info]")

        return blueprint

    def plan(self, prompt: str, analysis: str) -> list:
        console.print(Rule("[forge.phase]Phase 2 — Blueprint[/forge.phase]"))
        response  = self.llm.call(
            PromptLibrary.blueprint_system(analysis), f"Module request: {prompt}",
            self.config.resolve_model("planner"))
        blueprint = self._parse_blueprint(response, analysis)

        if not blueprint:
            console.print("[forge.warn]Blueprint parse failed — retrying strict...[/forge.warn]")
            strict_sys = (
                "Output file list ONLY:\n[FILEPATH]: <path>\n[DESCRIPTION]: <sentence>\n"
                "[DEPENDS_ON]: <comma-sep or blank>\nRepeat per file. End with ===. Zero prose.")
            blueprint = self._parse_blueprint(self.llm.call(
                strict_sys,
                f"Files for:\n{prompt}\n\nPrevious response:\n{response[:500]}\n"
                "Output ONLY the structured file list.",
                self.config.resolve_model("planner")), analysis)

        # Validate blueprint has all mandatory files and models from analysis
        blueprint = self._validate_blueprint(blueprint, analysis)

        console.print(f"\n[forge.phase]Spec planning for {len(blueprint)} files...[/forge.phase]")
        planned_so_far = []
        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                      MofNCompleteColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("Planning specs...", total=len(blueprint))
            for bf in blueprint:
                prog.update(task, description=f"Spec: {bf.filepath}")
                cross_file_ctx = ""
                if planned_so_far:
                    cross_file_ctx = (
                        "\n\nCROSS-FILE AWARENESS (already planned — your spec MUST be consistent):\n"
                        + "\n".join(f"  - {p}" for p in planned_so_far)
                        + "\nEnsure you reference the correct model names, field names, view IDs, "
                          "and XML IDs from those files."
                    )
                spec_user = (
                    f"MODULE GOAL:\n{prompt}\n\nMODULE ANALYSIS:\n{analysis[:4000]}\n\n"
                    f"Technical spec for ONLY:\nFILE: {bf.filepath}\n"
                    f"DESCRIPTION: {bf.description}\n"
                    f"{cross_file_ctx}\n\n"
                    "Provide ALL fields, records, views, code structures. Exhaustive. "
                    "No markdown fences, no intro/outro. NO raw code — plain English specs only.")
                skill_query = f"Odoo 18 {Path(bf.filepath).suffix.strip('.')} {bf.description}"
                bf.spec = self.llm.call(
                    PromptLibrary.spec_system(), spec_user, self.config.resolve_model("planner"),
                    skill_query=skill_query).strip()
                planned_so_far.append(f"{bf.filepath}: {bf.description}")
                prog.advance(task)

        return topological_sort(blueprint)
