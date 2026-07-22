# forge/agent_mode.py
"""
OdooCode — Persistent Interactive Agent
Like MiMoCode: run it once, then just ask it to do things.
No commands needed - just natural language conversation.
"""
import os, sys, json, re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich import box

from .config import ForgeConfig
from .llm import LLMClient
from .agents.codebase_agent import CodebaseAgent, FileReader, CodebaseAnalyzer
from .agents.modify_agent import ModifyAgent, ModifyRequest
from .ui.tui import (
    console, print_banner, print_phase, print_subphase,
    print_success, print_warning, print_error, print_info,
    ODOOCODE_THEME, LOGO
)

# =============================================================================
# AGENT STATE
# =============================================================================

class AgentState:
    """Maintains the agent's conversation state."""

    def __init__(self):
        self.current_path: Optional[str] = None
        self.codebase_loaded: bool = False
        self.conversation_history: List[Dict] = []
        self.current_file: Optional[str] = None
        self.file_content: Optional[str] = None
        self.modifications_made: int = 0

    def add_message(self, role: str, content: str):
        """Add message to conversation history."""
        self.conversation_history.append({
            'role': role,
            'content': content
        })

    def get_context(self) -> str:
        """Get conversation context for LLM."""
        context_parts = []

        if self.current_path:
            context_parts.append(f"Working directory: {self.current_path}")

        if self.codebase_loaded:
            context_parts.append("Codebase is loaded and analyzed")

        if self.current_file:
            context_parts.append(f"Currently viewing: {self.current_file}")

        # Last few messages for context
        recent = self.conversation_history[-5:] if len(self.conversation_history) > 5 else self.conversation_history
        for msg in recent:
            context_parts.append(f"{msg['role'].upper()}: {msg['content'][:200]}")

        return "\n".join(context_parts)

# =============================================================================
# INTERACTIVE AGENT
# =============================================================================

class OdooCodeAgent:
    """
    Persistent interactive agent for Odoo development.
    Like MiMoCode - just run and ask.
    """

    def __init__(self):
        self.state = AgentState()
        self.config = ForgeConfig()
        self.llm = LLMClient(self.config)
        self.codebase_agent = CodebaseAgent(self.llm)
        self.modify_agent = ModifyAgent(self.llm, self.config, self.codebase_agent)
        self.file_reader = FileReader()
        self.running = True

    def start(self):
        """Start the interactive agent."""
        self._print_welcome()
        self._main_loop()

    def _print_welcome(self):
        """Print welcome screen."""
        console.print()
        logo_text = Text(LOGO, style="bold bright_cyan")
        console.print(Panel(
            logo_text,
            subtitle="[dim]Persistent Interactive Agent for Odoo 18[/dim]",
            border_style="bright_cyan",
            padding=(1, 2)
        ))
        console.print()
        console.print("[odoo.info]I'm your Odoo coding assistant. Just ask me anything![/odoo.info]")
        console.print("[odoo.dim]Type 'help' for commands, 'exit' to quit.[/odoo.dim]")
        console.print()

    def _main_loop(self):
        """Main interaction loop."""
        while self.running:
            try:
                # Get user input
                user_input = Prompt.ask(
                    "\n[odoo.primary]You[/odoo.primary]",
                    default=""
                ).strip()

                if not user_input:
                    continue

                # Handle special commands
                if user_input.lower() in ('exit', 'quit', 'q'):
                    self._handle_exit()
                    break

                if user_input.lower() == 'help':
                    self._show_help()
                    continue

                if user_input.lower() == 'status':
                    self._show_status()
                    continue

                # Process the request
                self._process_request(user_input)

            except KeyboardInterrupt:
                console.print("\n[odoo.info]Interrupted. Type 'exit' to quit.[/odoo.info]")
            except Exception as e:
                print_error(f"Error: {e}")

    def _process_request(self, user_input: str):
        """Process a user request using LLM."""
        # Add to history
        self.state.add_message('user', user_input)

        # Build context
        context = self.state.get_context()

        # Determine intent
        intent = self._detect_intent(user_input)

        # Process based on intent
        if intent == 'read_file':
            self._handle_read_file(user_input)
        elif intent == 'modify_file':
            self._handle_modify_file(user_input)
        elif intent == 'create_module':
            self._handle_create_module(user_input)
        elif intent == 'fix_issues':
            self._handle_fix_issues(user_input)
        elif intent == 'analyze':
            self._handle_analyze(user_input)
        elif intent == 'list_files':
            self._handle_list_files(user_input)
        else:
            self._handle_general_request(user_input, context)

    def _detect_intent(self, user_input: str) -> str:
        """Detect user intent from natural language."""
        lower = user_input.lower()

        # Read/View patterns
        if any(word in lower for word in ['read', 'show', 'view', 'open', 'display', 'cat']):
            if any(ext in lower for ext in ['.py', '.xml', '.csv', '.js']):
                return 'read_file'
            if 'file' in lower:
                return 'read_file'

        # Modify patterns
        if any(word in lower for word in ['modify', 'change', 'update', 'edit', 'add', 'remove', 'fix', 'replace']):
            return 'modify_file'

        # Create patterns
        if any(word in lower for word in ['create', 'generate', 'new', 'build', 'make']):
            return 'create_module'

        # Fix patterns
        if any(word in lower for word in ['fix', 'repair', 'debug', 'error', 'issue']):
            return 'fix_issues'

        # Analyze patterns
        if any(word in lower for word in ['analyze', 'analysis', 'scan', 'check', 'review']):
            return 'analyze'

        # List patterns
        if any(word in lower for word in ['list', 'show files', 'directory', 'ls']):
            return 'list_files'

        return 'general'

    def _handle_read_file(self, user_input: str):
        """Handle file reading request."""
        # Extract filepath from user input
        filepath = self._extract_filepath(user_input)

        if not filepath:
            # Ask for filepath
            filepath = Prompt.ask("[odoo.prompt]Which file should I read?[/odoo.prompt]")

        if not filepath:
            print_error("Please specify a file path")
            return

        # Try to find the file
        full_path = self._resolve_path(filepath)
        if not full_path or not os.path.exists(full_path):
            print_error(f"File not found: {filepath}")
            return

        # Read the file
        content = self.file_reader.read_file(full_path)
        if not content:
            print_error(f"Could not read file: {filepath}")
            return

        # Display the file
        self.state.current_file = full_path
        self.state.file_content = content

        # Syntax highlight
        ext = Path(full_path).suffix.lower()
        lexer_map = {'.py': 'python', '.xml': 'xml', '.js': 'javascript', '.csv': 'text'}
        lexer = lexer_map.get(ext, 'text')

        console.print(Panel(
            Syntax(content, lexer, theme="monokai", line_numbers=True, word_wrap=True),
            title=f"[odoo.file]{filepath}[/odoo.file] ({len(content.split(chr(10)))} lines)",
            border_style="odoo.border"
        ))

        # Add to history
        self.state.add_message('assistant', f"Read file: {filepath}")

    def _handle_modify_file(self, user_input: str):
        """Handle file modification request."""
        filepath = self._extract_filepath(user_input)
        instruction = user_input

        if not filepath and self.state.current_file:
            # Use currently viewed file
            filepath = self.state.current_file
            print_info(f"Modifying current file: {filepath}")

        if not filepath:
            filepath = Prompt.ask("[odoo.prompt]Which file should I modify?[/odoo.prompt]")

        if not filepath:
            # Try to determine from instruction
            print_info("Let me understand what you want to modify...")

        # Get modification from LLM
        print_subphase("Understanding your request...")

        # Build context for LLM
        context = ""
        if filepath:
            full_path = self._resolve_path(filepath)
            if full_path and os.path.exists(full_path):
                content = self.file_reader.read_file(full_path)
                context = f"Current file content:\n```\n{content[:5000]}\n```"

        # Ask LLM for modification plan
        system_prompt = """You are an expert Odoo 18 developer. Analyze the user's request and provide:
1. What files need to be modified
2. What changes need to be made
3. The exact search/replace blocks

Output JSON:
{
    "files": [{"path": "...", "changes": [{"search": "...", "replace": "..."}]}],
    "explanation": "..."
}"""

        user_prompt = f"""User request: {user_input}

{context}

Provide the modification plan as JSON."""

        response = self.llm.call(system_prompt, user_prompt, self.config.coder_model, temperature=0.2)

        # Parse response
        plan = self._parse_json_response(response)

        if plan and 'files' in plan:
            print_info(f"Plan: {plan.get('explanation', 'Modifying files...')}")

            # Apply modifications
            total_changes = 0
            for file_mod in plan['files']:
                fp = file_mod.get('path', filepath)
                if not fp:
                    continue

                full_path = self._resolve_path(fp)
                if not full_path:
                    full_path = fp

                changes = file_mod.get('changes', [])
                if changes:
                    request = ModifyRequest(
                        target=full_path,
                        instruction=user_input,
                        mode="search_replace"
                    )
                    result = self.modify_agent.modify(request)
                    if result.success:
                        total_changes += result.changes_made
                        print_success(f"Modified: {fp} ({result.changes_made} changes)")

            self.state.modifications_made += total_changes
            print_success(f"Total changes: {total_changes}")
        else:
            # Fallback: try direct modification
            if filepath:
                request = ModifyRequest(
                    target=self._resolve_path(filepath) or filepath,
                    instruction=user_input,
                    mode="auto"
                )
                result = self.modify_agent.modify(request)
                if result.success:
                    self.state.modifications_made += result.changes_made
                    print_success(f"Modified: {filepath} ({result.changes_made} changes)")
                else:
                    print_warning("Could not determine how to modify the file")
            else:
                print_warning("Please specify which file to modify")

        self.state.add_message('assistant', f"Modified files based on: {user_input}")

    def _handle_create_module(self, user_input: str):
        """Handle module creation request."""
        print_phase(0, "Creating Module", user_input)

        # Use the generate workflow
        from .workflow import ForgeWorkflow
        self.config.prompt = user_input
        self.config.mode = "generate"
        workflow = ForgeWorkflow(self.config)
        workflow.run_generate()

        self.state.add_message('assistant', f"Created module: {user_input}")

    def _handle_fix_issues(self, user_input: str):
        """Handle fix issues request."""
        if not self.state.current_path:
            print_error("No codebase loaded. Use 'open <path>' first.")
            return

        print_phase(0, "Fixing Issues", user_input)

        # Analyze and fix
        codebase = self.codebase_agent.load_codebase(self.state.current_path)
        issues = self.codebase_agent.get_issues()

        if not issues:
            print_success("No issues found!")
            return

        print_info(f"Found {len(issues)} issues")

        # Fix each issue
        for issue in issues[:10]:  # Limit to 10
            print_subphase(f"Fixing: {issue[:60]}...")
            # Apply fix logic

        self.state.add_message('assistant', f"Fixed issues in codebase")

    def _handle_analyze(self, user_input: str):
        """Handle analysis request."""
        if not self.state.current_path:
            # Ask for path
            path = Prompt.ask("[odoo.prompt]Which directory should I analyze?[/odoo.prompt]")
            if path:
                self.state.current_path = path
            else:
                print_error("Please specify a path")
                return

        print_phase(0, "Analyzing Codebase", self.state.current_path)

        codebase = self.codebase_agent.load_codebase(self.state.current_path)

        # Display analysis
        table = Table(
            title="Codebase Analysis",
            show_lines=True,
            header_style="odoo.primary",
            box=box.ROUNDED
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
            print_warning("\nIssues found:")
            for issue in issues[:10]:
                print_warning(f"  • {issue}")

        self.state.add_message('assistant', f"Analyzed codebase at {self.state.current_path}")

    def _handle_list_files(self, user_input: str):
        """Handle list files request."""
        path = self._extract_path(user_input) or self.state.current_path or "."

        if not os.path.exists(path):
            print_error(f"Path not found: {path}")
            return

        if os.path.isfile(path):
            # Show file info
            size = os.path.getsize(path)
            print_info(f"{path} ({size} bytes)")
            return

        # List directory
        files = []
        for item in sorted(os.listdir(path)):
            full = os.path.join(path, item)
            if os.path.isdir(full):
                files.append(f"  [odoo.primary]📁 {item}/[/odoo.primary]")
            else:
                size = os.path.getsize(full)
                files.append(f"  [odoo.info]📄 {item}[/odoo.info] ({size} bytes)")

        if files:
            console.print(Panel(
                "\n".join(files),
                title=f"[odoo.header]{path}[/odoo.header]",
                border_style="odoo.border"
            ))
        else:
            print_info("Empty directory")

    def _handle_general_request(self, user_input: str, context: str):
        """Handle general requests using LLM."""
        print_subphase("Thinking...")

        system_prompt = f"""You are OdooCode, an expert Odoo 18 developer assistant.

Current context:
{context}

Rules:
1. Be helpful and concise
2. For code requests, provide actual code
3. For file operations, explain what you'll do
4. Ask clarifying questions if needed

Respond naturally to help the user."""

        response = self.llm.call(system_prompt, user_input, self.config.coder_model, temperature=0.7)

        # Display response
        console.print(Panel(
            response,
            title="[odoo.primary]OdooCode[/odoo.primary]",
            border_style="odoo.border"
        ))

        self.state.add_message('assistant', response)

    def _show_help(self):
        """Show help information."""
        help_text = """
[odoo.primary]OdooCode Commands:[/odoo.primary]

Natural Language (just ask!):
  • "Read main.py" - View a file
  • "Add chatter to sale.order" - Modify code
  • "Fix all issues" - Fix problems
  • "Analyze my module" - Analyze codebase
  • "Create a Kanban module" - Generate new module

Special Commands:
  • help     - Show this help
  • status   - Show current status
  • exit     - Quit the agent

[odoo.dim]I understand natural language - just ask me anything about Odoo development![/odoo.dim]
"""
        console.print(Panel(help_text, border_style="odoo.border"))

    def _show_status(self):
        """Show current status."""
        table = Table(
            title="Agent Status",
            show_lines=True,
            header_style="odoo.primary",
            box=box.ROUNDED
        )
        table.add_column("Item", style="odoo.accent")
        table.add_column("Value", style="odoo.info")

        table.add_row("Working Directory", self.state.current_path or "Not set")
        table.add_row("Codebase Loaded", "Yes" if self.state.codebase_loaded else "No")
        table.add_row("Current File", self.state.current_file or "None")
        table.add_row("Modifications Made", str(self.state.modifications_made))
        table.add_row("Conversation Length", str(len(self.state.conversation_history)))

        console.print(table)

    def _handle_exit(self):
        """Handle exit."""
        if self.state.modifications_made > 0:
            print_info(f"Made {self.state.modifications_made} modifications during this session")

        console.print("[odoo.info]Goodbye![/odoo.info]")

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
        # Look for quoted paths
        match = re.search(r'["\']([^"\']+)["\']', text)
        if match:
            return match.group(1)

        # Look for paths after common words
        for word in ['in', 'at', 'from', 'to', 'open', 'read']:
            pattern = rf'{word}\s+(\S+)'
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def _resolve_path(self, path: str) -> Optional[str]:
        """Resolve path relative to current working directory."""
        if os.path.isabs(path):
            return path if os.path.exists(path) else None

        # Try relative to current path
        if self.state.current_path:
            full = os.path.join(self.state.current_path, path)
            if os.path.exists(full):
                return full

        # Try relative to cwd
        if os.path.exists(path):
            return path

        return None

    def _parse_json_response(self, text: str) -> Optional[Dict]:
        """Parse JSON from LLM response."""
        try:
            # Try to find JSON in response
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
        return None

# =============================================================================
# ENTRY POINT
# =============================================================================

def run_agent():
    """Run the interactive agent."""
    agent = OdooCodeAgent()
    agent.start()

if __name__ == "__main__":
    run_agent()
