"""Thin, explicit wrapper over the ``git`` executable.

Git is the source of truth; this module only builds argument lists, runs them
without a shell, and turns failures into :class:`GitError`.

Policy enforced here, before any process starts:

* NETWORK and DESTRUCTIVE commands are always refused.
* Commands that write (commit, merge, add, ...) are allowed only inside
  CREW-owned worktrees. From the developer's repository root CREW may only read
  and manage worktrees (``git worktree add/remove/list``).
* Every invocation disables repository hooks, fsmonitor and auto-gc, so neither
  the developer's nor an agent's hooks run as a side effect of CREW's git calls.
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
from crew.security.execution_policy import classify_command, git_subcommand
from crew.security.redaction import redact
from crew.security.secret_files import is_commit_blocked

log = logging.getLogger(__name__)

# Called after every git invocation: (argv, cwd, exit_code, stderr).
GitRecorder = Callable[[list[str], Path, int, str], None]

# Write commands that may run from the developer's repository root.
_ROOT_WRITE_ALLOWED = frozenset({"worktree"})
_CONFLICT_MARKERS = ("<<<<<<< ", ">>>>>>> ", "||||||| ")


def _count_separators(lines: list[str]) -> int:
    return sum(1 for line in lines if line.rstrip() == "=======")


# Files larger than this that an agent newly creates are not committed.
MAX_NEW_FILE_BYTES = 10 * 1024 * 1024

CREW_IDENTITY = ("-c", "user.name=CREW", "-c", "user.email=crew@localhost")


def _isolation_options() -> tuple[str, ...]:
    opts = [
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "gc.auto=0",
        "-c",
        "core.quotePath=false",
    ]
    if os.name == "nt":
        opts += ["-c", "core.longpaths=true"]
    return tuple(opts)


def _split_z(out: str) -> list[str]:
    return [item for item in out.split("\0") if item]


@dataclass(frozen=True)
class GitStatus:
    branch: str | None  # None when HEAD is detached
    head: str | None  # None in a repository with no commits
    dirty_paths: list[str]

    @property
    def is_clean(self) -> bool:
        return not self.dirty_paths


@dataclass(frozen=True)
class CommitResult:
    sha: str | None  # None when there was nothing to commit
    excluded: list[str]  # new files deliberately left out (secrets, oversized)


class Git:
    """Run git commands in ``cwd``; ``owned_roots`` are CREW-owned worktree directories."""

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

    def in_owned_root(self) -> bool:
        cwd = self.cwd.resolve()
        return any(cwd == root or root in cwd.parents for root in self.owned_roots)

    def run(self, *args: str, check: bool = True) -> str:
        """Run git and return stdout; raise :class:`GitError` on failure if ``check``."""
        return self._exec(args, check=check).stdout

    def ok(self, *args: str) -> bool:
        """Run git and report whether it exited 0 (for predicates like ``--is-ancestor``)."""
        return self._exec(args, check=False).returncode == 0

    def _check_policy(self, argv: list[str]) -> None:
        category = classify_command(argv)
        if category is CommandCategory.NETWORK:
            raise PolicyViolationError(f"refusing network git command: {' '.join(argv)}")
        if category is CommandCategory.DESTRUCTIVE:
            raise PolicyViolationError(f"refusing destructive git command: {' '.join(argv)}")
        if category is CommandCategory.WORKTREE_WRITE and not self.in_owned_root():
            sub, _ = git_subcommand(argv[1:])
            if sub not in _ROOT_WRITE_ALLOWED:
                raise PolicyViolationError(
                    f"refusing to modify the developer's worktree: {' '.join(argv)}"
                )

    def _exec(self, args: tuple[str, ...], *, check: bool) -> subprocess.CompletedProcess[str]:
        argv = ["git", *args]
        self._check_policy(argv)
        proc = subprocess.run(
            ["git", *_isolation_options(), *args],
            cwd=self.cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",  # never crash on non-UTF-8 file names or content
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

    def git_path(self, name: str) -> Path:
        """Resolve a path inside the repository's git directory (``git rev-parse --git-path``).

        Handles linked worktrees, where e.g. ``info/exclude`` lives in the common dir.
        """
        path = Path(self.run("rev-parse", "--git-path", name).strip())
        return path if path.is_absolute() else self.cwd / path

    def head(self) -> str | None:
        out = self.run("rev-parse", "--verify", "--quiet", "HEAD", check=False).strip()
        return out or None

    def current_branch(self) -> str | None:
        out = self.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
        return out or None

    def status(self) -> GitStatus:
        entries = _split_z(self.run("status", "--porcelain=v1", "-z", "--untracked-files=all"))
        dirty: list[str] = []
        skip_next = False
        for entry in entries:
            if skip_next:  # original path of a rename/copy
                skip_next = False
                continue
            code, path = entry[:2], entry[3:]
            dirty.append(path)
            skip_next = code[0] in "RC" or code[1] in "RC"
        return GitStatus(branch=self.current_branch(), head=self.head(), dirty_paths=dirty)

    def rev_parse(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def branch_exists(self, name: str) -> bool:
        return (
            self.run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).strip()
            != ""
        )

    def branches(self, pattern: str) -> list[str]:
        out = self.run("for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}")
        return [line for line in out.splitlines() if line]

    def ls_files(self) -> list[str]:
        return _split_z(self.run("ls-files", "-z"))

    def changed_files(self, base: str, head: str = "HEAD") -> list[str]:
        return _split_z(self.run("diff", "--name-only", "-z", f"{base}...{head}"))

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

    def commit_all(self, message: str) -> CommitResult:
        """Stage everything in this CREW-owned worktree and commit it.

        Newly created secret-looking or oversized files are unstaged and reported
        instead of committed; they stay in the worktree for inspection.
        """
        self.run("add", "--all")
        excluded: list[str] = []
        for path in _split_z(self.run("diff", "--cached", "--name-only", "-z", "--diff-filter=A")):
            full = self.cwd / path
            too_big = full.is_file() and full.stat().st_size > MAX_NEW_FILE_BYTES
            if is_commit_blocked(path) or too_big:
                self.run("rm", "--cached", "--quiet", "--", path)
                excluded.append(path)
        if not self.run("diff", "--cached", "--name-only").strip():
            return CommitResult(sha=None, excluded=excluded)
        self.run(*CREW_IDENTITY, "commit", "--quiet", "--no-verify", "-m", message)
        return CommitResult(sha=self.head(), excluded=excluded)

    def merge_no_ff(self, ref: str, message: str) -> bool:
        """Merge ``ref`` into the current branch. Returns False on conflict (caller aborts)."""
        proc = self._exec(
            (*CREW_IDENTITY, "merge", "--no-ff", "--no-edit", "-m", message, ref), check=False
        )
        if proc.returncode == 0:
            return True
        if self._has_unmerged():
            return False
        raise GitError(
            f"`git merge {ref}` failed: {redact(proc.stderr.strip() or proc.stdout.strip())}"
        )

    def conflicted_files(self) -> list[str]:
        return _split_z(self.run("diff", "--name-only", "-z", "--diff-filter=U"))

    def _has_unmerged(self) -> bool:
        return bool(self.conflicted_files()) or (self.git_dir() / "MERGE_HEAD").exists()

    def merge_in_progress(self) -> bool:
        return (self.git_dir() / "MERGE_HEAD").exists()

    def merge_abort(self) -> None:
        self.run("merge", "--abort")

    def files_with_conflict_markers(self, files: list[str]) -> list[str]:
        """Files that still contain merge conflict markers.

        Start, end and diff3 base markers always count. A bare ``=======`` line is
        also a Markdown/reST heading underline, so it counts only when the file has
        more of them than either side of the merge had.
        """
        remaining = []
        for name in files:
            path = self.cwd / name
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if any(line.startswith(_CONFLICT_MARKERS) for line in lines):
                remaining.append(name)
                continue
            separators = _count_separators(lines)
            if separators and separators > max(
                _count_separators(self.run("show", f":{stage}:{name}", check=False).splitlines())
                for stage in (2, 3)
            ):
                remaining.append(name)
        return remaining

    def unstaged_changes(self) -> list[str]:
        """Tracked files changed since the index, plus untracked (non-ignored) files."""
        changed = _split_z(self.run("diff", "--name-only", "-z"))
        untracked = _split_z(self.run("ls-files", "--others", "--exclude-standard", "-z"))
        return sorted(set(changed) | set(untracked))

    def conclude_merge(self, files: list[str]) -> str | None:
        """Stage resolved files and create the merge commit. Returns the new HEAD."""
        self.run("add", "--", *files)
        self.run(*CREW_IDENTITY, "commit", "--quiet", "--no-verify", "--no-edit")
        return self.head()

    def push_owned_branch(self, remote: str, name: str, owned_prefix: str) -> None:
        """Push one CREW branch to ``remote``. The only network operation CREW performs.

        Used solely by the explicit ``crew publish`` command. Only names under
        ``owned_prefix`` are accepted and force-pushing is impossible.
        """
        if not name.startswith(owned_prefix) or ".." in name or name.startswith("-"):
            raise PolicyViolationError(f"refusing to push non-CREW branch {name!r}")
        if remote.startswith("-"):
            raise PolicyViolationError(f"invalid remote {remote!r}")
        argv = ["git", "push", "--no-verify", remote, f"refs/heads/{name}:refs/heads/{name}"]
        proc = subprocess.run(
            ["git", *_isolation_options(), *argv[1:]],
            cwd=self.cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=_git_env(),
        )
        if self.recorder:
            self.recorder(argv, self.cwd, proc.returncode, redact(proc.stderr[-2000:]))
        if proc.returncode != 0:
            raise GitError(f"could not push {name} to {remote}: {redact(proc.stderr.strip())}")

    def delete_owned_branch(self, name: str, owned_prefix: str) -> None:
        """Delete a CREW branch. Only names under ``owned_prefix`` are accepted.

        ``branch -D`` is otherwise classified destructive; this is the single,
        prefix-checked exception used by ``crew cleanup``.
        """
        if not name.startswith(owned_prefix) or ".." in name:
            raise PolicyViolationError(f"refusing to delete non-CREW branch {name!r}")
        proc = subprocess.run(
            ["git", *_isolation_options(), "branch", "-D", "--", name],
            cwd=self.cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=_git_env(),
        )
        if self.recorder:
            self.recorder(["git", "branch", "-D", name], self.cwd, proc.returncode, proc.stderr)
        if proc.returncode != 0:
            raise GitError(f"could not delete branch {name}: {redact(proc.stderr.strip())}")


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env
