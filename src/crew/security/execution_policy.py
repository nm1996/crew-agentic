"""Command classification and the guarded subprocess runner.

CREW itself runs only two kinds of external commands: Git plumbing (through
:mod:`crew.workspace.git`) and developer-configured verification commands. Both
go through :func:`run_command`, which never uses a shell, enforces a timeout and
an output-size limit, and redacts output before it is returned for persistence.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from crew.domain.enums import CommandCategory
from crew.domain.errors import PolicyViolationError
from crew.security.redaction import redact, redact_args

_DESTRUCTIVE_TOKENS = {
    "rm",
    "rmdir",
    "dd",
    "mkfs",
    "shred",
    "sudo",
    "su",
    "doas",
    "chown",
    "chmod",
    "kill",
    "pkill",
    "killall",
    "shutdown",
    "reboot",
}
_GIT_DESTRUCTIVE_ALWAYS = {"clean", "stash", "restore", "filter-branch", "gc", "prune"}
_GIT_DESTRUCTIVE_FLAGS = {
    "reset": {"--hard", "--merge", "--keep"},
    "branch": {"-D", "--delete", "-d", "--force", "-f"},
    "checkout": {"--", "-f", "--force"},
    "worktree": {"prune", "--force", "-f"},
}
_GIT_NETWORK = {"push", "fetch", "pull", "clone", "remote", "ls-remote"}
_GIT_READ_ONLY = {
    "status",
    "log",
    "diff",
    "show",
    "rev-parse",
    "ls-files",
    "merge-base",
    "cat-file",
}


def _git_subcommand(args: list[str]) -> tuple[str, set[str]]:
    """Return git's subcommand (skipping ``-C dir`` / ``-c k=v`` globals) and its other args."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args):
        return "", set()
    return args[i], set(args[i + 1 :])


_NETWORK_TOKENS = {"curl", "wget", "ssh", "scp", "rsync", "nc", "ncat", "telnet", "ftp"}
_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh"}
_READ_ONLY = {"ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "echo", "true"}


def classify_command(argv: list[str]) -> CommandCategory:
    """Classify a command per SECURITY.md §3. Unknown commands are VERIFICATION."""
    if not argv:
        raise PolicyViolationError("empty command")
    exe = Path(argv[0]).name
    if exe in _DESTRUCTIVE_TOKENS or exe in _SHELLS:
        return CommandCategory.DESTRUCTIVE
    if exe in _NETWORK_TOKENS:
        return CommandCategory.NETWORK
    if exe == "git":
        sub, flags = _git_subcommand(argv[1:])
        if sub in _GIT_DESTRUCTIVE_ALWAYS:
            return CommandCategory.DESTRUCTIVE
        if flags & _GIT_DESTRUCTIVE_FLAGS.get(sub, set()):
            return CommandCategory.DESTRUCTIVE
        if sub in _GIT_NETWORK:
            return CommandCategory.NETWORK
        if sub in _GIT_READ_ONLY:
            return CommandCategory.READ_ONLY
        return CommandCategory.WORKTREE_WRITE
    if exe in _READ_ONLY:
        return CommandCategory.READ_ONLY
    return CommandCategory.VERIFICATION


def parse_verification_command(command: str) -> list[str]:
    """Parse a configured verification command and refuse unsafe categories."""
    argv = shlex.split(command)
    category = classify_command(argv)
    if category in (CommandCategory.DESTRUCTIVE, CommandCategory.NETWORK):
        raise PolicyViolationError(
            f"verification command {command!r} is classified as {category.value}; "
            "only read-only and verification commands may run unattended",
        )
    if any(tok in {"|", "&&", "||", ";", ">", ">>", "<"} for tok in argv):
        raise PolicyViolationError(
            f"verification command {command!r} uses shell operators; commands run without a shell"
        )
    return argv


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]  # redacted
    cwd: str
    category: CommandCategory
    exit_code: int | None
    stdout: str  # redacted, truncated
    stderr: str  # redacted, truncated
    duration_seconds: float
    timed_out: bool
    truncated: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _minimal_env() -> dict[str, str]:
    # Pass a small allow-list rather than the full environment, so verification
    # commands do not inherit unrelated secrets. Project tools that need more can
    # read their own config files.
    keep = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
        "UV_CACHE_DIR",
    )
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["CI"] = "1"
    return env


def _truncate(data: bytes, limit: int) -> tuple[str, bool]:
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    return data[:limit].decode("utf-8", errors="replace") + "\n…[truncated]", True


async def terminate_process_group(proc: asyncio.subprocess.Process, grace: float = 5.0) -> bool:
    """SIGTERM the process group, then SIGKILL after ``grace`` seconds.

    Returns ``False`` if the process could not be confirmed stopped.
    """
    if proc.returncode is not None:
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return True
        except TimeoutError:
            continue
    return proc.returncode is not None


async def run_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    output_limit_bytes: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run ``argv`` without a shell in its own process group."""
    category = classify_command(argv)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env if env is not None else _minimal_env(),
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        await terminate_process_group(proc)
        out, err = b"", b""
    except asyncio.CancelledError:
        await terminate_process_group(proc)
        raise
    stdout, t1 = _truncate(out, output_limit_bytes)
    stderr, t2 = _truncate(err, output_limit_bytes)
    return CommandResult(
        argv=redact_args(argv),
        cwd=str(cwd),
        category=category,
        exit_code=None if timed_out else proc.returncode,
        stdout=redact(stdout),
        stderr=redact(stderr),
        duration_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        truncated=t1 or t2,
    )
