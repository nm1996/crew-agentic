"""Workflow orchestrator: owns run state, stage execution and developer gates.

The engine is a loop over persisted state. Each iteration reloads the run,
executes the handler for its current state and persists a validated transition.
Because every stage starts from persisted state, a crashed or interrupted run can
be resumed by simply driving the loop again after :func:`recovery.reconcile`.

Concurrency is limited to the IMPLEMENTING stage, where up to
``execution.max_implementers`` provider processes run under asyncio.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from crew.agents import payloads
from crew.agents.contracts import DirectorPlan, EvaluationReport, ImplementationReport, ReviewReport
from crew.agents.runtime import AgentRuntime
from crew.config.loader import ProjectPaths
from crew.config.schema import CrewConfig
from crew.context.collector import collect_context
from crew.domain.enums import (
    GATE_STATES,
    ApprovalDecision,
    ApprovalKind,
    ExecutionStatus,
    Role,
    RunState,
    TaskKind,
    TaskStatus,
)
from crew.domain.errors import (
    CrewError,
    InvalidTransitionError,
    PlanValidationError,
    PreconditionError,
)
from crew.domain.task_graph import Node, is_blocked, is_ready, topological_order, validate_graph
from crew.orchestrator import recovery
from crew.orchestrator.lock import RunLock
from crew.orchestrator.state_machine import is_terminal
from crew.persistence.models import JsonMixin, Run, Task, utcnow
from crew.persistence.store import Store
from crew.providers.base import AgentProvider
from crew.reporting import renderer
from crew.security.execution_policy import parse_verification_command, run_command
from crew.workspace.git import Git
from crew.workspace.worktrees import WorkspaceManager

log = logging.getLogger(__name__)

Notify = Callable[[str], None]
T = TypeVar("T")

# How often running stages check for a developer cancellation request.
CANCEL_POLL_SECONDS = 0.5


class RunCancelled(Exception):
    """Raised inside a stage when the developer requested cancellation."""


@dataclass
class Project:
    """Everything a use case needs about one CREW-initialised repository."""

    paths: ProjectPaths
    config: CrewConfig
    store: Store
    git: Git
    workspace: WorkspaceManager


class Orchestrator:
    def __init__(self, project: Project, provider: AgentProvider, notify: Notify | None = None):
        self.p = project
        self.store = project.store
        self.config = project.config
        self.provider = provider
        self.runtime = AgentRuntime(project.store, provider, project.config)
        self.notify: Notify = notify or (lambda _msg: None)
        self._handlers: dict[RunState, Callable[[Run], None]] = {
            RunState.CREATED: self._start,
            RunState.ANALYZING: self._analyze,
            RunState.PLANNING: self._plan,
            RunState.IMPLEMENTING: self._implement,
            RunState.INTEGRATING: self._integrate,
            RunState.REVIEWING: self._review,
            RunState.CHANGES_REQUESTED: self._request_changes,
            RunState.EXTERNAL_EVALUATION: self._evaluate,
        }

    # ======================================================================
    # Public use cases
    # ======================================================================

    def start_run(self, request: str) -> Run:
        request = request.strip()
        if not request:
            raise PreconditionError("The request must not be empty.")
        head = self.p.git.head()
        if head is None:
            raise PreconditionError(
                "The repository has no commits yet.", hint="Create an initial commit first."
            )
        status = self.p.git.status()
        run = self.store.create_run(
            request=request,
            provider=self.provider.name,
            base_branch=status.branch,
            base_commit=head,
        )
        self.store.update_run(
            run.id, integration_branch=self.p.workspace.integration_branch(run.id)
        )
        if not status.is_clean:
            msg = (
                f"working tree has {len(status.dirty_paths)} uncommitted change(s); planning uses "
                "the last commit and implementation will require a clean tree"
            )
            self.store.add_event(run.id, "dirty_worktree_warning", msg)
            self.notify(f"[warning] {msg}")
        self.notify(f"Run {run.id} created")
        return self.drive(run.id)

    def drive(self, run_id: str) -> Run:
        with self._lock(run_id):
            return self._drive_locked(run_id)

    def approve_plan(self, run_id: str, note: str | None = None, *, proceed: bool = True) -> Run:
        with self._lock(run_id):
            run = self._require_state(run_id, RunState.AWAITING_PLAN_APPROVAL)
            # Never implement on top of a dirty user tree (SECURITY.md §6).
            self.p.workspace.require_clean_user_worktree()
            self.store.add_approval(run.id, ApprovalKind.PLAN, ApprovalDecision.APPROVED, note)
            self._transition(run.id, RunState.IMPLEMENTING, "plan approved by developer")
            if not proceed:
                return self.store.get_run(run_id)
            return self._drive_locked(run_id)

    def reject_plan(self, run_id: str, note: str | None = None) -> Run:
        with self._lock(run_id):
            self._require_state(run_id, RunState.AWAITING_PLAN_APPROVAL)
            self.store.add_approval(run_id, ApprovalKind.PLAN, ApprovalDecision.REJECTED, note)
            self._transition(run_id, RunState.PLAN_REJECTED, note or "plan rejected by developer")
            self._cleanup_clean_worktrees(run_id)
            return self._finish(run_id)

    def accept(self, run_id: str, note: str | None = None) -> Run:
        with self._lock(run_id):
            self._require_state(run_id, RunState.AWAITING_FINAL_APPROVAL)
            self.store.add_approval(run_id, ApprovalKind.FINAL, ApprovalDecision.APPROVED, note)
            self._transition(run_id, RunState.COMPLETED, "result accepted by developer")
            self._cleanup_clean_worktrees(run_id)
            return self._finish(run_id)

    def reject(self, run_id: str, note: str | None = None) -> Run:
        with self._lock(run_id):
            self._require_state(run_id, RunState.AWAITING_FINAL_APPROVAL)
            self.store.add_approval(run_id, ApprovalKind.FINAL, ApprovalDecision.REJECTED, note)
            self._transition(run_id, RunState.REJECTED, note or "result rejected by developer")
            self._cleanup_clean_worktrees(run_id)
            return self._finish(run_id)

    def stop(self, run_id: str) -> tuple[Run, bool]:
        """Cancel a run. Returns (run, immediate). Cooperative when a driver is active."""
        run = self.store.get_run(run_id)
        if is_terminal(RunState(run.state)):
            raise InvalidTransitionError(f"Run {run_id} is already {run.state}.")
        lock = RunLock(self._lock_path(run_id))
        if lock.try_acquire():
            try:
                # No driver is alive, but a killed one may have left agent processes behind.
                for note in recovery.reconcile_processes(self.p, run):
                    self.notify(f"[recovery] {note}")
                for task in self.store.tasks(run_id):
                    if task.status in (TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.READY):
                        self.store.set_task_status(task, TaskStatus.CANCELLED, "run cancelled")
                self._transition(
                    run_id,
                    RunState.CANCELLED,
                    "cancelled by developer",
                    failure_reason="cancelled by developer",
                )
                return self._finish(run_id), True
            finally:
                lock.release()
        self.store.request_cancel(run_id)
        return self.store.get_run(run_id), False

    def resume(self, run_id: str) -> Run:
        with self._lock(run_id):
            run = self.store.get_run(run_id)
            if is_terminal(RunState(run.state)):
                raise InvalidTransitionError(
                    f"Run {run_id} is {run.state} and cannot be resumed.",
                    hint="Start a new run with `crew run`.",
                )
            notes = recovery.reconcile(self.p, run)
            for note in notes:
                self.notify(f"[recovery] {note}")
            return self._drive_locked(run_id)

    def write_report(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        content = renderer.render_final_report(self.store, run)
        renderer.write_artifact(
            self.store, self.p.paths.run_dir(run_id), run_id, "final-report.md", content
        )
        return content

    # ======================================================================
    # Loop
    # ======================================================================

    def _drive_locked(self, run_id: str) -> Run:
        while True:
            run = self.store.get_run(run_id)
            state = RunState(run.state)
            if is_terminal(state):
                return self._finish(run_id)
            # Checked before the gate test: a cancel requested while a stage was
            # running must not survive the gate and fire after a later approval.
            if run.cancel_requested:
                self._cancel(run_id)
                continue
            if state in GATE_STATES:
                return self._finish(run_id)
            handler = self._handlers[state]
            try:
                handler(run)
            except RunCancelled:
                self._cancel(run_id)
            except CrewError as exc:
                self._fail(run_id, exc.message)
            except Exception as exc:  # a bug: keep evidence, fail the run, surface it
                log.exception("internal error in state %s", state)
                self._fail(run_id, f"internal error in {state}: {exc!r}")

    def _finish(self, run_id: str) -> Run:
        self.write_report(run_id)
        return self.store.get_run(run_id)

    # ======================================================================
    # Stage handlers (one per non-gate active state)
    # ======================================================================

    def _start(self, run: Run) -> None:
        renderer.write_artifact(
            self.store, self._run_dir(run), run.id, "request.md", renderer.render_request(run)
        )
        self._transition(run.id, RunState.ANALYZING)

    def _analyze(self, run: Run) -> None:
        director_wt = self.p.workspace.ensure_director_worktree(run.id, run.base_commit)
        ctx = collect_context(
            director_wt,
            notes_dir=self.p.paths.context_dir,
            max_files=self.config.context.max_files_listed,
            exclude=tuple(self.config.context.exclude),
        )
        renderer.write_artifact(
            self.store, self._run_dir(run), run.id, "context.md", ctx.to_markdown()
        )
        self._transition(run.id, RunState.PLANNING, f"collected context: {ctx.total_files} files")

    def _plan(self, run: Run) -> None:
        context_file = self._run_dir(run) / "context.md"
        if not context_file.exists():  # e.g. deleted between crash and resume
            self._transition(
                run.id,
                RunState.FAILED,
                "context artifact missing",
                failure_reason="context artifact missing; start a new run",
            )
            return
        context_md = context_file.read_text(encoding="utf-8")
        director_wt = self.p.workspace.ensure_director_worktree(run.id, run.base_commit)
        self.notify("Director is planning…")
        error: str | None = None
        for _attempt in range(2):
            plan = self._invoke(
                Role.DIRECTOR,
                run,
                payloads.director_prompt(run, context_md, error),
                director_wt,
            )
            assert isinstance(plan, DirectorPlan)
            try:
                order = validate_graph([Node(t.id, tuple(t.dependencies)) for t in plan.tasks])
                break
            except PlanValidationError as exc:
                error = exc.message
                self.store.add_event(run.id, "plan_invalid", error)
                self.notify(f"[warning] {error}; asking Director to correct it")
        else:
            raise PlanValidationError(f"Director could not produce a valid plan: {error}")

        position = {task_id: i for i, task_id in enumerate(order)}
        tasks = [
            Task(
                key=t.id,
                kind=TaskKind.PLANNED,
                cycle=0,
                seq=position[t.id],
                title=t.title,
                description=t.description,
                rationale=t.rationale,
                dependencies_json=JsonMixin.dumps(t.dependencies),
                acceptance_json=JsonMixin.dumps(t.acceptance_criteria),
                expected_files_json=JsonMixin.dumps(t.expected_files),
                status=TaskStatus.PENDING,
            )
            for t in plan.tasks
        ]
        plan_dict = plan.model_dump(mode="json")
        renderer.write_artifact(
            self.store, self._run_dir(run), run.id, "plan.md", renderer.render_plan(plan_dict)
        )
        self._transition(
            run.id,
            RunState.AWAITING_PLAN_APPROVAL,
            f"plan with {len(tasks)} task(s) awaiting approval",
            new_tasks=tasks,
            plan_json=JsonMixin.dumps(plan_dict),
        )

    # ---- implementation --------------------------------------------------

    def _implement(self, run: Run) -> None:
        self.p.workspace.ensure_integration_worktree(run.id, run.base_commit)
        asyncio.run(self._implement_async(run))
        tasks = self.store.tasks(run.id, cycle=run.correction_cycle)
        if self.store.is_cancel_requested(run.id):
            raise RunCancelled
        unfinished = [t for t in tasks if t.status != TaskStatus.COMPLETED]
        if unfinished:
            summary = ", ".join(f"{t.key}={t.status}" for t in unfinished)
            raise CrewError(f"implementation did not complete: {summary}")
        self._write_implementation_artifact(run)
        self._transition(run.id, RunState.INTEGRATING, f"{len(tasks)} task(s) completed")

    async def _implement_async(self, run: Run) -> None:
        limit = self.config.execution.max_implementers
        running: dict[str, asyncio.Task[None]] = {}
        try:
            while True:
                if self.store.is_cancel_requested(run.id):
                    for t in running.values():
                        t.cancel()
                    await asyncio.gather(*running.values(), return_exceptions=True)
                    return
                tasks = self.store.tasks(run.id, cycle=run.correction_cycle)
                statuses = {t.key: TaskStatus(t.status) for t in tasks}
                waiting = [
                    t
                    for t in tasks
                    if t.status in (TaskStatus.PENDING, TaskStatus.READY) and t.key not in running
                ]
                for t in waiting:
                    if is_blocked(t.dependencies, statuses):
                        self.store.set_task_status(t, TaskStatus.BLOCKED, "a dependency failed")
                        statuses[t.key] = TaskStatus.BLOCKED
                ready = [
                    t
                    for t in waiting
                    if statuses[t.key] is not TaskStatus.BLOCKED
                    and is_ready(t.dependencies, statuses)
                ]
                for t in ready:
                    if t.status == TaskStatus.PENDING:
                        t = self.store.set_task_status(t, TaskStatus.READY)
                    if len(running) >= limit:
                        continue
                    execution_id = uuid.uuid4().hex[:16]
                    claimed = self.store.claim_task(t, execution_id)
                    if claimed is None:
                        continue
                    self.notify(f"Implementer started: {claimed.key} (attempt {claimed.attempt})")
                    running[claimed.key] = asyncio.create_task(
                        self._run_task(run, claimed, execution_id)
                    )
                if not running:
                    return
                await asyncio.wait(
                    running.values(),
                    timeout=CANCEL_POLL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for key in [k for k, t in running.items() if t.done()]:
                    finished = running.pop(key)
                    if not finished.cancelled() and (exc := finished.exception()):
                        raise exc
        finally:
            for t in running.values():
                t.cancel()
            if running:
                await asyncio.gather(*running.values(), return_exceptions=True)

    async def _run_task(self, run: Run, task: Task, execution_id: str) -> None:
        ws = self.p.workspace
        try:
            deps = [self.store.get_task(run.id, d) for d in task.dependencies]
            path, branch = ws.create_task_worktree(
                run.id,
                task.key,
                task.attempt,
                start_point=ws.integration_branch(run.id),
                dependency_branches=[d.branch for d in deps if d.branch],
            )
            wt = self.p.git.at(path)
            start_commit = wt.head()
            task = self.store.update_task(
                task.pk, worktree_path=str(path), branch=branch, start_commit=start_commit
            )
            prompt = payloads.implementer_prompt(run, task, {d.key: d.report for d in deps})
            report = await self.runtime.invoke(
                Role.IMPLEMENTER,
                run_id=run.id,
                prompt=prompt,
                cwd=path,
                cycle=run.correction_cycle,
                task=task,
                execution_id=execution_id,
            )
            assert isinstance(report, ImplementationReport)
            if wt.current_branch() != branch:
                raise PreconditionError(f"agent left branch {branch}; refusing to record result")
            sha = wt.commit_all(f"crew({task.key}): {task.title}\n\nCREW run {run.id}")
            head = wt.head()
            assert head and start_commit
            changed = wt.changed_files(start_commit, head)
            if not changed:
                self.store.add_event(
                    run.id, "task_no_changes", "task produced no changes", task_key=task.key
                )
            self.store.set_task_status(
                task,
                TaskStatus.COMPLETED,
                f"{len(changed)} file(s) changed",
                commit_sha=head,
                changed_files_json=JsonMixin.dumps(changed),
                report_json=report.model_dump_json(),
                finished_at=utcnow(),
            )
            self.notify(
                f"Implementer finished: {task.key} ({len(changed)} file(s), {(sha or head)[:10]})"
            )
        except asyncio.CancelledError:
            if self.store.is_cancel_requested(run.id):
                self.store.set_task_status(
                    task, TaskStatus.CANCELLED, "run cancelled", finished_at=utcnow()
                )
            else:  # process interrupted (Ctrl-C): leave for recovery to re-dispatch
                self.store.set_task_status(
                    task,
                    TaskStatus.PENDING,
                    "interrupted",
                    failure_reason="interrupted; previous worktree preserved",
                )
            raise
        except CrewError as exc:
            failures = task.failures + 1
            retry = failures < self.config.execution.max_task_attempts
            self.store.set_task_status(
                task,
                TaskStatus.PENDING if retry else TaskStatus.FAILED,
                f"attempt {task.attempt} failed: {exc.message}" + ("; retrying" if retry else ""),
                failures=failures,
                failure_reason=exc.message,
                finished_at=None if retry else utcnow(),
            )
            self.notify(f"[warning] task {task.key} attempt {task.attempt} failed: {exc.message}")

    # ---- integration, review, evaluation ---------------------------------

    def _integrate(self, run: Run) -> None:
        ws = self.p.workspace
        path = ws.integration_path(run.id)
        if not path.exists():
            ws.ensure_integration_worktree(run.id, run.base_commit)
        igit = self.p.git.at(path)
        tasks = {t.key: t for t in self.store.tasks(run.id, cycle=run.correction_cycle)}
        order = topological_order(
            {k: [d for d in t.dependencies if d in tasks] for k, t in tasks.items()}
        )
        for key in order or list(tasks):
            task = tasks[key]
            if task.integrated or task.status != TaskStatus.COMPLETED:
                continue
            assert task.branch and task.commit_sha
            if not igit.is_ancestor(task.commit_sha, "HEAD") and not igit.merge_no_ff(
                task.branch, f"crew: integrate {task.key} — {task.title}"
            ):
                conflicts = igit.conflicted_files()
                igit.merge_abort()
                raise CrewError(
                    f"merge conflict integrating task {task.key} in: {', '.join(conflicts)}. "
                    f"Task worktrees and branches were preserved under {ws.run_dir(run.id)}."
                )
            self.store.update_task(task.pk, integrated=True)
            self.store.add_event(run.id, "task_integrated", task.branch, task_key=task.key)
        self.notify("Integration complete; running verification commands…")
        self._run_cancellable(run.id, self._verify(run, path))
        renderer.write_artifact(
            self.store,
            self._run_dir(run),
            run.id,
            "test-results.md",
            renderer.render_commands(self.store.commands(run.id, purpose="verification")),
        )
        self._transition(run.id, RunState.REVIEWING)

    async def _verify(self, run: Run, cwd: Any) -> None:
        execution = self.config.execution
        for command in execution.verification_commands:
            argv = parse_verification_command(command)
            result = await run_command(
                argv,
                cwd=cwd,
                timeout_seconds=execution.command_timeout_seconds,
                output_limit_bytes=execution.command_output_limit_bytes,
            )
            self.store.record_command(
                run_id=run.id,
                purpose="verification",
                category=result.category,
                argv_json=JsonMixin.dumps(result.argv),
                cwd=result.cwd,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_seconds=result.duration_seconds,
                stdout=result.stdout,
                stderr=result.stderr,
                cycle=run.correction_cycle,
            )
            status = "timed out" if result.timed_out else f"exit {result.exit_code}"
            self.notify(f"  {command} → {status}")

    def _integrated_diff(self, run: Run) -> str:
        return self.p.git.diff(run.base_commit, self.p.workspace.integration_branch(run.id))

    def _cycle_commands(self, run: Run) -> list:
        return [
            c
            for c in self.store.commands(run.id, purpose="verification")
            if c.cycle == run.correction_cycle
        ]

    def _review(self, run: Run) -> None:
        self.notify("Reviewer is inspecting the integrated diff…")
        previous = (
            self.store.latest_output(run.id, Role.REVIEWER, cycle=run.correction_cycle - 1)
            if run.correction_cycle
            else None
        )
        review = self._invoke(
            Role.REVIEWER,
            run,
            payloads.reviewer_prompt(
                run,
                self.store.tasks(run.id),
                self._integrated_diff(run),
                self._cycle_commands(run),
                previous,
            ),
            self.p.workspace.integration_path(run.id),
        )
        assert isinstance(review, ReviewReport)
        self._write_review_artifact(run)
        blocking = review.blocking_findings
        if not blocking:
            self._transition(run.id, RunState.EXTERNAL_EVALUATION, "no blocking findings")
        elif run.correction_cycle < self.config.execution.max_correction_cycles:
            self._transition(
                run.id, RunState.CHANGES_REQUESTED, f"{len(blocking)} blocking finding(s)"
            )
        else:
            msg = (
                f"correction cycle limit ({self.config.execution.max_correction_cycles}) reached "
                f"with {len(blocking)} unresolved blocking finding(s); developer decision required"
            )
            self.store.add_event(run.id, "correction_limit_reached", msg)
            self.notify(f"[warning] {msg}")
            self._transition(run.id, RunState.EXTERNAL_EVALUATION, msg)

    def _request_changes(self, run: Run) -> None:
        review = self.store.latest_output(run.id, Role.REVIEWER, cycle=run.correction_cycle) or {}
        blocking = [f for f in review.get("findings", []) if f.get("blocking")]
        cycle = run.correction_cycle + 1
        task = Task(
            key=f"fix-{cycle}",
            kind=TaskKind.CORRECTION,
            cycle=cycle,
            seq=0,
            title=f"Address blocking review findings (cycle {cycle})",
            description=payloads.correction_description(blocking),
            rationale="Reviewer reported blocking findings",
            dependencies_json="[]",
            acceptance_json=JsonMixin.dumps([f"Resolved: {f['title']}" for f in blocking]),
            expected_files_json=JsonMixin.dumps(
                sorted({f["file"] for f in blocking if f.get("file")})
            ),
            status=TaskStatus.PENDING,
        )
        self._transition(
            run.id,
            RunState.IMPLEMENTING,
            f"correction cycle {cycle} with {len(blocking)} finding(s)",
            new_tasks=[task],
            correction_cycle=cycle,
        )

    def _evaluate(self, run: Run) -> None:
        self.notify("External Evaluator is assessing the result…")
        reviews = [
            e.output
            for e in self.store.executions(
                run.id, role=Role.REVIEWER, status=ExecutionStatus.SUCCEEDED
            )
            if e.output
        ]
        evaluation = self._invoke(
            Role.EVALUATOR,
            run,
            payloads.evaluator_prompt(
                run,
                self.store.tasks(run.id),
                self._integrated_diff(run),
                self._cycle_commands(run),
                reviews,
            ),
            self.p.workspace.integration_path(run.id),
        )
        assert isinstance(evaluation, EvaluationReport)
        renderer.write_artifact(
            self.store,
            self._run_dir(run),
            run.id,
            "external-evaluation.md",
            renderer.render_evaluation(evaluation.model_dump(mode="json")),
        )
        self._transition(
            run.id,
            RunState.AWAITING_FINAL_APPROVAL,
            f"evaluator recommends {evaluation.recommendation}",
        )

    # ======================================================================
    # Helpers
    # ======================================================================

    def _invoke(self, role: Role, run: Run, prompt: str, cwd: Any) -> Any:
        return self._run_cancellable(
            run.id,
            self.runtime.invoke(
                role, run_id=run.id, prompt=prompt, cwd=cwd, cycle=run.correction_cycle
            ),
        )

    def _run_cancellable(self, run_id: str, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` to completion, cancelling it if the developer stops the run."""

        async def watch() -> T:
            task = asyncio.ensure_future(coro)
            while True:
                done, _ = await asyncio.wait({task}, timeout=CANCEL_POLL_SECONDS)
                if done:
                    return task.result()
                if self.store.is_cancel_requested(run_id):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    raise RunCancelled

        return asyncio.run(watch())

    def _transition(self, run_id: str, to: RunState, message: str = "", **fields: Any) -> Run:
        run = self.store.transition(run_id, to, message=message, **fields)
        self.notify(f"→ {to}" + (f": {message}" if message else ""))
        return run

    def _fail(self, run_id: str, reason: str) -> None:
        self._transition(run_id, RunState.FAILED, reason, failure_reason=reason)

    def _cancel(self, run_id: str) -> None:
        self._transition(
            run_id,
            RunState.CANCELLED,
            "cancelled by developer",
            failure_reason="cancelled by developer",
        )

    def _require_state(self, run_id: str, state: RunState) -> Run:
        run = self.store.get_run(run_id)
        if run.state != state:
            raise InvalidTransitionError(f"Run {run_id} is {run.state}, expected {state}.")
        return run

    def _lock_path(self, run_id: str):
        return self.p.paths.run_dir(run_id) / ".lock"

    def _lock(self, run_id: str) -> RunLock:
        self.store.get_run(run_id)  # NotFoundError before touching the filesystem
        return RunLock(self._lock_path(run_id))

    def _run_dir(self, run: Run):
        return self.p.paths.run_dir(run.id)

    def _cleanup_clean_worktrees(self, run_id: str) -> None:
        """Remove CREW worktrees that hold no uncommitted work. Branches are kept."""
        ws = self.p.workspace
        run_dir = ws.run_dir(run_id)
        if not run_dir.exists():
            return
        for child in sorted(run_dir.iterdir()):
            if child.is_dir() and ws.owned(child) and not ws.remove_worktree_if_clean(child):
                self.store.add_event(run_id, "worktree_preserved", str(child))

    def _write_implementation_artifact(self, run: Run) -> None:
        lines = ["# Implementation", ""]
        for t in self.store.tasks(run.id):
            report = t.report or {}
            lines += [
                f"## `{t.key}` — {t.title} ({t.status})",
                "",
                f"- Branch: `{t.branch}`  Commit: `{(t.commit_sha or '-')[:12]}`",
                f"- Changed files: {', '.join(t.changed_files) or '-'}",
                f"- Summary (implementer claim): {report.get('summary', '-')}",
            ]
            for v in report.get("verification", []):
                lines.append(f"  - claimed `{v['command']}` → {v.get('exit_code')}: {v['summary']}")
            if report.get("limitations"):
                lines.append(f"- Limitations: {'; '.join(report['limitations'])}")
            lines.append("")
        renderer.write_artifact(
            self.store, self._run_dir(run), run.id, "implementation.md", "\n".join(lines)
        )

    def _write_review_artifact(self, run: Run) -> None:
        reviews = [
            (e.cycle, e.output)
            for e in self.store.executions(
                run.id, role=Role.REVIEWER, status=ExecutionStatus.SUCCEEDED
            )
            if e.output
        ]
        renderer.write_artifact(
            self.store, self._run_dir(run), run.id, "review.md", renderer.render_review(reviews)
        )
