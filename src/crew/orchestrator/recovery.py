"""Conservative reconciliation before resuming a run (docs/WORKFLOW.md §6).

Rules:
* Executions left RUNNING are marked INTERRUPTED; a still-alive orphaned agent
  process tree is terminated, after verifying (creation time + command line)
  that the PID was not reused.
* Tasks left RUNNING go back to PENDING. Their worktree is *preserved*; the next
  attempt gets a fresh worktree and branch. Partial changes are never assumed to
  be a completed task.
* Completed tasks must still have their commit; otherwise CREW stops and asks
  for developer intervention instead of guessing.
* A half-finished merge in the CREW-owned integration worktree is aborted, which
  restores it to the last fully integrated commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from crew.domain.enums import ExecutionStatus, RunState, TaskStatus
from crew.domain.errors import PreconditionError
from crew.persistence.models import Run
from crew.system.processes import is_same_process, kill_tree

if TYPE_CHECKING:
    from crew.orchestrator.engine import Project

_PAST_IMPLEMENTATION = {
    RunState.IMPLEMENTING,
    RunState.INTEGRATING,
    RunState.REVIEWING,
    RunState.CHANGES_REQUESTED,
    RunState.EXTERNAL_EVALUATION,
    RunState.AWAITING_FINAL_APPROVAL,
}


def _terminate_orphan(pid: int, started_at: float | None, executable_hint: str) -> bool:
    if not is_same_process(pid, started_at, executable_hint):
        return False  # gone, or PID reused by an unrelated process
    return kill_tree(pid)


def reconcile_processes(project: Project, run: Run) -> list[str]:
    """Mark executions left RUNNING as INTERRUPTED and stop orphaned agent trees."""
    store = project.store
    notes: list[str] = []
    exe = Path(project.config.provider.claude_code.executable).stem
    for execution in store.executions(run.id, status=ExecutionStatus.RUNNING):
        if execution.pid and _terminate_orphan(execution.pid, execution.process_started_at, exe):
            notes.append(f"terminated orphaned agent process {execution.pid}")
        store.finish_execution(
            execution.id, status=ExecutionStatus.INTERRUPTED, error="CREW process interrupted"
        )
        notes.append(f"execution {execution.id} ({execution.role}) marked INTERRUPTED")
    return notes


def reconcile(project: Project, run: Run) -> list[str]:
    store, git, ws = project.store, project.git, project.workspace
    notes = reconcile_processes(project, run)

    if not git.ok("cat-file", "-e", f"{run.base_commit}^{{commit}}"):
        raise PreconditionError(
            f"base commit {run.base_commit} no longer exists; cannot resume safely",
        )

    for task in store.tasks(run.id):
        if task.status == TaskStatus.RUNNING:
            store.set_task_status(
                task,
                TaskStatus.PENDING,
                "reset after interruption",
                failure_reason=f"interrupted; worktree preserved at {task.worktree_path}",
            )
            notes.append(f"task {task.key} reset to PENDING (old worktree preserved)")
        elif (
            task.status == TaskStatus.COMPLETED
            and task.commit_sha
            and not git.ok("cat-file", "-e", f"{task.commit_sha}^{{commit}}")
        ):
            raise PreconditionError(
                f"task {task.key} is COMPLETED but commit {task.commit_sha} is missing",
                hint="Inspect the CREW branches; start a new run if the history was rewritten.",
            )

    state = RunState(run.state)
    if state in _PAST_IMPLEMENTATION:
        branch = ws.integration_branch(run.id)
        path = ws.integration_path(run.id)
        if not git.branch_exists(branch) and state is not RunState.IMPLEMENTING:
            raise PreconditionError(f"integration branch {branch} is missing; cannot resume")
        if path.exists():
            igit = git.at(path)
            if (igit.git_dir() / "MERGE_HEAD").exists():
                igit.merge_abort()
                notes.append("aborted an interrupted merge in the integration worktree")
        else:
            ws.ensure_integration_worktree(run.id, run.base_commit)
            notes.append("re-created the integration worktree")

    if run.cancel_requested:
        store.update_run(run.id, cancel_requested=False)
        notes.append("cleared a stale cancellation request")
    store.add_event(run.id, "run_resumed", "; ".join(notes) or "nothing to reconcile")
    return notes
