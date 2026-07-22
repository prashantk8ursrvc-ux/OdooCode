# forge/agents/analyst.py
import logging
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.rule import Rule
from rich.panel import Panel
from .base import BaseAgent
from ..utils import strip_plan_code
from ..tools.web_search import web_search, extract_research_queries
from ..prompts import PromptLibrary
from ..ui.tui import console

logger = logging.getLogger("ForgeOrchestrator.Analyst")

class AnalystAgent(BaseAgent):
    def analyze(self, prompt: str, codebase_context: str = "") -> str:
        logger.info(f"AnalystAgent: '{prompt[:80]}'")
        console.print(Rule("[forge.phase]Phase 1 — Deep Analysis[/forge.phase]"))
        cb_block = (f"\nEXISTING CODEBASE (do NOT duplicate anything):\n"
                    f"{codebase_context}\n\n") if codebase_context else ""
        user_prompt = f"{cb_block}Requirement:\n{prompt}"
        skill_query = f"Odoo 18 module architecture best practices {prompt[:120]}"

        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                      TimeElapsedColumn(), transient=True, console=console) as p:
            p.add_task("Analysing requirement...", total=None)
            analysis = self.llm.call(
                PromptLibrary.analyst_system(), user_prompt, self.config.resolve_model("planner"),
                skill_query=skill_query)
        analysis = strip_plan_code(analysis)

        queries = extract_research_queries(analysis)
        if queries:
            console.print(f"[forge.phase]Web Research:[/forge.phase] {len(queries)} query/ies found")
            parts = []
            for q in queries[:3]:
                console.print(f"  [forge.info]Searching:[/forge.info] {q}")
                parts.append(f"Query: {q}\nResults:\n{web_search(q)}")
            refine_sys = (
                "You are a Senior Odoo 18 Architect refining your implementation plan.\n"
                "Incorporate the web research into your plan to make it more accurate.\n"
                "Keep the same section structure. Remove all [RESEARCH_NEEDED] tokens.\n"
                "CRITICAL: Write ONLY in plain English prose. NO raw Python or XML code blocks."
            )
            refine_usr = (f"ORIGINAL PLAN:\n{analysis}\n\n"
                          f"WEB RESEARCH:\n{chr(10).join(parts)}\n\n"
                          "Output the revised complete Implementation Plan in plain English. No code blocks.")
            with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                          transient=True, console=console) as p:
                p.add_task("Refining with research...", total=None)
                analysis = self.llm.call(refine_sys, refine_usr, self.config.resolve_model("planner"))
            analysis = strip_plan_code(analysis)

        preview = analysis[:800] + "\n..." if len(analysis) > 800 else analysis
        console.print(Panel(preview, title="[forge.accent]Plan Preview[/forge.accent]",
                            border_style="magenta"))
        return analysis
