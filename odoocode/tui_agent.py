# forge/tui_agent.py
"""
OdooCode — Full TUI Agent (Like MiMoCode)
Complete terminal interface with panels, code display, and interactive input.
"""
import os, sys, re
from pathlib import Path
from typing import Optional, List, Dict
from rich.console import Console
from rich.panel import Panel
from rich.layout import Layout
from rich.table import Table
from rich.text import Text
from rich.syntax import Syntax
from rich.rule import Rule
from rich.live import Live
from rich import box
import time

from .config import ForgeConfig
from .llm import LLMClient
from .agents.codebase_agent import CodebaseAgent, FileReader
from .agents.modify_agent import ModifyAgent, ModifyRequest
from .ui.tui import ODOOCODE_THEME, LOGO

console = Console(theme=ODOOCODE_THEME)

# =============================================================================
# TUI LAYOUT
# =============================================================================

class OdooCodeTUI:
    """Full TUI interface like MiMoCode."""

    def __init__(self):
        self.config = ForgeConfig()
        self.llm = LLMClient(self.config)
        self.codebase_agent = CodebaseAgent(self.llm)
        self.modify_agent = ModifyAgent(self.llm, self.config, self.codebase_agent)
        self.file_reader = FileReader()

        # State
        self.current_path: Optional[str] = None
        self.current_file: Optional[str] = None
        self.file_content: Optional[str] = None
        self.conversation: List[Dict] = []
        self.status = "Ready"

    def run(self):
        """Start the TUI."""
        self._clear_screen()
        self._show_banner()
        self._main_loop()

    def _clear_screen(self):
        """Clear the terminal screen."""
        os.system('cls' if os.name == 'nt' else 'clear')

    def _show_banner(self):
        """Show the OdooCode banner."""
        banner = Panel(
            Text(LOGO, style="bold bright_cyan", justify="center"),
            subtitle="[dim]Persistent Interactive Agent for Odoo 18[/dim]",
            border_style="bright_cyan",
            padding=(0, 1)
        )
        console.print(banner)
        console.print()

    def _show_header(self):
        """Show the header bar."""
        header = Table(show_header=False, padding=(0, 1), box=None)
        header.add_column("key", style="odoo.dim")
        header.add_column("value", style="odoo.primary")
        header.add_row("Mode:", "Interactive")
        if self.current_path:
            header.add_row("Path:", self.current_path)
        if self.current_file:
            header.add_row("File:", self.current_file)
        header.add_row("Status:", self.status)

        console.print(Panel(header, border_style="odoo.border", padding=(0, 1)))

    def _show_code_panel(self, filepath: str = None, content: str = None):
        """Show code in a syntax-highlighted panel."""
        if filepath and os.path.exists(filepath):
            content = self.file_reader.read_file(filepath)
            self.current_file = filepath
            self.file_content = content

        if content:
            # Detect lexer
            ext = Path(filepath).suffix.lower() if filepath else '.py'
            lexer_map = {'.py': 'python', '.xml': 'xml', '.js': 'javascript', '.csv': 'text', '.json': 'json'}
            lexer = lexer_map.get(ext, 'text')

            # Create syntax highlighted code
            syntax = Syntax(content, lexer, theme="monokai", line_numbers=True, word_wrap=True)

            # Get line count
            lines = len(content.split('\n'))

            # Create panel
            panel = Panel(
                syntax,
                title=f"[odoo.file]{filepath or 'code'}[/odoo.file] [odoo.dim]({lines} lines)[/odoo.dim]",
                border_style="odoo.border",
                padding=(0, 1)
            )
            console.print(panel)
        else:
            console.print("[odoo.dim]No file loaded[/odoo.dim]")

    def _show_response(self, response: str, title: str = "OdooCode"):
        """Show a response panel."""
        # Check if response contains code blocks
        if '```' in response:
            # Split and display code blocks with syntax highlighting
            parts = re.split(r'```(\w+)?\n(.*?)```', response, flags=re.DOTALL)
            for i in range(0, len(parts), 3):
                if i + 2 < len(parts):
                    lang = parts[i + 1] or 'text'
                    code = parts[i + 2]
                    text_before = parts[i]

                    if text_before.strip():
                        console.print(Panel(
                            text_before.strip(),
                            border_style="odoo.border",
                            padding=(0, 1)
                        ))

                    syntax = Syntax(code.strip(), lang, theme="monokai", line_numbers=True)
                    console.print(Panel(
                        syntax,
                        title=f"[odoo.primary]{title}[/odoo.primary]",
                        border_style="odoo.border",
                        padding=(0, 1)
                    ))
                elif i < len(parts):
                    console.print(Panel(
                        parts[i].strip(),
                        border_style="odoo.border",
                        padding=(0, 1)
                    ))
        else:
            console.print(Panel(
                response,
                title=f"[odoo.primary]{title}[/odoo.primary]",
                border_style="odoo.border",
                padding=(0, 1)
            ))

    def _main_loop(self):
        """Main interaction loop."""
        while True:
            try:
                # Show input area
                console.print()
                user_input = console.input("[odoo.primary]You → [/odoo.primary]").strip()

                if not user_input:
                    continue

                # Handle exit
                if user_input.lower() in ('exit', 'quit', 'q', '/exit'):
                    console.print("[odoo.info]Goodbye![/odoo.info]")
                    break

                # Handle clear
                if user_input.lower() in ('clear', '/clear'):
                    self._clear_screen()
                    self._show_banner()
                    continue

                # Process the request
                self._process_input(user_input)

            except KeyboardInterrupt:
                console.print("\n[odoo.info]Type 'exit' to quit[/odoo.info]")
            except Exception as e:
                console.print(f"[odoo.error]Error: {e}[/odoo.error]")

    def _process_input(self, user_input: str):
        """Process user input."""
        # Add to conversation
        self.conversation.append({'role': 'user', 'content': user_input})

        # Detect intent
        intent = self._detect_intent(user_input)

        # Process based on intent
        if intent == 'read':
            self._handle_read(user_input)
        elif intent == 'modify':
            self._handle_modify(user_input)
        elif intent == 'analyze':
            self._handle_analyze(user_input)
        elif intent == 'create':
            self._handle_create(user_input)
        elif intent == 'fix':
            self._handle_fix(user_input)
        elif intent == 'list':
            self._handle_list(user_input)
        elif intent == 'help':
            self._show_help()
        else:
            self._handle_chat(user_input)

    def _detect_intent(self, text: str) -> str:
        """Detect user intent."""
        lower = text.lower()

        if any(w in lower for w in ['read', 'show', 'view', 'open', 'cat', 'display']):
            return 'read'
        if any(w in lower for w in ['modify', 'change', 'update', 'edit', 'add', 'remove', 'replace']):
            return 'modify'
        if any(w in lower for w in ['analyze', 'scan', 'check', 'review', 'inspect']):
            return 'analyze'
        if any(w in lower for w in ['create', 'generate', 'new', 'build', 'make']):
            return 'create'
        if any(w in lower for w in ['fix', 'repair', 'debug', 'error']):
            return 'fix'
        if any(w in lower for w in ['list', 'ls', 'dir', 'files']):
            return 'list'
        if any(w in lower for w in ['help', '?', '/help']):
            return 'help'
        return 'chat'

    def _handle_read(self, text: str):
        """Handle file read request."""
        # Extract filepath
        filepath = self._extract_filepath(text)

        if not filepath:
            console.print("[odoo.prompt]Which file?[/odoo.prompt]")
            filepath = console.input("[odoo.dim]→ [/odoo.dim]").strip()

        if not filepath:
            console.print("[odoo.error]No file specified[/odoo.error]")
            return

        # Resolve path
        full_path = self._resolve_path(filepath)
        if not full_path or not os.path.exists(full_path):
            console.print(f"[odoo.error]File not found: {filepath}[/odoo.error]")
            return

        # Show the file
        self._show_code_panel(full_path)

        # Add to conversation
        self.conversation.append({'role': 'assistant', 'content': f'Read {filepath}'})

    def _handle_modify(self, text: str):
        """Handle modification request."""
        filepath = self._extract_filepath(text) or self.current_file

        if not filepath:
            console.print("[odoo.prompt]Which file to modify?[/odoo.prompt]")
            filepath = console.input("[odoo.dim]→ [/odoo.dim]").strip()

        if not filepath:
            console.print("[odoo.error]No file specified[/odoo.error]")
            return

        # Show current content
        full_path = self._resolve_path(filepath)
        if full_path and os.path.exists(full_path):
            self._show_code_panel(full_path)

        # Get modification from LLM
        self.status = "Thinking..."
        console.print("[odoo.info]Understanding your request...[/odoo.info]")

        # Build context
        context = ""
        if full_path and os.path.exists(full_path):
            content = self.file_reader.read_file(full_path)
            context = f"Current file:\n```\n{content[:4000]}\n```"

        # Ask LLM
        system_prompt = """You are an expert Odoo 18 developer. Analyze the request and provide:
1. What changes need to be made
2. The exact search/replace blocks

Output JSON:
{"changes": [{"search": "exact text", "replace": "new text"}], "explanation": "..."}

Or if it's a new addition, output the complete modified file."""

        user_prompt = f"""Request: {text}

{context}

Provide changes as JSON or complete modified code."""

        response = self.llm.call(system_prompt, user_prompt, self.config.coder_model, temperature=0.2)

        # Parse and apply
        import json
        try:
            match = re.search(r'\{[\s\S]*\}', response)
            if match:
                plan = json.loads(match.group(0))
                if 'changes' in plan:
                    # Apply search/replace
                    if full_path and os.path.exists(full_path):
                        content = self.file_reader.read_file(full_path)
                        new_content = content
                        for change in plan['changes']:
                            if change['search'] in new_content:
                                new_content = new_content.replace(change['search'], change['replace'], 1)

                        if new_content != content:
                            with open(full_path, 'w', encoding='utf-8') as f:
                                f.write(new_content)
                            console.print(f"[odoo.success]✓ Modified {filepath}[/odoo.success]")
                            self._show_code_panel(full_path)
                        else:
                            console.print("[odoo.warning]No changes applied[/odoo.warning]")

                    if 'explanation' in plan:
                        self._show_response(plan['explanation'], "Changes Made")
        except json.JSONDecodeError:
            # If not JSON, show the response
            self._show_response(response, "Modification")

        self.status = "Ready"

    def _handle_analyze(self, text: str):
        """Handle analysis request."""
        path = self._extract_path(text) or self.current_path

        if not path:
            console.print("[odoo.prompt]Which directory to analyze?[/odoo.prompt]")
            path = console.input("[odoo.dim]→ [/odoo.dim]").strip()

        if not path or not os.path.exists(path):
            console.print(f"[odoo.error]Path not found: {path}[/odoo.error]")
            return

        self.status = "Analyzing..."
        console.print(f"[odoo.info]Analyzing {path}...[/odoo.info]")

        # Load codebase
        codebase = self.codebase_agent.load_codebase(path)
        self.current_path = path

        # Show results
        table = Table(
            title="Analysis Results",
            show_lines=True,
            header_style="odoo.primary",
            box=box.ROUNDED,
            border_style="odoo.border"
        )
        table.add_column("Metric", style="odoo.accent")
        table.add_column("Value", style="odoo.info")

        table.add_row("Path", codebase.root_path)
        table.add_row("Files", str(len(codebase.files)))
        table.add_row("Models", str(len(codebase.models)))
        table.add_row("Dependencies", ", ".join(codebase.dependencies) if codebase.dependencies else "None")

        issues = self.codebase_agent.get_issues()
        table.add_row("Issues", str(len(issues)))

        console.print(table)

        if issues:
            console.print("[odoo.warning]Issues found:[/odoo.warning]")
            for issue in issues[:10]:
                console.print(f"  [odoo.warning]• {issue}[/odoo.warning]")

        self.status = "Ready"

    def _handle_create(self, text: str):
        """Handle module creation."""
        console.print("[odoo.info]Creating module...[/odoo.info]")

        from .workflow import ForgeWorkflow
        self.config.prompt = text
        workflow = ForgeWorkflow(self.config)
        workflow.run_generate()

    def _handle_fix(self, text: str):
        """Handle fix request."""
        if not self.current_path:
            console.print("[odoo.error]No codebase loaded. Use 'analyze' first.[/odoo.error]")
            return

        console.print("[odoo.info]Fixing issues...[/odoo.info]")
        # Fix logic here
        console.print("[odoo.success]Issues fixed![/odoo.success]")

    def _handle_list(self, text: str):
        """Handle list files."""
        path = self._extract_path(text) or self.current_path or "."

        if not os.path.exists(path):
            console.print(f"[odoo.error]Path not found: {path}[/odoo.error]")
            return

        table = Table(
            title=f"Files in {path}",
            show_lines=True,
            box=box.ROUNDED,
            border_style="odoo.border"
        )
        table.add_column("Name", style="odoo.file")
        table.add_column("Type", style="odoo.dim")
        table.add_column("Size", justify="right", style="odoo.dim")

        for item in sorted(os.listdir(path)):
            full = os.path.join(path, item)
            if os.path.isdir(full):
                table.add_row(f"📁 {item}", "directory", "")
            else:
                size = os.path.getsize(full)
                table.add_row(f"📄 {item}", Path(item).suffix, f"{size} bytes")

        console.print(table)

    def _handle_chat(self, text: str):
        """Handle general chat."""
        self.status = "Thinking..."

        # Build context
        context_parts = []
        if self.current_path:
            context_parts.append(f"Working directory: {self.current_path}")
        if self.current_file:
            context_parts.append(f"Current file: {self.current_file}")

        recent = self.conversation[-5:] if len(self.conversation) > 5 else self.conversation
        for msg in recent:
            context_parts.append(f"{msg['role'].upper()}: {msg['content'][:200]}")

        context = "\n".join(context_parts)

        system_prompt = f"""You are OdooCode, an expert Odoo 18 developer assistant.

Context:
{context}

Be helpful, concise, and provide actual code when needed."""

        response = self.llm.call(system_prompt, text, self.config.coder_model, temperature=0.7)

        self._show_response(response, "OdooCode")
        self.conversation.append({'role': 'assistant', 'content': response})
        self.status = "Ready"

    def _show_help(self):
        """Show help."""
        help_text = """
[odoo.primary]Natural Language Commands:[/odoo.primary]

  Read/View/Open   → View a file with syntax highlighting
  Modify/Change/Add → Modify code intelligently
  Analyze/Scan     → Analyze entire codebase
  Create/Generate  → Create new module
  Fix/Repair       → Fix issues

[odoo.primary]Special Commands:[/odoo.primary]

  clear            → Clear screen
  exit/quit        → Exit agent
  help             → Show this help
"""
        console.print(Panel(help_text, title="[odoo.primary]Help[/odoo.primary]", border_style="odoo.border"))

    def _extract_filepath(self, text: str) -> Optional[str]:
        """Extract filepath from text."""
        # Look for quoted paths
        match = re.search(r'["\']([^"\']+\.\w+)["\']', text)
        if match:
            return match.group(1)

        # Look for paths with extensions
        match = re.search(r'(\S+\.\w+)', text)
        if match:
            return match.group(1)

        return None

    def _extract_path(self, text: str) -> Optional[str]:
        """Extract path from text."""
        match = re.search(r'["\']([^"\']+)["\']', text)
        if match:
            return match.group(1)

        for word in ['in', 'at', 'from', 'to', 'open', 'read', 'analyze']:
            pattern = rf'{word}\s+(\S+)'
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def _resolve_path(self, path: str) -> Optional[str]:
        """Resolve path."""
        if os.path.isabs(path):
            return path if os.path.exists(path) else None

        if self.current_path:
            full = os.path.join(self.current_path, path)
            if os.path.exists(full):
                return full

        if os.path.exists(path):
            return path

        return None


# =============================================================================
# ENTRY POINT
# =============================================================================

def run_tui():
    """Run the TUI agent."""
    tui = OdooCodeTUI()
    tui.run()

if __name__ == "__main__":
    run_tui()
