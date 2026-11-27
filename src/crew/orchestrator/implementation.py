"""IMPLEMENTING: dependency-aware scheduler and one Implementer per task worktree.

A mixin of :class:`crew.orchestrator.engine.Orchestrator`; split out of engine.py.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from crew.agents import payloads
from crew.agents.contracts import ImplementationReport
from crew.agents.runtime import AgentRuntime
from crew.config.schema import CrewConfig
from crew.domain.enums import (
    Role,
    RunState,
    TaskStatus,
)
from crew.domain.errors import (
    BudgetExceededError,
    CrewError,
    MergeConflictError,
    PreconditionError,
    ProviderError,
)
from crew.domain.task_graph import (
    is_blocked,
    is_ready,
)
from crew.orchestrator.support import (
    CANCEL_POLL_SECONDS,
    Notify,
    Project,
    RunCancelled,
    expect_type,
    stop_task,
)
from crew.persistence.models import JsonMixin, Run, Task, utcnow
from crew.persistence.store import Store
from crew.reporting import renderer

log = logging.getLogger(__name__)


class ImplementationStage:
    # Provided by Orchestrator (engine.py).
    p: Project
    store: Store
    config: CrewConfig
    runtime: AgentRuntime
    notify: Notify

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
            await stop_task(broker)

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
            report = expect_type(report, ImplementationReport)
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
