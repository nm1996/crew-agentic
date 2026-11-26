"""Thin, explicit wrapper over the ``git`` executable.

Git is the source of truth; this module only builds argument lists, runs them
without a shell, and turns failures into :class:`GitError`. Destructive commands
against the developer's active worktree are refused by policy before execution.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crew.domain.enums import CommandCategory
from crew.domain.errors import GitError, PolicyViolationError
from crew.security.execution_policy import classify_command
from crew.security.redaction import redact

log = logging.getLogger(__name__)

# Called after every git invocation: (argv, cwd, exit_code, stderr).
GitRecorder = Callable[[list[str], Path, int, str], None]


@dataclass(frozen=True)
class GitStatus:
    branch: str | None  # None when HEAD is detached
    head: str | None  # None in a repository with no commits
    dirty_paths: list[str]

    @property
    def is_clean(self) -> bool:
        return not self.dirty_paths


class Git:
    """Run git commands in ``cwd``.

    ``owned_roots`` lists CREW-owned directories (worktrees) where history-
    rewriting operations such as ``merge --abort`` are allowed. Outside of them,
    any command classified DESTRUCTIVE or NETWORK is refused.
    """

    def __init__(
        self,
        cwd: Path,
        *,
        owned_roots: tuple[Path, ...] = (),
        recorder: GitRecorder | None = None,
    ) -> None:
        self.cwd = cwd
        self.owned_roots = tuple(p.resolve() for p in owned_roots)
        self.recorder = recorder

    def at(self, cwd: Path) -> Git:
        return Git(cwd, owned_roots=self.owned_roots, recorder=self.recorder)

    def _in_owned_root(self) -> bool:
        cwd = self.cwd.resolve()
        return any(cwd == root or root in cwd.parents for root in self.owned_roots)

    def run(self, *args: str, check: bool = True, allow_destructive: bool = False) -> str:
        """Run git and return stdout; raise :class:`GitError` on failure if ``check``."""
        return self._exec(args, check=check, allow_destructive=allow_destructive).stdout

    def ok(self, *args: str) -> bool:
        """Run git and report whether it exited 0 (for predicates like ``--is-ancestor``)."""
        return self._exec(args, check=False).returncode == 0

    def _exec(
        self, args: tuple[str, ...], *, check: bool, allow_destructive: bool = False
    ) -> subprocess.CompletedProcess[str]:
        argv = ["git", *args]
        category = classify_command(argv)
        if category is CommandCategory.NETWORK:
            raise PolicyViolationError(f"refusing network git command: {' '.join(argv)}")
        if category is CommandCategory.DESTRUCTIVE and not (
            allow_destructive and self._in_owned_root()
        ):
            raise PolicyViolationError(
                f"refusing destructive git command outside a CREW-owned worktree: {' '.join(argv)}"
            )
        proc = subprocess.run(
            argv,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
            env=_git_env(),
        )
        log.debug("git %s (cwd=%s) -> %s", " ".join(args), self.cwd, proc.returncode)
        if self.recorder:
            self.recorder(argv, self.cwd, proc.returncode, redact(proc.stderr[-2000:]))
        if check and proc.returncode != 0:
            raise GitError(
                f"`git {' '.join(args)}` failed ({proc.returncode}): {redact(proc.stderr.strip())}"
            )
        return proc

    # ---- inspection -------------------------------------------------------

    def is_repository(self) -> bool:
        return self.run("rev-parse", "--is-inside-work-tree", check=False).strip() == "true"

    def toplevel(self) -> Path:
        return Path(self.run("rev-parse", "--show-toplevel").strip())

    def git_dir(self) -> Path:
        return Path(self.run("rev-parse", "--absolute-git-dir").strip())

    def head(self) -> str | None:
        out = self.run("rev-parse", "--verify", "--quiet", "HEAD", check=False).strip()
        return out or None

    def current_branch(self) -> str | None:
        out = self.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
        return out or None

    def status(self) -> GitStatus:
        out = self.run("status", "--porcelain=v1", "--untracked-files=all")
        dirty = [line[3:] for line in out.splitlines() if line.strip()]
        return GitStatus(branch=self.current_branch(), head=self.head(), dirty_paths=dirty)

    def rev_parse(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def branch_exists(self, name: str) -> bool:
        return (
            self.run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).strip()
            != ""
        )

    def ls_files(self) -> list[str]:
        return [line for line in self.run("ls-files").splitlines() if line]

    def changed_files(self, base: str, head: str = "HEAD") -> list[str]:
        out = self.run("diff", "--name-only", f"{base}...{head}")
        return [line for line in out.splitlines() if line]

    def diff(self, base: str, head: str = "HEAD") -> str:
        return self.run("diff", "--stat", "--patch", f"{base}...{head}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    # ---- worktrees & commits (CREW-owned paths only) ---------------------

    def worktree_add(self, path: Path, branch: str, start_point: str) -> None:
        self.run("worktree", "add", "-b", branch, str(path), start_point)

    def worktree_add_detached(self, path: Path, commit: str) -> None:
        self.run("worktree", "add", "--detach", str(path), commit)

    def worktree_list(self) -> list[Path]:
        out = self.run("worktree", "list", "--porcelain")
        return [
            Path(line[len("worktree ") :])
            for line in out.splitlines()
            if line.startswith("worktree ")
        ]

    def worktree_remove(self, path: Path) -> None:
        """Remove a *clean* CREW worktree. Git itself refuses if it has changes."""
        self.run("worktree", "remove", str(path))

    def commit_all(self, message: str) -> str | None:
        """Stage everything in this (CREW-owned) worktree and commit. Returns SHA or None."""
        self.run("add", "--all")
        if not self.run("status", "--porcelain").strip():
            return None
        self.run(
            "-c",
            "user.name=CREW",
            "-c",
            "user.email=crew@localhost",
            "commit",
            "--quiet",
            "--no-verify",
            "-m",
            message,
        )
        return self.head()

    def merge_no_ff(self, ref: str, message: str) -> bool:
        """Merge ``ref`` into the current branch. Returns False (and aborts) on conflict."""
        proc = self._exec(
            (
                "-c",
                "user.name=CREW",
                "-c",
                "user.email=crew@localhost",
                "merge",
                "--no-ff",
                "--no-edit",
                "-m",
                message,
                ref,
            ),
            check=False,
        )
        if proc.returncode == 0:
            return True
        if self._has_unmerged():
            return False
        raise GitError(
            f"`git merge {ref}` failed: {redact(proc.stderr.strip() or proc.stdout.strip())}"
        )

    def conflicted_files(self) -> list[str]:
        out = self.run("diff", "--name-only", "--diff-filter=U")
        return [line for line in out.splitlines() if line]

    def _has_unmerged(self) -> bool:
        return bool(self.conflicted_files()) or (self.git_dir() / "MERGE_HEAD").exists()

    def merge_abort(self) -> None:
        self.run("merge", "--abort", allow_destructive=True)


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env
