# forge/agents/codebase_agent.py
"""
OdooCode — Agentic Codebase Reader & Modifier
Like MiMoCode but specialized for Odoo module development.
Reads files chunk by chunk, understands context, and makes intelligent changes.
"""
import os, re, ast, json
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from collections import defaultdict
import logging

logger = logging.getLogger("OdooCode.CodebaseAgent")

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class FileChunk:
    """A chunk of a file with context."""
    filepath: str
    start_line: int
    end_line: int
    content: str
    chunk_index: int
    total_chunks: int
    context_before: str = ""
    context_after: str = ""

@dataclass
class FileAnalysis:
    """Analysis result for a single file."""
    filepath: str
    file_type: str  # python, xml, csv, manifest, init
    models: List[str] = field(default_factory=list)
    fields: List[Dict] = field(default_factory=list)
    methods: List[Dict] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    views: List[Dict] = field(default_factory=list)
    security: List[Dict] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    total_lines: int = 0
    summary: str = ""

@dataclass
class CodebaseAnalysis:
    """Complete codebase analysis."""
    root_path: str
    files: Dict[str, FileAnalysis] = field(default_factory=dict)
    models: Dict[str, List[str]] = field(default_factory=dict)  # model_name -> [files]
    dependencies: Set[str] = field(default_factory=set)
    structure: Dict[str, List[str]] = field(default_factory=dict)  # directory -> [files]
    issues: List[str] = field(default_factory=list)
    summary: str = ""

@dataclass
class ModificationRequest:
    """A request to modify code."""
    filepath: str
    description: str
    search_text: Optional[str] = None
    replace_text: Optional[str] = None
    line_range: Optional[Tuple[int, int]] = None
    context: str = ""

@dataclass
class ModificationResult:
    """Result of a modification."""
    filepath: str
    success: bool
    old_content: str
    new_content: str
    changes_made: int
    description: str

# =============================================================================
# FILE READER — Chunk-by-chunk reading
# =============================================================================

class FileReader:
    """Reads files chunk by chunk with context preservation."""

    def __init__(self, chunk_size: int = 200, overlap: int = 50):
        self.chunk_size = chunk_size
        self.overlap = overlap

    def read_file(self, filepath: str) -> str:
        """Read entire file content."""
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except Exception as e:
            logger.error(f"Failed to read {filepath}: {e}")
            return ""

    def read_chunks(self, filepath: str) -> List[FileChunk]:
        """Read file in chunks with overlap for context."""
        content = self.read_file(filepath)
        if not content:
            return []

        lines = content.split('\n')
        total_lines = len(lines)
        chunks = []

        for i in range(0, total_lines, self.chunk_size - self.overlap):
            end = min(i + self.chunk_size, total_lines)
            chunk_lines = lines[i:end]
            chunk_content = '\n'.join(chunk_lines)

            # Context before (previous overlap)
            context_before_start = max(0, i - self.overlap)
            context_before = '\n'.join(lines[context_before_start:i]) if i > 0 else ""

            # Context after (next overlap)
            context_after_end = min(total_lines, end + self.overlap)
            context_after = '\n'.join(lines[end:context_after_end]) if end < total_lines else ""

            chunk = FileChunk(
                filepath=filepath,
                start_line=i + 1,
                end_line=end,
                content=chunk_content,
                chunk_index=len(chunks),
                total_chunks=0,  # Will be set after
                context_before=context_before,
                context_after=context_after
            )
            chunks.append(chunk)

        # Set total chunks
        for chunk in chunks:
            chunk.total_chunks = len(chunks)

        return chunks

    def read_range(self, filepath: str, start: int, end: int) -> str:
        """Read specific line range."""
        content = self.read_file(filepath)
        if not content:
            return ""
        lines = content.split('\n')
        return '\n'.join(lines[start-1:end])

# =============================================================================
# CODEBASE ANALYZER
# =============================================================================

class CodebaseAnalyzer:
    """Analyzes Odoo codebases chunk by chunk."""

    def __init__(self):
        self.reader = FileReader()

    def analyze_file(self, filepath: str) -> FileAnalysis:
        """Analyze a single file."""
        content = self.reader.read_file(filepath)
        if not content:
            return FileAnalysis(filepath=filepath, file_type="unknown")

        file_type = self._detect_file_type(filepath, content)
        analysis = FileAnalysis(filepath=filepath, file_type=file_type)
        analysis.total_lines = len(content.split('\n'))

        if file_type == "python":
            analysis = self._analyze_python(filepath, content, analysis)
        elif file_type == "xml":
            analysis = self._analyze_xml(filepath, content, analysis)
        elif file_type == "csv":
            analysis = self._analyze_csv(filepath, content, analysis)
        elif file_type == "manifest":
            analysis = self._analyze_manifest(filepath, content, analysis)

        return analysis

    def analyze_codebase(self, root_path: str) -> CodebaseAnalysis:
        """Analyze entire codebase."""
        codebase = CodebaseAnalysis(root_path=root_path)

        for root, dirs, files in os.walk(root_path):
            # Skip hidden and cache directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']

            for fname in sorted(files):
                filepath = os.path.join(root, fname)
                rel_path = os.path.relpath(filepath, root_path).replace('\\', '/')

                analysis = self.analyze_file(filepath)
                codebase.files[rel_path] = analysis

                # Track structure
                dir_name = os.path.dirname(rel_path) or "."
                if dir_name not in codebase.structure:
                    codebase.structure[dir_name] = []
                codebase.structure[dir_name].append(rel_path)

                # Track models
                for model in analysis.models:
                    if model not in codebase.models:
                        codebase.models[model] = []
                    codebase.models[model].append(rel_path)

                # Track dependencies
                codebase.dependencies.update(analysis.dependencies)

        # Generate summary
        codebase.summary = self._generate_summary(codebase)

        return codebase

    def _detect_file_type(self, filepath: str, content: str) -> str:
        """Detect file type."""
        fname = os.path.basename(filepath)

        if fname == "__manifest__.py":
            return "manifest"
        elif fname == "__init__.py":
            return "init"
        elif filepath.endswith(".py"):
            return "python"
        elif filepath.endswith(".xml"):
            return "xml"
        elif filepath.endswith(".csv"):
            return "csv"
        elif filepath.endswith(".js") or filepath.endswith(".ts"):
            return "javascript"
        return "unknown"

    def _analyze_python(self, filepath: str, content: str, analysis: FileAnalysis) -> FileAnalysis:
        """Analyze Python file."""
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            analysis.issues.append(f"Syntax error: {e}")
            return analysis

        for node in ast.walk(tree):
            # Import analysis
            if isinstance(node, ast.Import):
                for alias in node.names:
                    analysis.imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    analysis.imports.append(node.module)

            # Class analysis
            if isinstance(node, ast.ClassDef):
                # Check if it's a model
                for bases in node.bases:
                    base_str = ast.dump(bases)
                    if 'models.Model' in base_str or 'models.TransientModel' in base_str:
                        # Find _name attribute
                        for item in node.body:
                            if isinstance(item, ast.Assign):
                                for target in item.targets:
                                    if isinstance(target, ast.Constant) and target.value == '_name':
                                        if isinstance(item.value, ast.Constant):
                                            analysis.models.append(item.value.value)

                # Method analysis
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        analysis.methods.append({
                            'name': item.name,
                            'line': item.lineno,
                            'decorators': [ast.dump(d) for d in item.decorator_list]
                        })

        # Field analysis (simple regex for now)
        field_pattern = re.compile(r'(\w+)\s*=\s*fields\.(\w+)\(')
        for match in field_pattern.finditer(content):
            analysis.fields.append({
                'name': match.group(1),
                'type': match.group(2)
            })

        return analysis

    def _analyze_xml(self, filepath: str, content: str, analysis: FileAnalysis) -> FileAnalysis:
        """Analyze XML file."""
        # Simple regex-based analysis
        record_pattern = re.compile(r'<record\s+model="([^"]+)"\s+id="([^"]+)"')
        for match in record_pattern.finditer(content):
            analysis.views.append({
                'model': match.group(1),
                'id': match.group(2)
            })

        # Check for deprecated patterns
        if '<tree' in content:
            analysis.issues.append("Uses deprecated <tree> tag")
        if 'attrs=' in content:
            analysis.issues.append("Uses deprecated attrs= attribute")
        if re.search(r'states\s*=\s*["\']', content):
            analysis.issues.append("Uses deprecated states= attribute")

        return analysis

    def _analyze_csv(self, filepath: str, content: str, analysis: FileAnalysis) -> FileAnalysis:
        """Analyze CSV file."""
        lines = content.strip().split('\n')
        if lines:
            header = lines[0]
            if 'model_id:id' in header:
                # This is an access rights file
                for line in lines[1:]:
                    parts = line.split(',')
                    if len(parts) >= 3:
                        model_id = parts[2]
                        if model_id.startswith('model_'):
                            model_name = model_id[6:].replace('_', '.')
                            analysis.models.append(model_name)
        return analysis

    def _analyze_manifest(self, filepath: str, content: str, analysis: FileAnalysis) -> FileAnalysis:
        """Analyze manifest file."""
        try:
            manifest = ast.literal_eval(content)
            if isinstance(manifest, dict):
                analysis.dependencies = manifest.get('depends', [])
                analysis.summary = manifest.get('summary', '')
        except Exception:
            analysis.issues.append("Failed to parse manifest")
        return analysis

    def _generate_summary(self, codebase: CodebaseAnalysis) -> str:
        """Generate codebase summary."""
        total_files = len(codebase.files)
        total_models = len(codebase.models)
        total_issues = sum(len(f.issues) for f in codebase.files.values())

        summary_parts = [
            f"Codebase at {codebase.root_path}",
            f"Total files: {total_files}",
            f"Models found: {total_models}",
            f"Issues found: {total_issues}",
            f"Dependencies: {', '.join(codebase.dependencies) if codebase.dependencies else 'None'}"
        ]

        return "\n".join(summary_parts)

# =============================================================================
# CODEBASE AGENT — The intelligent agent
# =============================================================================

class CodebaseAgent:
    """
    Agentic codebase reader and modifier for Odoo.
    Like MiMoCode but specialized for Odoo module development.
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client
        self.analyzer = CodebaseAnalyzer()
        self.reader = FileReader()
        self.current_codebase: Optional[CodebaseAnalysis] = None
        self.modification_history: List[ModificationResult] = []

    def load_codebase(self, path: str) -> CodebaseAnalysis:
        """Load and analyze a codebase."""
        print(f"  [odoo.info]Analyzing codebase:[/odoo.info] {path}")
        self.current_codebase = self.analyzer.analyze_codebase(path)
        print(f"  [odoo.success]✓[/odoo.success] Found {len(self.current_codebase.files)} files, {len(self.current_codebase.models)} models")
        return self.current_codebase

    def read_file(self, filepath: str) -> str:
        """Read a file with chunk-by-chunk support."""
        if self.current_codebase and filepath in self.current_codebase.files:
            full_path = os.path.join(self.current_codebase.root_path, filepath)
        else:
            full_path = filepath

        return self.reader.read_file(full_path)

    def read_file_chunks(self, filepath: str) -> List[FileChunk]:
        """Read a file in chunks."""
        if self.current_codebase and filepath in self.current_codebase.files:
            full_path = os.path.join(self.current_codebase.root_path, filepath)
        else:
            full_path = filepath

        return self.reader.read_chunks(full_path)

    def get_model_files(self, model_name: str) -> List[str]:
        """Get all files related to a model."""
        if not self.current_codebase:
            return []
        return self.current_codebase.models.get(model_name, [])

    def get_file_analysis(self, filepath: str) -> Optional[FileAnalysis]:
        """Get analysis for a specific file."""
        if not self.current_codebase:
            return None
        return self.current_codebase.files.get(filepath)

    def get_issues(self) -> List[str]:
        """Get all issues found in the codebase."""
        if not self.current_codebase:
            return []
        issues = []
        for filepath, analysis in self.current_codebase.files.items():
            for issue in analysis.issues:
                issues.append(f"{filepath}: {issue}")
        return issues

    def suggest_fixes(self) -> List[Dict]:
        """Suggest fixes for issues found."""
        issues = self.get_issues()
        suggestions = []

        for issue in issues:
            if "deprecated <tree>" in issue:
                suggestions.append({
                    'issue': issue,
                    'fix': 'Replace <tree> with <list>',
                    'priority': 'high'
                })
            elif "deprecated attrs=" in issue:
                suggestions.append({
                    'issue': issue,
                    'fix': 'Replace attrs= with direct boolean expressions',
                    'priority': 'high'
                })
            elif "deprecated states=" in issue:
                suggestions.append({
                    'issue': issue,
                    'fix': 'Replace states= with invisible=',
                    'priority': 'high'
                })
            elif "Syntax error" in issue:
                suggestions.append({
                    'issue': issue,
                    'fix': 'Fix Python syntax error',
                    'priority': 'critical'
                })

        return suggestions

    def modify_file(self, filepath: str, search_text: str, replace_text: str) -> ModificationResult:
        """Modify a file with search/replace."""
        if self.current_codebase and filepath in self.current_codebase.files:
            full_path = os.path.join(self.current_codebase.root_path, filepath)
        else:
            full_path = filepath

        old_content = self.reader.read_file(full_path)
        if not old_content:
            return ModificationResult(
                filepath=filepath,
                success=False,
                old_content="",
                new_content="",
                changes_made=0,
                description="File not found or empty"
            )

        # Count occurrences
        changes_made = old_content.count(search_text)

        # Replace
        new_content = old_content.replace(search_text, replace_text)

        # Write back
        try:
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(new_content)

            result = ModificationResult(
                filepath=filepath,
                success=True,
                old_content=old_content,
                new_content=new_content,
                changes_made=changes_made,
                description=f"Replaced {changes_made} occurrence(s)"
            )
            self.modification_history.append(result)
            return result

        except Exception as e:
            return ModificationResult(
                filepath=filepath,
                success=False,
                old_content=old_content,
                new_content=new_content,
                changes_made=0,
                description=f"Write failed: {e}"
            )

    def apply_search_replace_blocks(self, filepath: str, blocks: List[Dict]) -> ModificationResult:
        """Apply multiple search/replace blocks to a file."""
        if self.current_codebase and filepath in self.current_codebase.files:
            full_path = os.path.join(self.current_codebase.root_path, filepath)
        else:
            full_path = filepath

        old_content = self.reader.read_file(full_path)
        if not old_content:
            return ModificationResult(
                filepath=filepath,
                success=False,
                old_content="",
                new_content="",
                changes_made=0,
                description="File not found"
            )

        new_content = old_content
        total_changes = 0

        for block in blocks:
            search = block.get('search', '')
            replace = block.get('replace', '')

            if search in new_content:
                new_content = new_content.replace(search, replace, 1)
                total_changes += 1

        # Write back
        try:
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(new_content)

            result = ModificationResult(
                filepath=filepath,
                success=True,
                old_content=old_content,
                new_content=new_content,
                changes_made=total_changes,
                description=f"Applied {total_changes} search/replace blocks"
            )
            self.modification_history.append(result)
            return result

        except Exception as e:
            return ModificationResult(
                filepath=filepath,
                success=False,
                old_content=old_content,
                new_content=new_content,
                changes_made=total_changes,
                description=f"Write failed: {e}"
            )

    def get_context_for_file(self, filepath: str) -> str:
        """Get comprehensive context for a file."""
        if not self.current_codebase:
            return ""

        analysis = self.current_codebase.files.get(filepath)
        if not analysis:
            return ""

        context_parts = [
            f"File: {filepath}",
            f"Type: {analysis.file_type}",
            f"Lines: {analysis.total_lines}",
            f"Models: {', '.join(analysis.models) if analysis.models else 'None'}",
            f"Dependencies: {', '.join(analysis.dependencies) if analysis.dependencies else 'None'}",
        ]

        if analysis.issues:
            context_parts.append(f"Issues: {'; '.join(analysis.issues)}")

        # Add related files
        related_files = set()
        for model in analysis.models:
            for related in self.current_codebase.models.get(model, []):
                if related != filepath:
                    related_files.add(related)

        if related_files:
            context_parts.append(f"Related files: {', '.join(list(related_files)[:5])}")

        return "\n".join(context_parts)

    def get_modification_history(self) -> List[ModificationResult]:
        """Get history of all modifications."""
        return self.modification_history

    def rollback_last_modification(self) -> bool:
        """Rollback the last modification."""
        if not self.modification_history:
            return False

        last_mod = self.modification_history[-1]
        if last_mod.success:
            try:
                full_path = os.path.join(self.current_codebase.root_path, last_mod.filepath) \
                    if self.current_codebase else last_mod.filepath

                with open(full_path, 'w', encoding='utf-8') as f:
                    f.write(last_mod.old_content)

                self.modification_history.pop()
                return True
            except Exception:
                return False

        return False

# =============================================================================
# ODOO SPECIALIST — Domain knowledge
# =============================================================================

class OdooSpecialist:
    """Domain knowledge for Odoo development."""

    ODOO_18_PATTERNS = {
        'deprecated': {
            '<tree': 'Use <list> instead',
            'attrs=': 'Use direct boolean expressions',
            'states=': 'Use invisible= instead',
            '@api.multi': 'Removed in Odoo 14',
            '@api.one': 'Removed in Odoo 14',
            'group_operator': 'Use aggregator= instead',
        },
        'required_fields': {
            'Model': ['_name', '_description', '_order'],
            'Field': ['required=True for NOT NULL'],
        },
        'security': {
            'access': 'ir.model.access.csv for each model',
            'rules': 'ir.rule XML for multi-company',
        }
    }

    @classmethod
    def validate_odoo_code(cls, content: str, file_type: str) -> List[str]:
        """Validate Odoo code against best practices."""
        issues = []

        if file_type == 'python':
            # Check for deprecated patterns
            for pattern, suggestion in cls.ODOO_18_PATTERNS['deprecated'].items():
                if pattern in content:
                    issues.append(f"Deprecated: {pattern} - {suggestion}")

            # Check for missing _description
            if 'models.Model' in content and '_description' not in content:
                issues.append("Missing _description on Model class")

        elif file_type == 'xml':
            if '<tree' in content:
                issues.append("Use <list> instead of <tree>")
            if 'attrs=' in content:
                issues.append("attrs= is deprecated, use direct expressions")

        return issues
