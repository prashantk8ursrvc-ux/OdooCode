# forge/interactive.py
"""
OdooCode — Interactive Wizard Interface
Guides users through module creation with beautiful UI.
"""
import os
from typing import List, Dict, Optional, Tuple
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich.rule import Rule
from rich import box

from .ui.tui import (
    console, display_wizard_header, display_question,
    display_wizard_summary, print_success, print_info, ODOOCODE_THEME
)

# =============================================================================
# QUESTION DEFINITIONS
# =============================================================================

QUESTIONS = {
    "module_type": {
        "question": "What type of Odoo module do you want to create?",
        "options": [
            ("new", "Create a brand new module from scratch"),
            ("extend", "Extend an existing Odoo module (add features)"),
            ("fix", "Fix bugs or issues in an existing module"),
            ("migrate", "Migrate a module to Odoo 18"),
        ],
        "multi": False,
    },
    "base_module": {
        "question": "Which existing Odoo module do you want to extend?",
        "options": [
            ("sale", "Sale Orders & Quotations"),
            ("purchase", "Purchase Orders"),
            ("stock", "Inventory & Warehouse"),
            ("account", "Accounting & Finance"),
            ("hr", "Human Resources"),
            ("crm", "CRM & Pipeline"),
            ("project", "Project Management"),
            ("manufacturing", "Manufacturing (mrp)"),
            ("website", "Website Builder"),
            ("portal", "Portal & Frontend"),
        ],
        "multi": True,
        "condition": lambda answers: answers.get("module_type") in ("extend", "fix", "migrate"),
    },
    "business_area": {
        "question": "What business area does this module serve?",
        "options": [
            ("sales", "Sales & CRM"),
            ("inventory", "Inventory & Warehouse"),
            ("manufacturing", "Manufacturing & Production"),
            ("accounting", "Accounting & Finance"),
            ("hr", "Human Resources"),
            ("project", "Project Management"),
            ("purchase", "Procurement & Purchasing"),
            ("website", "Website & eCommerce"),
            ("custom", "Custom/Business Specific"),
        ],
        "multi": False,
        "condition": lambda answers: answers.get("module_type") == "new",
    },
    "features": {
        "question": "Which features does your module need?",
        "options": [
            ("chatter", "Chatter (mail.thread) for activity tracking"),
            ("activities", "Activity scheduling (mail.activity.mixin)"),
            ("multi_company", "Multi-company support with record isolation"),
            ("approvals", "Approval workflow with state machine"),
            ("reports", "PDF/QWeb reports"),
            ("website", "Website/frontend integration"),
            ("api", "REST API endpoints"),
            ("scheduler", "Scheduled actions (cron jobs)"),
            ("sequences", "Auto-numbering sequences"),
            ("demo_data", "Demo data for testing"),
            ("tests", "Unit test suite"),
            ("wizards", "Transient wizard models"),
        ],
        "multi": True,
    },
    "state_machine": {
        "question": "Does your module need a state/workflow machine?",
        "options": [
            ("simple", "Simple: draft → confirmed → done"),
            ("approval", "With approval: draft → pending → approved → done"),
            ("complex", "Complex: draft → review → approved → running → done/cancelled"),
            ("none", "No state machine needed"),
        ],
        "multi": False,
    },
    "views": {
        "question": "Which view types do you need?",
        "options": [
            ("form", "Form view for data entry"),
            ("list", "List/table view for records"),
            ("kanban", "Kanban board view"),
            ("search", "Advanced search with filters"),
            ("calendar", "Calendar view"),
            ("pivot", "Pivot table view"),
            ("graph", "Graph/chart view"),
            ("activity", "Activity view"),
        ],
        "multi": True,
    },
    "security_level": {
        "question": "What level of security do you need?",
        "options": [
            ("basic", "Basic: manager + user groups"),
            ("roles", "Role-based: manager, user, viewer, approver"),
            ("sensitive", "Sensitive data: audit trail, record ownership"),
            ("compliance", "Compliance: full logging, restricted access"),
        ],
        "multi": False,
    },
    "data_complexity": {
        "question": "How complex is the data model?",
        "options": [
            ("simple", "1-2 models with basic fields"),
            ("medium", "3-5 models with relationships"),
            ("complex", "5+ models with many2many, computed fields, inheritance"),
            ("enterprise", "Enterprise: multi-company, accounting integration"),
        ],
        "multi": False,
    },
    "performance": {
        "question": "Any specific performance requirements?",
        "options": [
            ("standard", "Standard (< 10k records)"),
            ("large", "Large dataset (10k-100k records)"),
            ("massive", "Massive (100k+ records, need optimization)"),
            ("realtime", "Real-time updates required"),
        ],
        "multi": False,
    },
    "integration": {
        "question": "Any external integrations needed?",
        "options": [
            ("none", "No external integrations"),
            ("api", "External REST/SOAP API"),
            ("payment", "Payment gateway"),
            ("shipping", "Shipping carrier"),
            ("email", "Email/SMS services"),
            ("iot", "IoT devices"),
        ],
        "multi": True,
    },
    "ui_preference": {
        "question": "Any UI/UX preferences?",
        "options": [
            ("standard", "Standard Odoo interface"),
            ("enhanced", "Enhanced with custom widgets"),
            ("mobile", "Mobile-friendly responsive design"),
            ("dashboard", "Dashboard with KPIs and charts"),
        ],
        "multi": False,
    },
}

# =============================================================================
# WIZARD ENGINE
# =============================================================================

class OdooCodeWizard:
    """Interactive wizard that guides users through module creation."""

    def __init__(self):
        self.answers: Dict[str, any] = {}
        self.custom_input: Dict[str, str] = {}

    def _should_ask(self, question_id: str) -> bool:
        """Check if question should be asked based on conditions."""
        q = QUESTIONS[question_id]
        condition = q.get("condition")
        if condition and not condition(self.answers):
            return False
        return True

    def _get_custom_details(self, question_id: str):
        """Get additional custom details for important questions."""
        if question_id == "module_type" and self.answers.get("module_type") == "new":
            name = Prompt.ask("[odoo.primary]Enter module technical name[/odoo.primary]", default="my_module")
            self.custom_input["technical_name"] = name

            desc = Prompt.ask("[odoo.primary]Enter module description[/odoo.primary]", default="")
            self.custom_input["description"] = desc

        elif question_id == "features":
            features = self.answers.get("features", [])
            if "approvals" in features:
                states = Prompt.ask(
                    "[odoo.primary]Enter approval states (comma-separated)[/odoo.primary]",
                    default="draft,pending,approved,done"
                )
                self.custom_input["states"] = [s.strip() for s in states.split(",")]

            if "sequences" in features:
                prefix = Prompt.ask("[odoo.primary]Enter sequence prefix (e.g., SEQ/)[/odoo.primary]", default="SEQ/")
                self.custom_input["sequence_prefix"] = prefix

    def run(self) -> Dict[str, any]:
        """Run the interactive wizard and return collected answers."""
        display_wizard_header()

        for question_id, q in QUESTIONS.items():
            if not self._should_ask(question_id):
                continue

            self.answers[question_id] = display_question(
                q["question"],
                q["options"],
                q.get("multi", False)
            )
            self._get_custom_details(question_id)

        # Display summary
        display_wizard_summary(self.answers, QUESTIONS)

        # Confirm
        console.print()
        if Confirm.ask("[odoo.success]Proceed with module generation?[/odoo.success]"):
            return self._build_config()
        else:
            console.print("[odoo.warning]Generation cancelled.[/odoo.warning]")
            return None

    def _build_config(self) -> Dict[str, any]:
        """Build configuration dictionary from answers."""
        answers = self.answers
        custom = self.custom_input

        parts = []

        # Module type
        if answers.get("module_type") == "new":
            parts.append(f"Create a new Odoo 18 module called '{custom.get('technical_name', 'my_module')}'.")
            if custom.get("description"):
                parts.append(f"Description: {custom['description']}")
        elif answers.get("module_type") == "extend":
            base = answers.get("base_module", ["sale"])
            parts.append(f"Extend the following Odoo modules: {', '.join(base)}.")
        elif answers.get("module_type") == "migrate":
            base = answers.get("base_module", ["sale"])
            parts.append(f"Migrate these modules to Odoo 18: {', '.join(base)}.")

        if answers.get("business_area"):
            parts.append(f"Business area: {answers['business_area']}.")

        features = answers.get("features", [])
        if features:
            feature_map = {
                "chatter": "Include mail.thread for activity tracking and chatter",
                "activities": "Include mail.activity.mixin for activity scheduling",
                "multi_company": "Add multi-company support with ir.rule record isolation",
                "approvals": "Implement approval workflow with state machine",
                "reports": "Add PDF/QWeb report templates",
                "website": "Include website/frontend integration",
                "api": "Create REST API endpoints",
                "scheduler": "Add scheduled actions (cron jobs)",
                "sequences": "Implement auto-numbering sequences",
                "demo_data": "Include demo data for testing",
                "tests": "Write unit test suite",
                "wizards": "Create transient wizard models",
            }
            for f in features:
                if f in feature_map:
                    parts.append(f"Feature: {feature_map[f]}.")

        states = answers.get("state_machine")
        if states and states != "none":
            if "states" in custom:
                state_list = custom["states"]
                parts.append(f"Implement state machine with states: {', '.join(state_list)}.")
            elif states == "simple":
                parts.append("Implement simple state machine: draft → confirmed → done.")
            elif states == "approval":
                parts.append("Implement approval workflow: draft → pending → approved → done.")
            elif states == "complex":
                parts.append("Implement complex workflow: draft → review → approved → running → done/cancelled.")

        views = answers.get("views", [])
        if views:
            parts.append(f"Required views: {', '.join(views)}.")

        security = answers.get("security_level")
        if security:
            security_map = {
                "basic": "Implement basic security: manager and user groups with ir.model.access.csv",
                "roles": "Implement role-based access: manager, user, viewer, approver groups",
                "sensitive": "Implement sensitive data protection: audit trail, record ownership ir.rule",
                "compliance": "Implement compliance-grade security: full logging, restricted access, ir.rule",
            }
            parts.append(security_map.get(security, ""))

        perf = answers.get("performance")
        if perf and perf != "standard":
            perf_map = {
                "large": "Optimize for large datasets (10k-100k records)",
                "massive": "Optimize for massive datasets (100k+ records) with pagination and caching",
                "realtime": "Implement real-time updates",
            }
            parts.append(perf_map.get(perf, ""))

        integrations = answers.get("integration", [])
        if integrations and "none" not in integrations:
            integ_map = {
                "api": "Integrate with external REST/SOAP API",
                "payment": "Integrate with payment gateway",
                "shipping": "Integrate with shipping carrier",
                "email": "Integrate with email/SMS services",
                "iot": "Integrate with IoT devices",
            }
            for integ in integrations:
                if integ in integ_map:
                    parts.append(integ_map[integ])

        if "sequence_prefix" in custom:
            parts.append(f"Use sequence prefix '{custom['sequence_prefix']}' for auto-numbering.")

        return {
            "prompt": " ".join(parts),
            "answers": answers,
            "custom": custom,
        }


# =============================================================================
# QUICK MODE (Non-interactive)
# =============================================================================

def quick_build(prompt: str) -> Dict[str, any]:
    """Build configuration from a simple prompt without wizard."""
    return {
        "prompt": prompt,
        "answers": {},
        "custom": {},
    }


# =============================================================================
# ENTRY POINT
# =============================================================================

def run_wizard() -> Optional[Dict[str, any]]:
    """Run the interactive wizard and return configuration."""
    wizard = OdooCodeWizard()
    return wizard.run()
