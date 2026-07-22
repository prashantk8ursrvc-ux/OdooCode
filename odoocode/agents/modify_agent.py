# forge/agents/modify_agent.py
"""
OdooCode — Intelligent Modification Agent
Reads files chunk by chunk and makes intelligent modifications.
"""
import os, re, json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from .base import BaseAgent
from .codebase_agent import CodebaseAgent, FileChunk, FileReader
from ..prompts import PromptLibrary
from ..utils import safe_json_extract

@dataclass
class ModifyRequest:
    """Request to modify a file or codebase."""
    target: str  # filepath, model name, or pattern
    instruction: str  # what to do
    context: str = ""
    mode: str = "auto"  # auto, search_replace, rewrite, fix

@dataclass
class ModifyResult:
    """Result of modification."""
    success: bool
    files_modified: List[str]
    changes_made: int
    description: str
    before_summary: str = ""
    after_summary: str = ""

class ModifyAgent(BaseAgent):
    """
    Intelligent modification agent that can:
    1. Read files chunk by chunk
    2. Understand code context
    3. Make targeted modifications
    4. Fix issues automatically
    5. Apply patterns from existing code
    """

    def __init__(self, llm, config, codebase_agent: CodebaseAgent = None):
        super().__init__(llm, config)
        self.codebase = codebase_agent or CodebaseAgent()
        self.file_reader = FileReader()
        self.modification_history = []

    def modify(self, request: ModifyRequest) -> ModifyResult:
        """Execute a modification request."""
        # Determine what to modify
        if os.path.isfile(request.target):
            return self._modify_file(request)
        elif os.path.isdir(request.target):
            return self._modify_directory(request)
        else:
            return self._modify_by_pattern(request)

    def _modify_file(self, request: ModifyRequest) -> ModifyResult:
        """Modify a single file."""
        filepath = request.target

        # Read the file
        content = self.file_reader.read_file(filepath)
        if not content:
            return ModifyResult(
                success=False,
                files_modified=[],
                changes_made=0,
                description=f"Could not read file: {filepath}"
            )

        # Analyze current state
        before_summary = self._analyze_content(content, filepath)

        # Get modification from LLM
        modified_content = self._get_modification(filepath, content, request)

        if modified_content and modified_content != content:
            # Write the modified content
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(modified_content)

                after_summary = self._analyze_content(modified_content, filepath)
                changes = self._count_changes(content, modified_content)

                return ModifyResult(
                    success=True,
                    files_modified=[filepath],
                    changes_made=changes,
                    description=f"Modified {filepath} ({changes} changes)",
                    before_summary=before_summary,
                    after_summary=after_summary
                )
            except Exception as e:
                return ModifyResult(
                    success=False,
                    files_modified=[],
                    changes_made=0,
                    description=f"Write failed: {e}"
                )

        return ModifyResult(
            success=False,
            files_modified=[],
            changes_made=0,
            description="No modifications needed or generated"
        )

    def _modify_directory(self, request: ModifyRequest) -> ModifyResult:
        """Modify all files in a directory."""
        directory = request.target
        files_modified = []
        total_changes = 0

        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']

            for fname in sorted(files):
                if fname.endswith(('.py', '.xml', '.csv')):
                    filepath = os.path.join(root, fname)
                    file_request = ModifyRequest(
                        target=filepath,
                        instruction=request.instruction,
                        context=request.context,
                        mode=request.mode
                    )
                    result = self._modify_file(file_request)
                    if result.success:
                        files_modified.extend(result.files_modified)
                        total_changes += result.changes_made

        return ModifyResult(
            success=len(files_modified) > 0,
            files_modified=files_modified,
            changes_made=total_changes,
            description=f"Modified {len(files_modified)} files ({total_changes} total changes)"
        )

    def _modify_by_pattern(self, request: ModifyRequest) -> ModifyResult:
        """Modify files matching a pattern."""
        if not self.codebase.current_codebase:
            return ModifyResult(
                success=False,
                files_modified=[],
                changes_made=0,
                description="No codebase loaded"
            )

        files_modified = []
        total_changes = 0

        # Find matching files
        pattern = request.target.lower()
        for filepath, analysis in self.codebase.current_codebase.files.items():
            if pattern in filepath.lower() or pattern in str(analysis.models).lower():
                file_request = ModifyRequest(
                    target=os.path.join(self.codebase.current_codebase.root_path, filepath),
                    instruction=request.instruction,
                    context=request.context,
                    mode=request.mode
                )
                result = self._modify_file(file_request)
                if result.success:
                    files_modified.extend(result.files_modified)
                    total_changes += result.changes_made

        return ModifyResult(
            success=len(files_modified) > 0,
            files_modified=files_modified,
            changes_made=total_changes,
            description=f"Modified {len(files_modified)} files matching '{request.target}'"
        )

    def _get_modification(self, filepath: str, content: str, request: ModifyRequest) -> Optional[str]:
        """Get modification from LLM."""
        # Build context
        file_context = self.codebase.get_context_for_file(filepath) if self.codebase.current_codebase else ""

        # Determine modification approach
        if request.mode == "search_replace":
            return self._search_replace_modification(filepath, content, request)
        elif request.mode == "rewrite":
            return self._rewrite_modification(filepath, content, request)
        elif request.mode == "fix":
            return self._fix_modification(filepath, content, request)
        else:
            # Auto mode - let LLM decide
            return self._auto_modification(filepath, content, request, file_context)

    def _auto_modification(self, filepath: str, content: str, request: ModifyRequest, context: str) -> Optional[str]:
        """Let LLM decide the best modification approach."""
        system_prompt = f"""You are an expert Odoo 18 developer modifying code.

FILE: {filepath}
CONTEXT: {context}

INSTRUCTION: {request.instruction}

Rules:
1. Read the existing code carefully
2. Make minimal, targeted changes
3. Preserve working code
4. Follow Odoo 18 best practices
5. Output the COMPLETE modified file content

Output ONLY the modified file content, no explanations."""

        user_prompt = f"""Current file content:
```python
{content[:5000]}
```

Instruction: {request.instruction}

Please modify this file according to the instruction."""

        response = self.llm.call(system_prompt, user_prompt, self.config.coder_model, temperature=0.1)

        # Clean up response
        if response:
            # Remove code fences if present
            if response.startswith('```'):
                lines = response.split('\n')
                lines = lines[1:]  # Remove opening fence
                if lines and lines[-1].strip() == '```':
                    lines = lines[:-1]  # Remove closing fence
                response = '\n'.join(lines)

            # Check if it's a valid modification
            if len(response) > 100 and response != content:
                return response

        return None

    def _search_replace_modification(self, filepath: str, content: str, request: ModifyRequest) -> Optional[str]:
        """Use search/replace approach."""
        system_prompt = """You are an expert Odoo 18 developer making targeted modifications.

Output ONLY search/replace blocks in this format:
<<<<<<< SEARCH
exact text to find
=======
replacement text
>>>>>>> REPLACE

Rules:
1. Search text must match exactly
2. Multiple blocks allowed
3. Preserve indentation"""

        user_prompt = f"""File: {filepath}

Content:
```
{content[:5000]}
```

Instruction: {request.instruction}

Generate search/replace blocks:"""

        response = self.llm.call(system_prompt, user_prompt, self.config.coder_model, temperature=0.1)

        if response:
            return self._apply_search_replace_blocks(content, response)

        return None

    def _apply_search_replace_blocks(self, content: str, blocks_text: str) -> str:
        """Apply search/replace blocks to content."""
        result = content

        # Parse blocks
        pattern = re.compile(r'<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE', re.DOTALL)
        for match in pattern.finditer(blocks_text):
            search = match.group(1)
            replace = match.group(2)
            if search in result:
                result = result.replace(search, replace, 1)

        return result

    def _rewrite_modification(self, filepath: str, content: str, request: ModifyRequest) -> Optional[str]:
        """Complete rewrite approach."""
        system_prompt = f"""You are an expert Odoo 18 developer rewriting a file.

FILE: {filepath}
INSTRUCTION: {request.instruction}

Rules:
1. Write complete, production-ready code
2. Follow Odoo 18 best practices
3. Include all necessary imports
4. Output ONLY the file content"""

        user_prompt = f"""Rewrite this file according to the instruction:

Current content:
```
{content[:5000]}
```

Instruction: {request.instruction}"""

        response = self.llm.call(system_prompt, user_prompt, self.config.coder_model, temperature=0.2)

        if response and len(response) > 100:
            # Clean up
            if response.startswith('```'):
                lines = response.split('\n')
                lines = lines[1:]
                if lines and lines[-1].strip() == '```':
                    lines = lines[:-1]
                response = '\n'.join(lines)
            return response

        return None

    def _fix_modification(self, filepath: str, content: str, request: ModifyRequest) -> Optional[str]:
        """Fix issues approach."""
        # First, analyze issues
        issues = OdooSpecialist.validate_odoo_code(content, self._detect_file_type(filepath))

        if not issues:
            return None

        system_prompt = f"""You are an expert Odoo 18 developer fixing code issues.

FILE: {filepath}
ISSUES FOUND:
{chr(10).join(f'- {issue}' for issue in issues)}

Rules:
1. Fix ALL issues listed
2. Make minimal changes
3. Preserve working code
4. Output ONLY the fixed file content"""

        user_prompt = f"""Fix these issues in the file:

Content:
```
{content[:5000]}
```

Issues:
{chr(10).join(f'- {issue}' for issue in issues)}"""

        response = self.llm.call(system_prompt, user_prompt, self.config.coder_model, temperature=0.1)

        if response and len(response) > 100:
            if response.startswith('```'):
                lines = response.split('\n')
                lines = lines[1:]
                if lines and lines[-1].strip() == '```':
                    lines = lines[:-1]
                response = '\n'.join(lines)
            return response

        return None

    def _detect_file_type(self, filepath: str) -> str:
        """Detect file type."""
        if filepath.endswith('.py'):
            return 'python'
        elif filepath.endswith('.xml'):
            return 'xml'
        elif filepath.endswith('.csv'):
            return 'csv'
        return 'unknown'

    def _analyze_content(self, content: str, filepath: str) -> str:
        """Analyze content and return summary."""
        lines = content.split('\n')
        file_type = self._detect_file_type(filepath)

        summary_parts = [f"{len(lines)} lines"]

        if file_type == 'python':
            # Count classes, methods
            classes = content.count('class ')
            methods = content.count('def ')
            summary_parts.append(f"{classes} classes, {methods} methods")

        elif file_type == 'xml':
            # Count records, views
            records = content.count('<record')
            summary_parts.append(f"{records} records")

        return ", ".join(summary_parts)

    def _count_changes(self, old: str, new: str) -> int:
        """Count number of changes between old and new content."""
        old_lines = old.split('\n')
        new_lines = new.split('\n')

        changes = 0
        max_lines = max(len(old_lines), len(new_lines))

        for i in range(max_lines):
            old_line = old_lines[i] if i < len(old_lines) else ""
            new_line = new_lines[i] if i < len(new_lines) else ""

            if old_line != new_line:
                changes += 1

        return changes

    def interactive_modify(self, filepath: str) -> ModifyResult:
        """Interactive modification with user input."""
        content = self.file_reader.read_file(filepath)
        if not content:
            return ModifyResult(
                success=False,
                files_modified=[],
                changes_made=0,
                description=f"Could not read file: {filepath}"
            )

        print(f"\nFile: {filepath}")
        print(f"Lines: {len(content.split(chr(10)))}")
        print("\nWhat would you like to do?")
        print("1. Fix all issues")
        print("2. Apply specific change")
        print("3. Rewrite section")
        print("4. Add new feature")
        print("5. Cancel")

        choice = input("\nEnter choice (1-5): ").strip()

        if choice == "1":
            request = ModifyRequest(
                target=filepath,
                instruction="Fix all Odoo 18 deprecation issues and best practice violations",
                mode="fix"
            )
        elif choice == "2":
            instruction = input("Describe the change: ").strip()
            request = ModifyRequest(
                target=filepath,
                instruction=instruction,
                mode="auto"
            )
        elif choice == "3":
            start_line = int(input("Start line: ").strip())
            end_line = int(input("End line: ").strip())
            instruction = input("How to rewrite this section: ").strip()
            request = ModifyRequest(
                target=filepath,
                instruction=f"Rewrite lines {start_line}-{end_line}: {instruction}",
                mode="rewrite"
            )
        elif choice == "4":
            instruction = input("Describe the new feature: ").strip()
            request = ModifyRequest(
                target=filepath,
                instruction=f"Add new feature: {instruction}",
                mode="auto"
            )
        else:
            return ModifyResult(
                success=False,
                files_modified=[],
                changes_made=0,
                description="Cancelled"
            )

        return self.modify(request)
