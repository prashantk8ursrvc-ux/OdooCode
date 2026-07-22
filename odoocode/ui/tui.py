# forge/ui/tui.py
"""
OdooCode — Beautiful Terminal User Interface
Inspired by MiMoCode's elegant TUI design.
"""
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme
from rich.text import Text
from rich.rule import Rule
from rich.syntax import Syntax
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.live import Live
from rich.layout import Layout
from rich.columns import Columns
from rich.align import Align
from rich import box
import time

# =============================================================================
# ODOOCODE THEME — Modern, Elegant Color Scheme
# =============================================================================

ODOOCODE_THEME = Theme({
    # Primary colors
    "odoo.primary":    "bright_cyan",
    "odoo.secondary":  "bright_magenta",
    "odoo.accent":     "bright_yellow",

    # Status colors
    "odoo.success":    "bold bright_green",
    "odoo.warning":    "bold bright_yellow",
    "odoo.error":      "bold bright_red",
    "odoo.info":       "dim bright_white",

    # Element colors
    "odoo.phase":      "bold bright_cyan",
    "odoo.file":       "bright_cyan",
    "odoo.model":      "bright_blue",
    "odoo.version":    "bright_magenta",
    "odoo.prompt":     "bright_yellow",
    "odoo.answer":     "bright_green",

    # UI elements
    "odoo.border":     "bright_cyan",
    "odoo.header":     "bold bright_magenta",
    "odoo.dim":        "dim white",
})

console = Console(theme=ODOOCODE_THEME)

# =============================================================================
# ASCII ART LOGO
# =============================================================================

LOGO = r"""
 ██████╗ ██████╗  ██████╗  ██████╗  ██████╗  ██████╗ ██████╗ ███████╗
██╔═══██╗██╔══██╗██╔═══██╗██╔═══██╗██╔════╝ ██╔═══██╗██╔══██╗██╔════╝
██║   ██║██║  ██║██║   ██║██║   ██║██║      ██║   ██║██║  ██║█████╗
██║   ██║██║  ██║██║   ██║██║   ██║██║      ██║   ██║██║  ██║██╔══╝
╚██████╔╝██████╔╝╚██████╔╝╚██████╔╝╚██████╗ ╚██████╔╝██████╔╝███████╗
 ╚═════╝ ╚═════╝  ╚═════╝  ╚═════╝  ╚═════╝  ╚═════╝ ╚═════╝ ╚══════╝

                           O D O O C O D E
"""

LOGO_COMPACT = "ODOOCODE"

# =============================================================================
# BANNER & HEADER
# =============================================================================

def print_banner(config=None):
    """Display the OdooCode banner with configuration info."""
    console.print()

    # Logo
    logo_text = Text(LOGO, style="bold bright_cyan")
    console.print(Align.center(logo_text))
    console.print()

    # Version tagline
    tagline = Text("Professional Odoo 18 Module Generator", style="dim bright_white")
    console.print(Align.center(tagline))
    console.print()

    if config:
        # Configuration panel
        config_table = Table(show_header=False, padding=(0, 2), box=box.ROUNDED)
        config_table.add_column("Key", style="odoo.dim", no_wrap=True)
        config_table.add_column("Value", style="odoo.info")

        config_table.add_row("Mode", f"[odoo.primary]{config.mode.upper()}[/odoo.primary]")
        config_table.add_row("Output", f"[odoo.file]{config.output_dir}[/odoo.file]")
        config_table.add_row("Coder", f"[odoo.model]{config.resolve_model('coder')}[/odoo.model]")
        config_table.add_row("Planner", f"[odoo.model]{config.resolve_model('planner')}[/odoo.model]")
        config_table.add_row("Critic", f"[odoo.model]{config.resolve_model('critic')}[/odoo.model]")

        if config.codebase_path:
            config_table.add_row("Codebase", f"[odoo.file]{config.codebase_path}[/odoo.file]")

        panel = Panel(
            config_table,
            title="[odoo.header]Configuration[/odoo.header]",
            border_style="odoo.border",
            padding=(1, 2)
        )
        console.print(Align.center(panel))

    console.print()

# =============================================================================
# PROGRESS INDICATORS
# =============================================================================

class OdooProgress:
    """Beautiful progress indicator for long operations."""

    def __init__(self, description: str, total: int = None):
        self.description = description
        self.total = total
        self.progress = None
        self.task = None

    def __enter__(self):
        self.progress = Progress(
            SpinnerColumn("dots", style="odoo.primary"),
            TextColumn("[odoo.primary]{task.description}[/odoo.primary]"),
            BarColumn(bar_width=40, complete_style="odoo.primary", finished_style="odoo.success"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True
        )
        self.progress.start()
        self.task = self.progress.add_task(self.description, total=self.total)
        return self

    def __exit__(self, *args):
        if self.progress:
            self.progress.stop()

    def update(self, description: str = None, advance: int = 1):
        if self.progress and self.task:
            if description:
                self.progress.update(self.task, description=description)
            self.progress.advance(self.task, advance)

    def complete(self, message: str = "Done"):
        if self.progress:
            self.progress.update(self.task, description=f"[odoo.success]{message}[/odoo.success]")
            time.sleep(0.3)  # Brief pause to show completion

# =============================================================================
# PHASE DISPLAY
# =============================================================================

def print_phase(phase_num: int, phase_name: str, description: str = ""):
    """Display a phase header with beautiful formatting."""
    console.print()
    console.print(Rule(f"[odoo.phase]Phase {phase_num} — {phase_name}[/odoo.phase]", style="odoo.primary"))
    if description:
        console.print(f"[odoo.dim]{description}[/odoo.dim]")
    console.print()

def print_subphase(subphase: str):
    """Display a subphase indicator."""
    console.print(f"  [odoo.info]▸[/odoo.info] [odoo.primary]{subphase}[/odoo.primary]")

import sys
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def _safe_symbol(sym: str, fallback: str) -> str:
    try:
        sym.encode(sys.stdout.encoding or "utf-8")
        return sym
    except Exception:
        return fallback

ICON_SUCCESS = _safe_symbol("✓", "[+]")
ICON_WARNING = _safe_symbol("⚠", "[!]")
ICON_ERROR = _safe_symbol("✗", "[x]")
ICON_INFO = _safe_symbol("i", "[i]")

# =============================================================================
# STATUS DISPLAYS
# =============================================================================

def print_success(message: str):
    """Display a success message."""
    console.print(f"  [odoo.success]{ICON_SUCCESS}[/odoo.success] {message}")

def print_warning(message: str):
    """Display a warning message."""
    console.print(f"  [odoo.warning]{ICON_WARNING}[/odoo.warning] {message}")

def print_error(message: str):
    """Display an error message."""
    console.print(f"  [odoo.error]{ICON_ERROR}[/odoo.error] {message}")

def print_info(message: str):
    """Display an info message."""
    console.print(f"  [odoo.info]{ICON_INFO}[/odoo.info] {message}")

def print_step(step: int, total: int, message: str):
    """Display a step indicator."""
    console.print(f"  [odoo.primary][{step}/{total}][/odoo.primary] {message}")

# =============================================================================
# FILE DISPLAY
# =============================================================================

def display_preview(bf):
    """Display a syntax-highlighted preview of generated code."""
    from ..utils import detect_lexer

    lexer = detect_lexer(bf.filepath)
    preview = bf.content[:1500] + "\n...[truncated]" if len(bf.content) > 1500 else bf.content

    try:
        syntax = Syntax(preview, lexer, theme="monokai", line_numbers=True, word_wrap=True)
    except Exception:
        syntax = Syntax(preview, "text", theme="monokai", line_numbers=True)

    panel = Panel(
        syntax,
        title=f"[odoo.file]{bf.filepath}[/odoo.file]  [odoo.dim]({len(bf.content)} chars)[/odoo.dim]",
        border_style="odoo.border",
        padding=(0, 1)
    )
    console.print(panel)

def display_file_list(files: list, title: str = "Files"):
    """Display a list of files in a beautiful table."""
    table = Table(
        title=f"[odoo.header]{title}[/odoo.header]",
        show_lines=True,
        header_style="odoo.primary",
        box=box.ROUNDED,
        border_style="odoo.border"
    )
    table.add_column("#", style="odoo.dim", no_wrap=True, width=4)
    table.add_column("File", style="odoo.file", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Score", justify="right", style="odoo.dim")

    for idx, (filepath, status, score) in enumerate(files, 1):
        status_color = {
            "approved": "odoo.success",
            "generated": "odoo.warning",
            "failed": "odoo.error",
            "pending": "odoo.dim"
        }.get(status, "odoo.info")

        score_str = f"{int(score)}/100" if score else "—"
        table.add_row(
            str(idx),
            filepath,
            f"[{status_color}]{status}[/{status_color}]",
            score_str
        )

    console.print(table)

# =============================================================================
# BLUEPRINT TABLE
# =============================================================================

def display_blueprint_table(blueprint):
    """Display the module blueprint in a beautiful table."""
    table = Table(
        title="[odoo.header]Module Blueprint[/odoo.header]",
        show_lines=True,
        header_style="odoo.primary",
        box=box.ROUNDED,
        border_style="odoo.border"
    )
    table.add_column("#", style="odoo.dim", no_wrap=True, width=4)
    table.add_column("File", style="odoo.file", no_wrap=True)
    table.add_column("Depends On", style="odoo.dim")
    table.add_column("Status", justify="center")

    status_colors = {
        "pending": "odoo.dim",
        "generated": "odoo.warning",
        "validated": "odoo.primary",
        "approved": "odoo.success",
        "failed": "odoo.error"
    }

    for idx, bf in enumerate(blueprint, 1):
        dep_str = ", ".join(bf.depends_on) if bf.depends_on else "—"
        sc = status_colors.get(bf.status, "odoo.dim")
        table.add_row(
            str(idx),
            bf.filepath,
            dep_str,
            f"[{sc}]{bf.status}[/{sc}]"
        )

    console.print(table)

# =============================================================================
# SUMMARY TABLE
# =============================================================================

def display_summary(blueprint, audit_result=None, struct_valid=True):
    """Display a comprehensive generation summary."""
    console.print()
    console.print(Rule("[odoo.header]Generation Summary[/odoo.header]", style="odoo.primary"))
    console.print()

    # File summary
    table = Table(
        show_lines=True,
        header_style="odoo.primary",
        box=box.ROUNDED,
        border_style="odoo.border"
    )
    table.add_column("File", style="odoo.file", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Score", justify="right")
    table.add_column("Retries", justify="right", style="odoo.dim")

    for bf in blueprint:
        status_color = "odoo.success" if bf.status == "approved" else \
                      "odoo.error" if bf.status == "failed" else "odoo.warning"
        score_str = f"{int(bf.critic_score)}/100" if bf.critic_score else "—"
        table.add_row(
            bf.filepath,
            f"[{status_color}]{bf.status}[/{status_color}]",
            score_str,
            str(bf.retry_count)
        )

    console.print(table)
    console.print()

    # Security summary
    if audit_result:
        score = audit_result.get('security_score', 0)
        color = "odoo.success" if score >= 80 else "odoo.warning" if score >= 60 else "odoo.error"
        console.print(f"  [odoo.dim]Security Score:[/odoo.dim] [{color}]{score}/100[/{color}]")

    # Structure summary
    if struct_valid:
        console.print(f"  [odoo.dim]Module Structure:[/odoo.dim] [odoo.success]COMPLETE[/odoo.success]")
    else:
        console.print(f"  [odoo.dim]Module Structure:[/odoo.dim] [odoo.warning]HAS ISSUES[/odoo.warning]")

    console.print()

# =============================================================================
# WIZARD UI
# =============================================================================

def display_wizard_header():
    """Display the interactive wizard header."""
    console.print()
    logo_text = Text(LOGO, style="bold bright_cyan")
    console.print(Align.center(logo_text))
    console.print()

    tagline = Text("Interactive Module Builder", style="dim bright_white")
    console.print(Align.center(tagline))
    console.print()

    instructions = Panel(
        "[odoo.info]Answer the questions below to configure your module.\n"
        "Type the number(s) of your choice, or 'custom' to type your own.[/odoo.info]",
        border_style="odoo.border",
        padding=(1, 2)
    )
    console.print(Align.center(instructions))
    console.print()

def display_question(question: str, options: list, multi: bool = False):
    """Display a question with options in a beautiful format."""
    console.print(f"[odoo.primary]{question}[/odoo.primary]")
    if multi:
        console.print("[odoo.dim](Select multiple: e.g., 1,3,5 or 'all')[/odoo.dim]")
    console.print()

    table = Table(show_header=False, padding=(0, 2), box=box.SIMPLE)
    table.add_column("Num", style="odoo.accent", no_wrap=True, width=4)
    table.add_column("Key", style="odoo.primary", no_wrap=True, width=15)
    table.add_column("Description", style="odoo.info")

    for i, (key, desc) in enumerate(options, 1):
        table.add_row(str(i), key, desc)

    console.print(table)
    console.print()

def display_wizard_summary(answers: dict, questions: dict):
    """Display the wizard configuration summary."""
    console.print(Rule("[odoo.header]Configuration Summary[/odoo.header]", style="odoo.primary"))

    table = Table(
        show_header=True,
        header_style="odoo.primary",
        box=box.ROUNDED,
        border_style="odoo.border"
    )
    table.add_column("Setting", style="odoo.accent", no_wrap=True)
    table.add_column("Value", style="odoo.info")

    for q_id, answer in answers.items():
        q = questions[q_id]
        # Format answer for display
        if isinstance(answer, list):
            display_val = ", ".join(answer)
        else:
            # Find description from options
            desc = next((opt[1] for opt in q["options"] if opt[0] == answer), answer)
            display_val = desc

        # Truncate long questions
        q_text = q["question"][:45] + "..." if len(q["question"]) > 45 else q["question"]
        table.add_row(q_text, display_val)

    console.print(table)

# =============================================================================
# CODE PREVIEW
# =============================================================================

def display_code_preview(filepath: str, content: str, max_lines: int = 50):
    """Display a code preview with syntax highlighting."""
    from ..utils import detect_lexer

    lexer = detect_lexer(filepath)
    lines = content.split("\n")
    preview = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        preview += f"\n... [{len(lines) - max_lines} more lines]"

    syntax = Syntax(preview, lexer, theme="monokai", line_numbers=True, word_wrap=True)
    panel = Panel(
        syntax,
        title=f"[odoo.file]{filepath}[/odoo.file]",
        border_style="odoo.border",
        padding=(0, 1)
    )
    console.print(panel)

# =============================================================================
# FINAL DISPLAY
# =============================================================================

def display_complete(output_dir: str, file_count: int, zip_path: str = None):
    """Display completion message."""
    console.print()
    console.print(Rule("[odoo.success]Generation Complete[/odoo.success]", style="odoo.success"))
    console.print()

    summary = Panel(
        f"[odoo.success]✓[/odoo.success] [odoo.dim]Generated[/odoo.dim] [odoo.primary]{file_count}[/odoo.primary] [odoo.dim]files[/odoo.dim]\n"
        f"[odoo.success]✓[/odoo.success] [odoo.dim]Output:[/odoo.dim] [odoo.file]{output_dir}[/odoo.file]" +
        (f"\n[odoo.success]✓[/odoo.success] [odoo.dim]Archive:[/odoo.dim] [odoo.file]{zip_path}[/odoo.file]" if zip_path else ""),
        border_style="odoo.success",
        padding=(1, 2)
    )
    console.print(summary)
    console.print()
