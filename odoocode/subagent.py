# forge/subagent.py
"""
Parallel Subagent Execution for OdooCode.
Asyncio-based file generation with dependency-aware scheduling.
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional, Callable, Any
from collections import defaultdict

logger = logging.getLogger("Forge.subagent")


@dataclass
class WorkUnit:
    """A single unit of work (one file to generate)."""
    filepath: str
    depends_on: List[str] = field(default_factory=list)
    spec: str = ""
    description: str = ""
    status: str = "pending"  # pending, running, completed, failed
    content: str = ""
    error: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    attempts: int = 0

    @property
    def duration_ms(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0


class DependencyGraph:
    """Manages file dependencies and determines execution order."""

    def __init__(self):
        self._units: Dict[str, WorkUnit] = {}
        self._dependents: Dict[str, Set[str]] = defaultdict(set)  # file -> files that depend on it
        self._deps_remaining: Dict[str, int] = {}  # file -> count of unmet dependencies

    def add_unit(self, unit: WorkUnit):
        """Add a work unit to the graph."""
        self._units[unit.filepath] = unit
        self._deps_remaining[unit.filepath] = len(unit.depends_on)
        for dep in unit.depends_on:
            self._dependents[dep].add(unit.filepath)

    def get_ready(self) -> List[WorkUnit]:
        """Get all work units whose dependencies are satisfied."""
        ready = []
        for fp, count in self._deps_remaining.items():
            if count == 0 and self._units[fp].status == "pending":
                ready.append(self._units[fp])
        return ready

    def mark_complete(self, filepath: str):
        """Mark a work unit as complete and decrement dependents' counters."""
        self._units[filepath].status = "completed"
        for dependent in self._dependents.get(filepath, set()):
            if dependent in self._deps_remaining:
                self._deps_remaining[dependent] -= 1

    def mark_failed(self, filepath: str):
        """Mark a work unit as failed."""
        self._units[filepath].status = "failed"

    @property
    def all_done(self) -> bool:
        """Check if all work units are completed or failed."""
        return all(u.status in ("completed", "failed") for u in self._units.values())

    @property
    def stats(self) -> dict:
        statuses = defaultdict(int)
        for u in self._units.values():
            statuses[u.status] += 1
        return dict(statuses)


class SharedContext:
    """Thread-safe shared context for parallel subagents."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._completed_files: Dict[str, str] = {}  # filepath -> content
        self._generated_so_far: Dict[str, str] = {}  # all generated content

    async def add_completed(self, filepath: str, content: str):
        """Record a completed file."""
        async with self._lock:
            self._completed_files[filepath] = content
            self._generated_so_far[filepath] = content

    def get_completed(self) -> Dict[str, str]:
        """Get all completed files (synchronous read is safe)."""
        return dict(self._completed_files)

    def get_team_context(self, exclude: str = "", max_per_file: int = 2000) -> str:
        """Build team context string for a subagent."""
        completed_snapshot = self.get_completed()
        if not completed_snapshot:
            return ""
        lines = ["[TEAM SHARED MEMORY — files already generated]\n"
                 "Reference their field names, model names, and XML IDs exactly.\n"]
        for fp, content in completed_snapshot.items():
            if fp == exclude:
                continue
            snippet = content[:max_per_file]
            if len(content) > max_per_file:
                snippet += "\n...[truncated]"
            lines.append(f"--- TEAMMATE FILE: {fp} ---\n{snippet}\n")
        return "\n".join(lines)


class SubagentRunner:
    """
    Runs file generation in parallel using asyncio + thread pool.

    Usage:
        runner = SubagentRunner(llm_client, config, max_workers=4)
        results = await runner.run_all(work_units, blueprint_summary, memory_context)
    """

    def __init__(self, llm_client, config, max_workers: int = None):
        self.llm = llm_client
        self.config = config
        self.max_workers = max_workers or getattr(config, 'max_parallel_files', 4)
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self._shared = SharedContext()

    async def run_all(
        self,
        work_units: List[WorkUnit],
        blueprint_summary: str,
        memory_context: str = "",
        generate_fn: Callable = None,
        validate_fn: Callable = None,
        critic_fn: Callable = None,
        progress_callback: Callable = None,
    ) -> Dict[str, WorkUnit]:
        """
        Run all work units with dependency-aware parallel scheduling.

        Returns dict of filepath -> WorkUnit with results.
        """
        # Build dependency graph
        graph = DependencyGraph()
        for unit in work_units:
            graph.add_unit(unit)

        logger.info(f"Starting parallel execution: {len(work_units)} files, "
                     f"max_workers={self.max_workers}")

        # Run the scheduling loop
        pending_tasks: Dict[str, asyncio.Task] = {}

        while not graph.all_done:
            # Get ready units (dependencies satisfied)
            ready = graph.get_ready()

            for unit in ready:
                if unit.filepath not in pending_tasks:
                    task = asyncio.create_task(
                        self._run_unit(
                            unit, graph, blueprint_summary,
                            memory_context, generate_fn,
                            validate_fn, critic_fn,
                        )
                    )
                    pending_tasks[unit.filepath] = task

            # Wait for at least one task to complete before checking again
            if pending_tasks:
                done, _ = await asyncio.wait(
                    pending_tasks.values(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Clean up completed tasks
                completed_fps = [
                    fp for fp, task in pending_tasks.items()
                    if task.done()
                ]
                for fp in completed_fps:
                    del pending_tasks[fp]
            else:
                # No tasks running and none ready = deadlock or done
                break

        # Wait for any remaining tasks
        if pending_tasks:
            await asyncio.gather(*pending_tasks.values(), return_exceptions=True)

        return {fp: graph._units[fp] for fp in graph._units}

    async def _run_unit(
        self,
        unit: WorkUnit,
        graph: DependencyGraph,
        blueprint_summary: str,
        memory_context: str,
        generate_fn: Callable,
        validate_fn: Callable,
        critic_fn: Callable,
    ):
        """Execute a single work unit with retry logic."""
        unit.status = "running"
        unit.start_time = time.monotonic()
        unit.attempts += 1

        try:
            # Get team context from completed files
            team_ctx = self._shared.get_team_context(exclude=unit.filepath)

            # Run generation in thread pool (LLM calls are synchronous)
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(
                self._executor,
                self._generate_with_retry,
                unit, blueprint_summary, team_ctx,
                memory_context, generate_fn,
            )

            # Validate
            if validate_fn:
                ok, err = validate_fn(unit.filepath, content)
                if not ok:
                    logger.warning(f"Validation failed for {unit.filepath}: {err}")
                    # Try auto-fix
                    if generate_fn:
                        content = await loop.run_in_executor(
                            self._executor,
                            self._auto_fix,
                            unit, err, generate_fn,
                        )

            unit.content = content
            unit.status = "completed"
            unit.end_time = time.monotonic()

            # Record in shared context
            await self._shared.add_completed(unit.filepath, content)

            # Mark complete in graph
            graph.mark_complete(unit.filepath)

            logger.info(f"Completed {unit.filepath} ({unit.duration_ms:.0f}ms, "
                        f"attempt {unit.attempts})")

        except Exception as exc:
            unit.status = "failed"
            unit.error = str(exc) or repr(exc)
            unit.end_time = time.monotonic()
            graph.mark_failed(unit.filepath)
            logger.error(f"Failed {unit.filepath}: {unit.error}", exc_info=True)

    def _generate_with_retry(
        self,
        unit: WorkUnit,
        blueprint_summary: str,
        team_ctx: str,
        memory_context: str,
        generate_fn: Callable,
    ) -> str:
        """Generate file content with retry."""
        last_exc = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                content = generate_fn(
                    filepath=unit.filepath,
                    spec=unit.spec,
                    description=unit.description,
                    blueprint_summary=blueprint_summary,
                    team_context=team_ctx,
                    memory_context=memory_context,
                )
                return content
            except Exception as exc:
                last_exc = exc
                logger.warning(f"Generate attempt {attempt} failed for {unit.filepath}: {exc}")
                if attempt < self.config.max_retries:
                    time.sleep(2 ** attempt)

        raise RuntimeError(f"Generation failed after {self.config.max_retries} attempts: {last_exc}")

    def _auto_fix(self, unit: WorkUnit, error: str, generate_fn: Callable) -> str:
        """Attempt to auto-fix validation errors."""
        # Simple retry with error context
        return generate_fn(
            filepath=unit.filepath,
            spec=unit.spec,
            description=unit.description,
            blueprint_summary="",
            team_context="",
            memory_context="",
            feedback=f"FIX THIS ERROR: {error}",
        )

    def shutdown(self):
        """Shutdown the thread pool."""
        self._executor.shutdown(wait=False)


def build_work_units(blueprint) -> List[WorkUnit]:
    """Convert a blueprint list to work units."""
    units = []
    for bf in blueprint:
        units.append(WorkUnit(
            filepath=bf.filepath,
            depends_on=bf.depends_on or [],
            spec=bf.spec or "",
            description=bf.description or "",
        ))
    return units


def get_parallel_stats(units: List[WorkUnit]) -> dict:
    """Get execution statistics."""
    completed = [u for u in units if u.status == "completed"]
    failed = [u for u in units if u.status == "failed"]
    total_time = sum(u.duration_ms for u in completed) if completed else 0
    avg_time = total_time / len(completed) if completed else 0

    return {
        "total": len(units),
        "completed": len(completed),
        "failed": len(failed),
        "total_time_ms": total_time,
        "avg_time_ms": avg_time,
        "max_workers": 0,  # Set by runner
    }
