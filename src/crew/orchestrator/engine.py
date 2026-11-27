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
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

from crew.agents import payloads
from crew.agents.contracts import DirectorPlan
from crew.agents.runtime import AgentRuntime
from crew.context.collector import collect_context
from crew.domain.enums import (
    GATE_STATES,
    ApprovalDecision,
    ApprovalKind,
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
from crew.domain.task_graph import (
    Node,
    order_shared_files,
    validate_graph,
)
from crew.integrations.base import (
    CodeHost,
    DocPage,
    DocsTarget,
    InlineComment,
    IssueTracker,
    PublishResult,
)
from crew.orchestrator import recovery
from crew.orchestrator.implementation import ImplementationStage
from crew.orchestrator.integration import IntegrationStage
from crew.orchestrator.lock import RunLock
from crew.orchestrator.permissions import PermissionBroker
from crew.orchestrator.review import ReviewStage
from crew.orchestrator.state_machine import is_terminal, retry_target
from crew.orchestrator.support import (
    CANCEL_POLL_SECONDS,
    CURRENT_RUN,
    LINKED_DOCS_MAX_CHARS,
    AskPermission,
    CleanupReport,
    Notify,
    Project,
    RunCancelled,
    T,
    expect_type,
    stop_task,
)
from crew.persistence.models import JsonMixin, Run, Task
from crew.providers.base import AgentProvider
from crew.reporting import renderer

log = logging.getLogger(__name__)

__all__ = ["CURRENT_RUN", "AskPermission", "CleanupReport", "Notify", "Orchestrator", "Project"]


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


class Orchestrator(ImplementationStage, IntegrationStage, ReviewStage, PermissionBroker):
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
                inline = [
                    InlineComment(f["file"], f["line_start"], renderer.render_inline_finding(f))
                    for f in review.get("findings", [])
                    if f.get("file") and f.get("line_start")
                ]
                placed = code_host.post_review(
                    number, renderer.render_review_comment(review), inline
                )
                result.review_posted = True
                result.inline_comments = placed
                self.store.add_event(
                    run_id,
                    "review_posted",
                    f"{run.pull_request_url or ''} ({placed}/{len(inline)} inline comment(s))",
                )

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
            plan = expect_type(plan, DirectorPlan)
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
                await stop_task(broker)

        return asyncio.run(watch())

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
