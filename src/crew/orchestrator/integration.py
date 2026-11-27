"""INTEGRATING: merge task branches, resolve conflicts, run verification commands.

A mixin of :class:`crew.orchestrator.engine.Orchestrator`; split out of engine.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from crew.agents import payloads
from crew.agents.runtime import AgentRuntime
from crew.config.schema import CrewConfig
from crew.domain.enums import (
    Role,
    RunState,
    TaskStatus,
)
from crew.domain.errors import (
    CrewError,
    MergeConflictError,
    PreconditionError,
)
from crew.domain.task_graph import topological_order
from crew.orchestrator.support import (
    Notify,
    Project,
)
from crew.persistence.models import JsonMixin, Run
from crew.persistence.store import Store
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


class IntegrationStage:
    # Provided by Orchestrator (engine.py).
    p: Project
    store: Store
    config: CrewConfig
    runtime: AgentRuntime
    notify: Notify

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
