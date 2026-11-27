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
import json
import logging
import threading
import uuid
from collections.abc import Callable, Coroutine, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
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
    BudgetExceededError,
    CrewError,
    InvalidTransitionError,
    MergeConflictError,
    PlanValidationError,
    PreconditionError,
    ProviderError,
)
from crew.domain.task_graph import (
    RESERVED_PREFIX,
    Node,
    is_blocked,
    is_ready,
    order_shared_files,
    topological_order,
    validate_graph,
)
from crew.integrations.base import CodeHost, DocPage, DocsTarget, IssueTracker, PublishResult
from crew.orchestrator import recovery
from crew.orchestrator.lock import RunLock
from crew.orchestrator.state_machine import is_terminal, retry_target
from crew.persistence.models import JsonMixin, Run, Task, utcnow
from crew.persistence.store import Store
from crew.providers.base import AgentProvider
from crew.reporting import renderer
from crew.security.execution_policy import (
    classify_command,
    parse_verification_command,
    run_command,
)
from crew.security.redaction import redact_args
from crew.security.sandbox import SandboxSettings, resolves_on_host
from crew.security.sandbox import check_available as check_sandbox
from crew.security.sandbox import wrap as wrap_in_sandbox
from crew.workspace.git import Git
from crew.workspace.worktrees import WorkspaceManager

log = logging.getLogger(__name__)

Notify = Callable[[str], None]
# Shown a permission request (dict from the permission server); returns
# "allow" (once), "allow_run" (for the rest of the run) or "deny".
AskPermission = Callable[[dict[str, Any]], str]
T = TypeVar("T")

# Linked documentation (Confluence pages, …) added to the Director's context.
LINKED_DOCS_MAX_CHARS = 30_000

# How often running stages check for a developer cancellation request.
CANCEL_POLL_SECONDS = 0.5

# The run whose work is in progress; lets the git recorder attribute commands.
CURRENT_RUN: ContextVar[str | None] = ContextVar("crew_current_run", default=None)


class RunCancelled(Exception):
    """Raised inside a stage when the developer requested cancellation."""


PERMISSION_POLL_SECONDS = 0.3


async def _stop(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _read_json_list(path: Path) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _pr_title(run: Run) -> str:
    first = run.request.strip().splitlines()[0] if run.request.strip() else run.id
    title = first if len(first) <= 72 else first[:69] + "…"
    if run.issue_key and not title.startswith(run.issue_key):
        title = f"{run.issue_key}: {title}"
    return title


def _pr_number(url: str | None) -> int | None:
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _expect[M](value: object, cls: type[M]) -> M:
    if not isinstance(value, cls):
        raise CrewError(f"expected {cls.__name__} from agent runtime, got {type(value).__name__}")
    return value


@dataclass
class CleanupReport:
    removed_worktrees: list[str] = field(default_factory=list)
    kept_worktrees: list[str] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    kept_branches: list[str] = field(default_factory=list)


@dataclass
class Project:
    """Everything a use case needs about one CREW-initialised repository."""

    paths: ProjectPaths
    config: CrewConfig
    store: Store
    git: Git
    workspace: WorkspaceManager


class Orchestrator:
    def __init__(
        self,
        project: Project,
        provider: AgentProvider,
        notify: Notify | None = None,
        ask_permission: AskPermission | None = None,
    ):
        self.p = project
        self.store = project.store
        self.config = project.config
        self.provider = provider
        self.ask_permission = ask_permission
        self.runtime = AgentRuntime(
            project.store, provider, project.config, permission_dir=self._permission_dir
        )
        self.notify: Notify = notify or (lambda _msg: None)
        self._rate_limited = False
        self._git_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None
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

    def start_run(
        self,
        request: str,
        *,
        issue_key: str | None = None,
        linked_docs: list[DocPage] | None = None,
    ) -> Run:
        """Create a run and drive it to the plan gate.

        ``linked_docs`` (e.g. Confluence pages) are added to the Director's
        context; ``issue_key`` links the run to an issue for `crew publish`.
        """
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
            run.id,
            integration_branch=self.p.workspace.integration_branch(run.id),
            issue_key=issue_key,
        )
        if linked_docs:
            renderer.write_artifact(
                self.store,
                self.p.paths.run_dir(run.id),
                run.id,
                "linked-docs.md",
                renderer.render_linked_docs(linked_docs),
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

    def retry(self, run_id: str, note: str | None = None) -> Run:
        """Retry a FAILED run from the stage it failed in.

        Failed, blocked and cancelled tasks of the current cycle get fresh
        attempts (in new worktrees; old ones are preserved). Useful after
        transient provider errors, timeouts or an exhausted budget.
        """
        with self._lock(run_id):
            run = self._require_state(run_id, RunState.FAILED)
            failed = RunState(run.failed_state) if run.failed_state else None
            target = retry_target(failed)
            if target is None:
                raise InvalidTransitionError(
                    f"Run {run_id} cannot be retried: the failed stage was not recorded.",
                    hint="Start a new run with `crew run`.",
                )
            for note_line in recovery.reconcile_processes(self.p, run):
                self.notify(f"[recovery] {note_line}")
            retryable = (TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED)
            for task in self.store.tasks(run_id, cycle=run.correction_cycle):
                if task.status in retryable:
                    self.store.set_task_status(
                        task, TaskStatus.PENDING, "reset for retry", failures=0, finished_at=None
                    )
            self.store.add_approval(run_id, ApprovalKind.RETRY, ApprovalDecision.APPROVED, note)
            self._transition(
                run_id,
                target,
                f"retry requested by developer (from {failed})",
                failure_reason=None,
                failed_state=None,
                cancel_requested=False,
            )
            return self._drive_locked(run_id)

    def publish(
        self,
        run_id: str,
        *,
        code_host: CodeHost | None = None,
        open_pr: bool = False,
        draft: bool = False,
        post_review: bool = False,
        issue_tracker: IssueTracker | None = None,
        docs: DocsTarget | None = None,
    ) -> PublishResult:
        """Share a COMPLETED run: push + PR, review, issue update, report page.

        The only place CREW talks to the outside world on its own initiative, and
        only because the developer ran `crew publish`. Every step is recorded.
        """
        with self._lock(run_id):
            run = self._require_state(run_id, RunState.COMPLETED)
            result = PublishResult()
            report = self.write_report(run_id)
            settings = self.config.integrations.code_host
            self.store.add_approval(run_id, ApprovalKind.PUBLISH, ApprovalDecision.APPROVED, None)

            if open_pr and code_host is not None and settings is not None:
                branch = self.p.workspace.integration_branch(run_id)
                self.p.git.push_owned_branch(
                    settings.remote, branch, f"{self.p.workspace.prefix}/{run_id}/"
                )
                result.pushed_branch = branch
                self.store.add_event(run_id, "published_branch", f"{settings.remote} {branch}")
                base = settings.base_branch or run.base_branch
                if not base:
                    raise PreconditionError("cannot open a PR: the run has no base branch")
                pr = code_host.open_pull_request(branch, base, _pr_title(run), report, draft=draft)
                result.pull_request = pr
                self.store.update_run(run_id, pull_request_url=pr.url)
                self.store.add_event(run_id, "pull_request_opened", pr.url)
                run = self.store.get_run(run_id)

            if post_review and code_host is not None:
                number = _pr_number(run.pull_request_url)
                if number is None:
                    raise PreconditionError("no pull request to review; publish with --pr first")
                review = self.store.latest_output(run_id, Role.REVIEWER) or {}
                code_host.post_review(number, renderer.render_review_comment(review))
                result.review_posted = True
                self.store.add_event(run_id, "review_posted", run.pull_request_url or "")

            if issue_tracker is not None and run.issue_key:
                tracker_settings = self.config.integrations.issue_tracker
                if tracker_settings is None or tracker_settings.comment_on_publish:
                    issue_tracker.comment(run.issue_key, renderer.render_issue_comment(run))
                    result.issue_commented = True
                    self.store.add_event(run_id, "issue_commented", run.issue_key)
                if tracker_settings is not None and tracker_settings.transition_on_publish:
                    issue_tracker.transition(run.issue_key, tracker_settings.transition_on_publish)
                    result.issue_transitioned = tracker_settings.transition_on_publish
                    self.store.add_event(
                        run_id, "issue_transitioned", tracker_settings.transition_on_publish
                    )

            if docs is not None:
                title = f"CREW report {run_id}: {_pr_title(run)}"
                result.doc_url = docs.publish_page(title, report)
                self.store.add_event(run_id, "report_published", result.doc_url)
            return result

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
        context_md = ctx.to_markdown()
        linked = self._run_dir(run) / "linked-docs.md"
        if linked.exists():
            context_md += "\n\n" + linked.read_text(encoding="utf-8")[:LINKED_DOCS_MAX_CHARS]
        renderer.write_artifact(self.store, self._run_dir(run), run.id, "context.md", context_md)
        self._transition(run.id, RunState.PLANNING, f"collected context: {ctx.total_files} files")

    def _plan(self, run: Run) -> None:
        context_file = self._run_dir(run) / "context.md"
        if not context_file.exists():  # e.g. deleted between crash and resume
            self._fail(run.id, "context artifact missing", retry_from=RunState.ANALYZING)
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
            plan = _expect(plan, DirectorPlan)
            try:
                order = validate_graph([Node(t.id, tuple(t.dependencies)) for t in plan.tasks])
                break
            except PlanValidationError as exc:
                error = exc.message
                self.store.add_event(run.id, "plan_invalid", error)
                self.notify(f"[warning] {error}; asking Director to correct it")
        else:
            raise PlanValidationError(f"Director could not produce a valid plan: {error}")

        added: dict[str, dict[str, list[str]]] = {}
        if self.config.execution.serialize_shared_files:
            added = order_shared_files(
                order,
                {t.id: t.dependencies for t in plan.tasks},
                {t.id: t.expected_files for t in plan.tasks},
            )
        for t in plan.tasks:
            for dep, shared in added.get(t.id, {}).items():
                t.dependencies.append(dep)
                self.store.add_event(
                    run.id,
                    "plan_serialized",
                    f"{t.id} now depends on {dep}: both change {', '.join(shared)}",
                    task_key=t.id,
                    data={"dependency": dep, "files": shared},
                )
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
        for t in plan_dict["tasks"]:
            if t["id"] in added:
                t["crew_added_dependencies"] = added[t["id"]]
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
        if unfinished and self._rate_limited:
            reason = next((t.failure_reason for t in tasks if t.failure_reason), "")
            raise CrewError(
                f"stopped: {reason or 'Claude usage limit reached'}. Completed tasks are kept; "
                "continue with `crew retry <run-id>` after the limit resets."
            )
        if unfinished:
            summary = ", ".join(
                f"{t.key}={t.status}" + (f" ({t.failure_reason})" if t.failure_reason else "")
                for t in unfinished
            )
            raise CrewError(f"implementation did not complete: {summary}")
        self._write_implementation_artifact(run)
        self._transition(run.id, RunState.INTEGRATING, f"{len(tasks)} task(s) completed")

    async def _implement_async(self, run: Run) -> None:
        self._rate_limited = False
        limit = self.config.execution.max_implementers
        running: dict[str, asyncio.Task[None]] = {}
        broker = self._start_permission_broker(run.id)
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
                if self._rate_limited:
                    ready = []  # let running tasks finish; dispatch nothing new
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
            await _stop(broker)

    async def _git[R](self, fn: Callable[..., R], *args: Any) -> R:
        """Run a blocking git call in a thread, one at a time.

        Off the event loop so other Implementers' agent processes keep streaming;
        serialized because concurrent git commands in one repository can collide on
        shared ref and worktree lock files.
        """
        loop = asyncio.get_running_loop()
        if self._git_lock is None or self._git_lock[0] is not loop:
            self._git_lock = (loop, asyncio.Lock())
        async with self._git_lock[1]:
            call = asyncio.ensure_future(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(call)
            except asyncio.CancelledError:
                # A git command cannot be interrupted halfway; let it finish before
                # the lock is released so the next one never overlaps it.
                await asyncio.wait({call})
                raise

    async def _run_task(self, run: Run, task: Task, execution_id: str) -> None:
        ws = self.p.workspace
        try:
            deps = [self.store.get_task(run.id, d) for d in task.dependencies]
            path, branch = await self._git(
                ws.create_task_worktree,
                run.id,
                task.key,
                task.attempt,
                ws.integration_branch(run.id),
            )
            wt = self.p.git.at(path)
            for dep in deps:
                if not dep.branch:
                    continue
                conflicts = await self._git(
                    ws.merge_into, path, dep.branch, f"crew: bring in dependency {dep.key}"
                )
                if conflicts:
                    await self._resolve_conflict(
                        run,
                        path,
                        dep.branch,
                        conflicts,
                        {d.key: d.report for d in deps},
                        into=f"task {task.key}",
                    )
            start_commit = await self._git(wt.head)
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
            report = _expect(report, ImplementationReport)
            if await self._git(wt.current_branch) != branch:
                raise PreconditionError(f"agent left branch {branch}; refusing to record result")
            commit = await self._git(
                wt.commit_all, f"crew({task.key}): {task.title}\n\nCREW run {run.id}"
            )
            if commit.excluded:
                self.store.add_event(
                    run.id,
                    "files_not_committed",
                    "secret-looking or oversized new files left uncommitted: "
                    + ", ".join(commit.excluded),
                    task_key=task.key,
                )
                self.notify(f"[warning] {task.key}: not committed: {', '.join(commit.excluded)}")
            head = await self._git(wt.head)
            if not head or not start_commit:
                raise CrewError(f"could not determine commits for task {task.key}")
            changed = await self._git(wt.changed_files, start_commit, head)
            if not changed:
                self.store.add_event(
                    run.id, "task_no_changes", "task produced no changes", task_key=task.key
                )
            unplanned = (
                sorted(set(changed) - set(task.expected_files)) if task.expected_files else []
            )
            if unplanned:
                # Not an error (scope notes explain it), but parallel tasks touching the
                # same unplanned file are the usual source of merge conflicts.
                self.store.add_event(
                    run.id,
                    "scope_expanded",
                    "changed files outside the plan: " + ", ".join(unplanned),
                    task_key=task.key,
                    data={"files": unplanned},
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
            self.notify(f"Implementer finished: {task.key} ({len(changed)} file(s), {head[:10]})")
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
            rate_limited = isinstance(exc, ProviderError) and exc.kind == "rate_limited"
            if rate_limited:
                # Every other agent would hit the same limit: stop dispatching new work.
                self._rate_limited = True
            retry = (
                failures < self.config.execution.max_task_attempts
                and not rate_limited
                and not isinstance(exc, BudgetExceededError | MergeConflictError)
            )
            self.store.set_task_status(
                task,
                TaskStatus.PENDING if retry else TaskStatus.FAILED,
                f"attempt {task.attempt} failed: {exc.message}" + ("; retrying" if retry else ""),
                failures=failures,
                failure_reason=exc.message,
                finished_at=None if retry else utcnow(),
            )
            self.notify(f"[warning] task {task.key} attempt {task.attempt} failed: {exc.message}")
        except Exception as exc:  # a bug or OS error: fail this task, keep the others running
            log.exception("unexpected error in task %s", task.key)
            self.store.set_task_status(
                task,
                TaskStatus.FAILED,
                f"unexpected error: {exc!r}",
                failures=task.failures + 1,
                failure_reason=f"unexpected error: {exc!r}",
                finished_at=utcnow(),
            )
            self.notify(f"[warning] task {task.key} failed unexpectedly: {exc!r}")

    # ---- integration, review, evaluation ---------------------------------

    def _integrate(self, run: Run) -> None:
        ws = self.p.workspace
        path = ws.integration_path(run.id)
        if not path.exists():
            ws.ensure_integration_worktree(run.id, run.base_commit)
        igit = self.p.git.at(path)
        if not igit.status().is_clean:
            raise CrewError(
                f"integration worktree {path} has uncommitted changes; refusing to merge into it"
            )
        tasks = {t.key: t for t in self.store.tasks(run.id, cycle=run.correction_cycle)}
        order = topological_order(
            {k: [d for d in t.dependencies if d in tasks] for k, t in tasks.items()}
        )
        for key in order or list(tasks):
            task = tasks[key]
            if task.integrated or task.status != TaskStatus.COMPLETED:
                continue
            if not task.branch or not task.commit_sha:
                raise CrewError(f"task {task.key} is COMPLETED but has no branch/commit recorded")
            conflicts = ws.merge_into(
                path, task.branch, f"crew: integrate {task.key} — {task.title}"
            )
            container = None
            if conflicts:
                container = next(
                    (
                        other.key
                        for other in tasks.values()
                        if other.key != key
                        and other.status == TaskStatus.COMPLETED
                        and other.commit_sha
                        and igit.is_ancestor(task.commit_sha, other.commit_sha)
                    ),
                    None,
                )
            if container:
                # A later task already merged this branch and resolved the conflict in
                # its own worktree; integrating that task brings both in, so the same
                # conflict is not resolved twice (possibly differently).
                igit.merge_abort()
                self.store.add_event(
                    run.id,
                    "integration_deferred",
                    f"{task.key} conflicts here; it is integrated through {container}, "
                    "which already contains it",
                    task_key=task.key,
                    data={"files": conflicts, "via": container},
                )
                continue
            if conflicts:
                integrated = {t.key: t.report for t in tasks.values() if t.integrated}
                self._run_cancellable(
                    run.id,
                    self._resolve_conflict(
                        run,
                        path,
                        task.branch,
                        conflicts,
                        {**integrated, task.key: task.report},
                        into="the integration branch",
                    ),
                )
            self.store.update_task(task.pk, integrated=True)
            self.store.add_event(run.id, "task_integrated", task.branch, task_key=task.key)
        for task in self.store.tasks(run.id, cycle=run.correction_cycle):
            if task.integrated or task.status != TaskStatus.COMPLETED or not task.commit_sha:
                continue
            if not igit.is_ancestor(task.commit_sha, "HEAD"):
                raise CrewError(f"task {task.key} was deferred but never reached integration")
            self.store.update_task(task.pk, integrated=True)
            self.store.add_event(
                run.id, "task_integrated", f"{task.branch} (via a later task)", task_key=task.key
            )
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

    def _sandbox_settings(self) -> SandboxSettings:
        execution = self.config.execution
        return SandboxSettings(
            mode=execution.sandbox,
            image=execution.sandbox_image,
            network=execution.sandbox_network,
            writable_paths=tuple(Path(p) for p in execution.sandbox_writable_paths),
        )

    async def _resolve_conflict(
        self,
        run: Run,
        path: Path,
        ref: str,
        files: list[str],
        reports: dict[str, Any],
        *,
        into: str,
    ) -> None:
        """Resolve an in-progress merge in a CREW worktree, or abort it and fail.

        An Implementer edits the conflicted files; CREW then checks that no
        conflict markers remain and creates the merge commit itself.
        """
        wt = self.p.git.at(path)
        summary = f"merging {ref} into {into} conflicted in: {', '.join(files)}"
        self.store.add_event(run.id, "merge_conflict", summary, data={"files": files})
        if self.config.execution.conflict_resolution == "fail":
            await self._git(wt.merge_abort)
            raise MergeConflictError(
                f"{summary}. Worktrees and branches were preserved under "
                f"{self.p.workspace.run_dir(run.id)}.",
                hint="Set execution.conflict_resolution=agent to let CREW resolve conflicts.",
            )
        self.notify(f"[warning] {summary}; asking an Implementer to resolve it")
        try:
            await self.runtime.invoke(
                Role.IMPLEMENTER,
                run_id=run.id,
                prompt=payloads.conflict_prompt(run, into, ref, files, reports),
                cwd=path,
                cycle=run.correction_cycle,
            )
        except CrewError as exc:
            await self._git(wt.merge_abort)
            raise MergeConflictError(f"{summary}; the resolver failed: {exc.message}") from exc
        stray = sorted(set(await self._git(wt.unstaged_changes)) - set(files))
        if stray:
            # Committing only the conflicted files would leave these changes behind,
            # uncommitted, in the worktree that later merges and tests run in.
            await self._git(wt.merge_abort)
            raise MergeConflictError(
                f"{summary}; the resolver also changed files outside the conflict: "
                f"{', '.join(stray)}. The worktree {path} was preserved for inspection."
            )
        remaining = await self._git(wt.files_with_conflict_markers, files)
        if remaining:
            await self._git(wt.merge_abort)
            raise MergeConflictError(
                f"{summary}; conflict markers remain in: {', '.join(remaining)}"
            )
        head = await self._git(wt.conclude_merge, files)
        self.store.add_event(
            run.id, "merge_conflict_resolved", f"{ref} into {into} ({(head or '')[:10]})"
        )
        self.notify(f"Conflict resolved: {ref} into {into}")

    async def _verify(self, run: Run, cwd: Any) -> None:
        execution = self.config.execution
        if not execution.verification_commands:
            return
        sandbox = self._sandbox_settings()
        # Fail closed: never fall back to running agent-written code unsandboxed.
        if problem := await asyncio.to_thread(check_sandbox, sandbox):
            raise PreconditionError(
                f"verification sandbox '{sandbox.mode}' is unavailable: {problem}",
                hint="Fix the sandbox (see `crew doctor`), then `crew retry <run-id>`.",
            )
        for command in execution.verification_commands:
            argv = parse_verification_command(command, resolve=resolves_on_host(sandbox.mode))
            result = await run_command(
                wrap_in_sandbox(argv, Path(cwd), sandbox),
                cwd=cwd,
                timeout_seconds=execution.command_timeout_seconds,
                output_limit_bytes=execution.command_output_limit_bytes,
            )
            self.store.record_command(
                run_id=run.id,
                purpose="verification",
                category=classify_command(argv),
                sandbox=sandbox.mode,
                argv_json=JsonMixin.dumps(redact_args(argv)),
                cwd=result.cwd,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_seconds=result.duration_seconds,
                stdout=result.stdout,
                stderr=result.stderr,
                cycle=run.correction_cycle,
            )
            status = "timed out" if result.timed_out else f"exit {result.exit_code}"
            shown = command if isinstance(command, str) else " ".join(command)
            where = "" if sandbox.mode == "none" else f" [sandbox: {sandbox.mode}]"
            self.notify(f"  {shown}{where} → {status}")

    def _integrated_diff(self, run: Run) -> str:
        """The integrated change as markdown, in the configured review_context mode."""
        git, branch = self.p.git, self.p.workspace.integration_branch(run.id)
        patch = git.diff(run.base_commit, branch)
        mode = self.config.execution.review_context
        if mode == "full":
            return payloads.diff_section(run.base_commit, patch, "", [], mode="full")
        stat = git.run("diff", "--stat", f"{run.base_commit}...{branch}")
        files = git.changed_files(run.base_commit, branch)
        return payloads.diff_section(run.base_commit, patch, stat, files, mode="summary")

    def _cycle_commands(self, run: Run) -> list:
        """Latest result per verification command in the current cycle.

        A stage re-run after a crash records commands again; only the newest
        result for each command is evidence.
        """
        latest: dict[str, Any] = {}
        for c in self.store.commands(run.id, purpose="verification"):
            if c.cycle == run.correction_cycle:
                latest[c.argv_json] = c
        return list(latest.values())

    def _snapshot(self, run: Run, purpose: str) -> Any:
        tip = self.p.git.rev_parse(self.p.workspace.integration_branch(run.id))
        return self.p.workspace.ensure_snapshot_worktree(run.id, purpose, tip)

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
            self._snapshot(run, "review"),
        )
        review = _expect(review, ReviewReport)
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
            key=f"{RESERVED_PREFIX}fix-{cycle}",
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

    def _evaluation_skip_reason(self, run: Run) -> str | None:
        limit = self.config.execution.skip_evaluation_max_lines
        if limit is None:
            return None
        commands = self._cycle_commands(run)
        if not commands or any(c.exit_code != 0 or c.timed_out for c in commands):
            return None
        if any(e.type == "correction_limit_reached" for e in self.store.events(run.id)):
            return None
        lines = self.p.git.changed_lines(
            run.base_commit, self.p.workspace.integration_branch(run.id)
        )
        if lines > limit:
            return None
        return (
            f"{lines} changed line(s) (limit {limit}), {len(commands)} verification "
            "command(s) passed, no blocking review findings"
        )

    def _evaluate(self, run: Run) -> None:
        if reason := self._evaluation_skip_reason(run):
            self.store.add_event(run.id, "evaluation_skipped", reason)
            renderer.write_artifact(
                self.store,
                self._run_dir(run),
                run.id,
                "external-evaluation.md",
                "# External evaluation\n\n_Skipped (`execution.skip_evaluation_max_lines`): "
                f"{reason}._\n",
            )
            self.notify(f"External evaluation skipped: {reason}")
            self._transition(run.id, RunState.AWAITING_FINAL_APPROVAL, "evaluation skipped")
            return
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
            self._snapshot(run, "evaluate"),
        )
        evaluation = _expect(evaluation, EvaluationReport)
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
            broker = self._start_permission_broker(run_id)
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=CANCEL_POLL_SECONDS)
                    if done:
                        return task.result()
                    if self.store.is_cancel_requested(run_id):
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                        raise RunCancelled
            finally:
                await _stop(broker)

        return asyncio.run(watch())

    # ---- permission prompts -------------------------------------------------

    def _permission_dir(self, run_id: str) -> Path:
        return self.p.paths.run_dir(run_id) / "permissions"

    def _start_permission_broker(self, run_id: str) -> asyncio.Task[None] | None:
        if self.config.approvals.permission_prompts != "ask":
            return None
        return asyncio.create_task(self._permission_broker(run_id))

    async def _permission_broker(self, run_id: str) -> None:
        """Answer agents' permission requests (written by the permission server)."""
        directory = self._permission_dir(run_id)
        while True:
            for request_file in sorted(directory.glob("*.request.json")):
                response_file = request_file.with_name(
                    request_file.name.replace(".request.json", ".response.json")
                )
                if response_file.exists():
                    continue
                try:
                    request = json.loads(request_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                decision = await self._ask_developer(request)
                if decision == "allow_run":
                    allowed_file = directory / "allowed.json"
                    allowed = _read_json_list(allowed_file)
                    allowed.append(request["key"])
                    _atomic_write(allowed_file, json.dumps(allowed))
                _atomic_write(response_file, json.dumps({"decision": decision}))
                approved = decision in ("allow", "allow_run")
                note = f"{request.get('role')} {request.get('key')}"
                self.store.add_approval(
                    run_id,
                    ApprovalKind.PERMISSION,
                    ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED,
                    f"{note} ({decision})",
                )
                self.store.add_event(run_id, "permission_" + decision, note)
            await asyncio.sleep(PERMISSION_POLL_SECONDS)

    async def _ask_developer(self, request: dict[str, Any]) -> str:
        if self.ask_permission is None:
            return "deny"
        ask = self.ask_permission
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        def worker() -> None:
            try:
                answer = ask(request)
            except Exception:  # a broken prompt must not crash the run
                answer = "deny"
            loop.call_soon_threadsafe(lambda: future.done() or future.set_result(answer))

        # A daemon thread, so an unanswered prompt never blocks CREW from exiting.
        threading.Thread(target=worker, daemon=True).start()
        answer = await future
        return answer if answer in ("allow", "allow_run", "deny") else "deny"

    def _transition(self, run_id: str, to: RunState, message: str = "", **fields: Any) -> Run:
        run = self.store.transition(run_id, to, message=message, **fields)
        self.notify(f"→ {to}" + (f": {message}" if message else ""))
        return run

    def _fail(self, run_id: str, reason: str, *, retry_from: RunState | None = None) -> None:
        state = retry_from or RunState(self.store.get_run(run_id).state)
        self._transition(run_id, RunState.FAILED, reason, failure_reason=reason, failed_state=state)
        self._cleanup_after_failure(run_id)

    def _cancel(self, run_id: str) -> None:
        self._transition(
            run_id,
            RunState.CANCELLED,
            "cancelled by developer",
            failure_reason="cancelled by developer",
        )
        self._cleanup_after_failure(run_id)

    def _cleanup_after_failure(self, run_id: str) -> None:
        # Even when not preserving, only worktrees without uncommitted work are removed.
        if not self.config.execution.preserve_worktrees_on_failure:
            self._cleanup_clean_worktrees(run_id)

    def _require_state(self, run_id: str, state: RunState) -> Run:
        run = self.store.get_run(run_id)
        if run.state != state:
            raise InvalidTransitionError(f"Run {run_id} is {run.state}, expected {state}.")
        return run

    def _lock_path(self, run_id: str):
        return self.p.paths.run_dir(run_id) / ".lock"

    @contextlib.contextmanager
    def _lock(self, run_id: str) -> Iterator[None]:
        self.store.get_run(run_id)  # NotFoundError before touching the filesystem
        token = CURRENT_RUN.set(run_id)
        try:
            with RunLock(self._lock_path(run_id)):
                yield
        finally:
            CURRENT_RUN.reset(token)

    def _run_dir(self, run: Run):
        return self.p.paths.run_dir(run.id)

    def _cleanup_clean_worktrees(self, run_id: str) -> tuple[list[str], list[str]]:
        """Remove CREW worktrees that hold no uncommitted work. Branches are kept.

        Returns (removed, kept) paths.
        """
        ws = self.p.workspace
        run_dir = ws.run_dir(run_id)
        removed: list[str] = []
        kept: list[str] = []
        if not run_dir.exists():
            return removed, kept
        for child in sorted(run_dir.iterdir()):
            if not child.is_dir() or not ws.owned(child):
                continue
            if ws.remove_worktree_if_clean(child):
                removed.append(str(child))
            else:
                kept.append(str(child))
                self.store.add_event(run_id, "worktree_preserved", str(child))
        with contextlib.suppress(OSError):
            run_dir.rmdir()  # only succeeds when nothing is left
        return removed, kept

    def cleanup(self, run_id: str, *, branches: bool = False) -> CleanupReport:
        """Remove a finished run's clean worktrees and, optionally, its branches.

        Never touches worktrees with uncommitted work. Branch rules:
        * task branches are deleted once their work is in the integration branch
          (or the run was rejected/cancelled, i.e. the work was discarded);
        * the integration branch of a COMPLETED run is deleted only after you
          merged it into the base branch;
        * FAILED runs keep their branches so `crew retry` keeps working.
        """
        with self._lock(run_id):
            run = self.store.get_run(run_id)
            state = RunState(run.state)
            if not is_terminal(state):
                raise InvalidTransitionError(
                    f"Run {run_id} is {run.state}; only finished runs can be cleaned up.",
                    hint="Use `crew stop` first if you want to abandon it.",
                )
            removed, kept = self._cleanup_clean_worktrees(run_id)
            report = CleanupReport(removed_worktrees=removed, kept_worktrees=kept)
            if branches and state is not RunState.FAILED:
                self._cleanup_branches(run, report)
            elif branches:
                report.kept_branches.append("(all) — FAILED runs keep branches for `crew retry`")
            self.store.add_event(
                run_id,
                "cleanup",
                f"removed {len(removed)} worktree(s), deleted "
                f"{len(report.deleted_branches)} branch(es)",
            )
            return report

    def _cleanup_branches(self, run: Run, report: CleanupReport) -> None:
        git, ws = self.p.git, self.p.workspace
        prefix = f"{ws.prefix}/{run.id}/"
        integration = ws.integration_branch(run.id)
        completed = run.state == RunState.COMPLETED
        for branch in git.branches(f"{prefix}*"):
            if branch == integration:
                merged = (
                    bool(run.base_branch)
                    and git.branch_exists(run.base_branch)
                    and (git.is_ancestor(integration, run.base_branch))
                )
                deletable = merged or not completed
                reason = "not merged into " + (run.base_branch or "the base branch") + " yet"
            else:
                deletable = not completed or git.is_ancestor(branch, integration)
                reason = "not contained in the integration branch"
            if not deletable:
                report.kept_branches.append(f"{branch} ({reason})")
                continue
            try:
                git.delete_owned_branch(branch, prefix)
                report.deleted_branches.append(branch)
            except CrewError as exc:  # e.g. still checked out in a preserved worktree
                report.kept_branches.append(f"{branch} ({exc.message})")

    def _write_implementation_artifact(self, run: Run) -> None:
        lines = ["# Implementation", ""]
        for t in self.store.tasks(run.id):
            report = t.report or {}
            lines += [
                f"## `{t.key}` — {t.title} ({t.status})",
                "",
                f"- Branch: `{t.branch}`  Commit: `{(t.commit_sha or '-')[:12]}`",
                f"- Changed files: {', '.join(t.changed_files) or '-'}",
                *(
                    [f"- Outside the plan: {', '.join(extra)}"]
                    if t.expected_files
                    and (extra := sorted(set(t.changed_files) - set(t.expected_files)))
                    else []
                ),
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
