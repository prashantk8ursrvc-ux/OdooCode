# forge/tools/git.py
"""
Git integration for OdooCode.
Version control operations for generated modules.
"""
import os
import logging
from typing import Optional, List
from dataclasses import dataclass
from pathlib import Path

from .shell import ShellRunner, ShellResult

logger = logging.getLogger("Forge.tools.git")


@dataclass
class GitStatus:
    """Git repository status."""
    branch: str = ""
    is_clean: bool = True
    modified: List[str] = None
    untracked: List[str] = None
    staged: List[str] = None

    def __post_init__(self):
        if self.modified is None:
            self.modified = []
        if self.untracked is None:
            self.untracked = []
        if self.staged is None:
            self.staged = []


class GitClient:
    """
    Git client for version control operations.
    """

    def __init__(self, cwd: str = None):
        self.runner = ShellRunner(cwd=cwd)
        self._is_repo = None

    def is_repo(self) -> bool:
        """Check if the current directory is a git repository."""
        if self._is_repo is None:
            result = self.runner.run("git rev-parse --is-inside-work-tree")
            self._is_repo = result.success and "true" in result.stdout.lower()
        return self._is_repo

    def init(self, path: str = ".") -> ShellResult:
        """Initialize a new git repository."""
        result = self.runner.run(f"git init {path}", cwd=path)
        self._is_repo = None
        return result

    def status(self) -> GitStatus:
        """Get repository status."""
        if not self.is_repo():
            return GitStatus()

        # Get branch
        branch_result = self.runner.run("git branch --show-current")
        branch = branch_result.stdout.strip() if branch_result.success else ""

        # Get status
        status_result = self.runner.run("git status --porcelain")
        if not status_result.success:
            return GitStatus(branch=branch)

        modified = []
        untracked = []
        staged = []

        for line in status_result.stdout.strip().split('\n'):
            if not line:
                continue
            status_code = line[:2]
            filepath = line[3:].strip()

            if status_code[0] in ('M', 'A', 'D', 'R'):
                staged.append(filepath)
            if status_code[1] == 'M':
                modified.append(filepath)
            elif status_code == '??':
                untracked.append(filepath)

        return GitStatus(
            branch=branch,
            is_clean=len(modified) == 0 and len(untracked) == 0 and len(staged) == 0,
            modified=modified,
            untracked=untracked,
            staged=staged,
        )

    def add(self, files: List[str] = None) -> ShellResult:
        """Stage files."""
        if files:
            cmd = f"git add {' '.join(files)}"
        else:
            cmd = "git add ."
        return self.runner.run(cmd)

    def commit(self, message: str, files: List[str] = None) -> ShellResult:
        """Create a commit."""
        if files:
            self.add(files)
        return self.runner.run(f'git commit -m "{message}"')

    def diff(self, staged: bool = False) -> ShellResult:
        """Show diff."""
        cmd = "git diff --staged" if staged else "git diff"
        return self.runner.run(cmd)

    def log(self, count: int = 10) -> ShellResult:
        """Show recent commits."""
        return self.runner.run(f"git log --oneline -{count}")

    def create_branch(self, name: str) -> ShellResult:
        """Create and switch to a new branch."""
        result = self.runner.run(f"git checkout -b {name}")
        self._is_repo = None
        return result

    def checkout(self, branch: str) -> ShellResult:
        """Switch to a branch."""
        result = self.runner.run(f"git checkout {branch}")
        self._is_repo = None
        return result

    def stash(self) -> ShellResult:
        """Stash changes."""
        return self.runner.run("git stash")

    def stash_pop(self) -> ShellResult:
        """Pop stashed changes."""
        return self.runner.run("git stash pop")

    def generate_commit_message(self, module_name: str, files: List[str]) -> str:
        """Generate a commit message for generated module files."""
        file_types = {}
        for f in files:
            ext = Path(f).suffix or "other"
            file_types[ext] = file_types.get(ext, 0) + 1

        parts = [f"feat({module_name}):"]
        for ext, count in sorted(file_types.items()):
            parts.append(f"- add {count} {ext[1:] if ext != 'other' else 'file'} files")

        return " ".join(parts)


# Convenience function
def git_status(cwd: str = None) -> GitStatus:
    """Get git status."""
    return GitClient(cwd=cwd).status()


def git_commit(message: str, files: List[str] = None, cwd: str = None) -> ShellResult:
    """Commit files."""
    return GitClient(cwd=cwd).commit(message, files)
