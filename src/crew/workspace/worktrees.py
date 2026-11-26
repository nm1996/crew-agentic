"""CREW-owned worktree layout and lifecycle.

Layout for a run ``<run>``::

    .crew/worktrees/<run>/director        detached at the run's base commit (read-only agents)
    .crew/worktrees/<run>/integration     branch <prefix>/<run>/integration
    .crew/worktrees/<run>/<task>-a<n>     branch <prefix>/<run>/<task>-a<n>

Integration strategy (decided in docs/DECISIONS.md): every task branch is merged
into the integration branch with ``git merge --no-ff``, which keeps one merge
commit per task (provenance) and lets Git detect conflicts. A task whose
dependencies are complete starts from the integration tip and merges its
dependencies' branches first, so it builds on their work.
"""

from __future__ import annotations

from pathlib import Path

from crew.domain.errors import GitError, PreconditionError
from crew.workspace.git import Git


class WorkspaceManager:
    def __init__(self, git: Git, project_root: Path, worktree_dir: str, branch_prefix: str) -> None:
        self.git = git
        self.project_root = project_root
        self.base_dir = project_root / worktree_dir
        self.prefix = branch_prefix

    # ---- naming -----------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.base_dir / run_id

    def director_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "director"

    def integration_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "integration"

    def integration_branch(self, run_id: str) -> str:
        return f"{self.prefix}/{run_id}/integration"

    def task_slot(self, task_id: str, attempt: int) -> str:
        return f"{task_id}-a{attempt}"

    def task_path(self, run_id: str, task_id: str, attempt: int) -> Path:
        return self.run_dir(run_id) / self.task_slot(task_id, attempt)

    def task_branch(self, run_id: str, task_id: str, attempt: int) -> str:
        return f"{self.prefix}/{run_id}/{self.task_slot(task_id, attempt)}"

    # ---- preconditions ----------------------------------------------------

    def require_clean_user_worktree(self) -> None:
        status = self.git.status()
        if not status.is_clean:
            shown = ", ".join(status.dirty_paths[:8])
            more = "" if len(status.dirty_paths) <= 8 else f" (+{len(status.dirty_paths) - 8} more)"
            raise PreconditionError(
                f"Your working tree has uncommitted changes: {shown}{more}",
                hint="Commit them, or move them aside yourself (CREW never stashes, resets or "
                "cleans your work), then retry.",
            )

    # ---- lifecycle --------------------------------------------------------

    def ensure_director_worktree(self, run_id: str, base_commit: str) -> Path:
        path = self.director_path(run_id)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git.worktree_add_detached(path, base_commit)
        return path

    def ensure_integration_worktree(self, run_id: str, base_commit: str) -> Path:
        path = self.integration_path(run_id)
        branch = self.integration_branch(run_id)
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.git.branch_exists(branch):
            self.git.run("worktree", "add", str(path), branch)
        else:
            self.git.worktree_add(path, branch, base_commit)
        return path

    def create_task_worktree(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        start_point: str,
        dependency_branches: list[str],
    ) -> tuple[Path, str]:
        """Create a fresh worktree for one task attempt and merge its dependencies in.

        Raises :class:`PreconditionError` when the slot already exists (a previous
        attempt is preserved, never reused) or dependency branches conflict.
        """
        path = self.task_path(run_id, task_id, attempt)
        branch = self.task_branch(run_id, task_id, attempt)
        if path.exists() or self.git.branch_exists(branch):
            raise PreconditionError(f"worktree slot {path} or branch {branch} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.worktree_add(path, branch, start_point)
        wt = self.git.at(path)
        for dep_branch in dependency_branches:
            if wt.is_ancestor(dep_branch, "HEAD"):
                continue
            if not wt.merge_no_ff(dep_branch, f"crew: bring in dependency {dep_branch}"):
                files = wt.conflicted_files()
                wt.merge_abort()
                raise PreconditionError(
                    f"dependency branches for task {task_id} conflict in: {', '.join(files)}",
                    hint=f"Inspect {path} and the dependency branches.",
                )
        return path, branch

    def remove_worktree_if_clean(self, path: Path) -> bool:
        """Remove a CREW worktree only if Git agrees it holds no uncommitted changes."""
        if not path.exists():
            return False
        try:
            self.git.worktree_remove(path)
        except GitError:
            return False
        return True

    def owned(self, path: Path) -> bool:
        path = path.resolve()
        base = self.base_dir.resolve()
        return path == base or base in path.parents
