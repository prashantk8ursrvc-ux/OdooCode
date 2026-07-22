# forge/main.py
"""
OdooCode — Professional Odoo 18 Module Generator
Persistent Interactive Agent Mode
"""
import sys, os, re, argparse, textwrap
from .config import ForgeConfig, DEFAULT_MODEL_GROUPS
from .ui.tui import console, print_banner

VERSION = "6.0.0"
APP_NAME = "OdooCode"

def main():
    parser = argparse.ArgumentParser(
        prog="odoocode",
        description=f"{APP_NAME} v{VERSION} — Multi-Provider Agentic Odoo 18 Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            USAGE:
              # Start interactive agent (default)
              python -m odoocode

              # Start with a codebase
              python -m odoocode --codebase ./my_addon

              # Quick command mode
              python -m odoocode "Create a Barber Shop module" ./odoocode_output

              # Interactive wizard
              python -m odoocode --wizard

              # Backward compatible alias
              python -m forge "Create fleet module"

            PROVIDERS:
              Ollama (local):     model_name (e.g., odoo18-coder-v3:latest)
              OpenAI:             openai/gpt-4o
              Anthropic:          anthropic/claude-sonnet-4-20250514
              OpenRouter:         openrouter/vendor/model-name

            MODEL GROUPS:
              Define in odoocode_config.json or use --model-group:
              "model_groups": {{
                "coder": "anthropic/claude-sonnet-4-20250514",
                "planner": "openai/gpt-4o",
                "critic": "anthropic/claude-sonnet-4-20250514"
              }}
        """))

    # Mode selection (optional - default is agent mode)
    parser.add_argument("prompt", nargs="?", default=None,
                       help="Quick command (optional - omit for agent mode)")
    parser.add_argument("output_dir", nargs="?", default="./odoocode_output",
                       help="Output directory")

    # Agent mode
    parser.add_argument("--codebase", "-cb", default="",
                       help="Path to existing codebase to work with")

    # Wizard mode
    parser.add_argument("--wizard", "-w", action="store_true",
                       help="Launch interactive wizard")

    # Model configuration
    parser.add_argument("-cm", "--coder-model", default=None,
                       help="Model for code generation (overrides model_groups)")
    parser.add_argument("-pm", "--planner-model", default=None,
                       help="Model for planning (overrides model_groups)")
    parser.add_argument("-cr", "--critic-model", default=None,
                       help="Model for review (overrides model_groups)")

    # Model groups (override via CLI)
    parser.add_argument("--model-group", action="append", default=[],
                       metavar="ROLE=MODEL",
                       help="Override a model group (e.g., --model-group coder=anthropic/claude-sonnet-4-20250514)")

    # Config file
    parser.add_argument("--config", default=None,
                       help="Path to config file (default: odoocode_config.json)")

    # Info commands
    parser.add_argument("--list-models", action="store_true",
                       help="List available providers and models, then exit")

    # Other options
    parser.add_argument("--num-ctx", type=int, default=16384,
                       help="Context window size")
    parser.add_argument("--temperature", type=float, default=0.3,
                       help="LLM temperature")

    args = parser.parse_args()

    # Build config from file + CLI overrides
    config = ForgeConfig.from_file(args.config)

    # Apply CLI model overrides
    if args.coder_model:
        config.coder_model = args.coder_model
        config.model_groups["coder"] = args.coder_model
    if args.planner_model:
        config.planner_model = args.planner_model
        config.model_groups["planner"] = args.planner_model
    if args.critic_model:
        config.critic_model = args.critic_model
        config.model_groups["critic"] = args.critic_model

    # Apply --model-group overrides
    for group_arg in args.model_group:
        if "=" in group_arg:
            role, model = group_arg.split("=", 1)
            config.model_groups[role.strip()] = model.strip()

    # Apply numeric overrides
    config.num_ctx = args.num_ctx
    if args.temperature != 0.3:  # Only override if changed from default
        config.temperature = args.temperature

    # List models command
    if args.list_models:
        from .llm import LLMClient
        llm = LLMClient(config)
        models = llm.list_available_models()
        console.print(f"\n[bold]Available Providers & Models:[/bold]\n")
        for provider, model_list in models.items():
            status = f"[green]({len(model_list)} models)[/green]" if model_list else "[dim](not available)[/dim]"
            console.print(f"  {provider}: {status}")
            for m in model_list[:10]:
                console.print(f"    - {m}")
            if len(model_list) > 10:
                console.print(f"    ... and {len(model_list) - 10} more")
        console.print(f"\n[dim]Model groups configured:[/dim]")
        for role, model in config.model_groups.items():
            console.print(f"  {role}: {model}")
        return

    # If wizard mode, run wizard
    if args.wizard:
        from .interactive import run_wizard
        result = run_wizard()
        if result:
            config.prompt = result["prompt"]
            config.output_dir = args.output_dir
            from .workflow import ForgeWorkflow
            workflow = ForgeWorkflow(config)
            workflow.run()
        return

    # If quick command provided, run it
    if args.prompt is not None:
        config.prompt = args.prompt
        config.output_dir = args.output_dir
        config.codebase_path = args.codebase

        # Determine mode from prompt
        p_lower = args.prompt.lower().strip()
        words_in_prompt = set(re.findall(r'\b\w+\b', p_lower))
        if p_lower.startswith(('create', 'build', 'generate', 'make', 'new')):
            config.mode = "generate"
        elif 'repair' in words_in_prompt or 'fix' in words_in_prompt:
            config.mode = "repair"
        elif any(w in words_in_prompt for w in ['modify', 'change', 'update']):
            config.mode = "edit"
        else:
            config.mode = "generate"

        from .workflow import ForgeWorkflow
        workflow = ForgeWorkflow(config)
        workflow.run()
        return

    # Default: Start persistent TUI agent mode
    from .tui_agent import OdooCodeTUI
    tui = OdooCodeTUI()

    # Set codebase if provided
    if args.codebase:
        tui.current_path = os.path.abspath(args.codebase)

    tui.run()

if __name__ == "__main__":
    main()
