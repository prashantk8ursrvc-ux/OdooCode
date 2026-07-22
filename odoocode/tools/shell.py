# forge/tools/shell.py
"""
Shell tool for running commands in the OdooCode workflow.
Supports Odoo-specific commands (test, lint, module install).
"""
import os
import subprocess
import logging
import shlex
from typing import Optional, Tuple
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("Forge.tools.shell")


@dataclass
class ShellResult:
    """Result of a shell command execution."""
    command: str
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Combined output."""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        return "\n".join(parts)

    def __str__(self):
        status = "OK" if self.success else f"FAILED (code={self.returncode})"
        return f"ShellResult({status}, {len(self.output)} chars)"


class ShellRunner:
    """
    Executes shell commands with timeout and error handling.
    Safe for use in the OdooCode workflow.
    """

    def __init__(self, cwd: str = None, timeout: int = 120):
        self.cwd = cwd or os.getcwd()
        self.timeout = timeout

    def run(self, command: str, cwd: str = None, timeout: int = None,
            env: dict = None, shell: bool = True) -> ShellResult:
        """Run a shell command and return the result."""
        work_dir = cwd or self.cwd
        cmd_timeout = timeout or self.timeout

        # Merge environment
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)

        logger.info(f"Shell run: {command} (cwd={work_dir}, timeout={cmd_timeout}s)")

        try:
            result = subprocess.run(
                command,
                cwd=work_dir,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=cmd_timeout,
                env=exec_env,
            )
            return ShellResult(
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out: {command}")
            return ShellResult(
                command=command,
                stdout="",
                stderr=f"Command timed out after {cmd_timeout}s",
                returncode=-1,
                timed_out=True,
            )
        except Exception as e:
            logger.error(f"Shell error: {e}")
            return ShellResult(
                command=command,
                stdout="",
                stderr=str(e),
                returncode=-1,
            )

    def run_python(self, script: str, cwd: str = None) -> ShellResult:
        """Run a Python script."""
        return self.run(f"python -c {shlex.quote(script)}", cwd=cwd)

    def run_odoo_test(self, module: str, odoo_bin: str = "odoo-bin",
                      db_name: str = "test_db", cwd: str = None) -> ShellResult:
        """Run Odoo tests for a specific module."""
        cmd = (
            f"{odoo_bin} --test-enable --stop-after-init "
            f"-d {db_name} -i {module} --log-level=test"
        )
        return self.run(cmd, cwd=cwd, timeout=300)

    def run_lint(self, path: str, linter: str = "ruff") -> ShellResult:
        """Run a linter on a path."""
        if linter == "ruff":
            return self.run(f"ruff check {shlex.quote(path)}")
        elif linter == "pylint":
            return self.run(f"pylint --output-format=text {shlex.quote(path)}")
        elif linter == "flake8":
            return self.run(f"flake8 {shlex.quote(path)}")
        else:
            return self.run(f"{linter} {shlex.quote(path)}")

    def check_odoo_installable(self, module_path: str) -> ShellResult:
        """Check if an Odoo module is properly structured."""
        manifest = Path(module_path) / "__manifest__.py"
        init = Path(module_path) / "__init__.py"
        models_init = Path(module_path) / "models" / "__init__.py"

        issues = []
        if not manifest.exists():
            issues.append("Missing __manifest__.py")
        if not init.exists():
            issues.append("Missing __init__.py")
        if not models_init.exists() and (Path(module_path) / "models").exists():
            issues.append("Missing models/__init__.py")

        if issues:
            return ShellResult(
                command="check_odoo_structure",
                stdout="",
                stderr="\n".join(issues),
                returncode=1,
            )
        return ShellResult(
            command="check_odoo_structure",
            stdout="Module structure OK",
            stderr="",
            returncode=0,
        )


# Convenience function
def run_command(command: str, cwd: str = None, timeout: int = 120) -> ShellResult:
    """Run a shell command."""
    runner = ShellRunner(cwd=cwd, timeout=timeout)
    return runner.run(command)
