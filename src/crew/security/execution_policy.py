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
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from crew.domain.enums import CommandCategory
from crew.domain.errors import PolicyViolationError
from crew.security.redaction import redact, redact_args
from crew.system.processes import spawn_kwargs, terminate_tree

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
    # Windows
    "del",
    "erase",
    "rd",
    "remove-item",
    "taskkill",
    "icacls",
    "takeown",
    "format",
    "reg",
    "runas",
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
    "symbolic-ref",
    "for-each-ref",
    "ls-tree",
    "rev-list",
    "show-ref",
    "status",
    "log",
    "diff",
    "show",
    "rev-parse",
    "ls-files",
    "merge-base",
    "cat-file",
}


def git_subcommand(args: list[str]) -> tuple[str, set[str]]:
    """Return git's subcommand (skipping ``-C dir`` / ``-c k=v`` globals) and its other args."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args):
        return "", set()
    return args[i], set(args[i + 1 :])


_NETWORK_TOKENS = {
    "curl",
    "wget",
    "ssh",
    "scp",
    "rsync",
    "nc",
    "ncat",
    "telnet",
    "ftp",
    "invoke-webrequest",
    "iwr",
    "bitsadmin",
    "certutil",
}
_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "cmd", "powershell", "pwsh"}
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
# Characters cmd.exe interprets when Windows runs a .cmd/.bat file implicitly.
_CMD_METACHARS = frozenset('&|<>^%!"\n\r')
_SHELL_OPERATORS = frozenset({"|", "&&", "||", ";", ">", ">>", "<", "&"})


def executable_name(path: str) -> str:
    """Normalised program name: ``C:\\x\\RM.EXE`` and ``/bin/rm`` both become ``rm``."""
    name = Path(path.replace("\\", "/")).name.casefold()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


_READ_ONLY = {"ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "echo", "true"}


def classify_command(argv: list[str]) -> CommandCategory:
    """Classify a command per SECURITY.md §3. Unknown commands are VERIFICATION."""
    if not argv:
        raise PolicyViolationError("empty command")
    exe = executable_name(argv[0])
    if exe in _DESTRUCTIVE_TOKENS or exe in _SHELLS:
        return CommandCategory.DESTRUCTIVE
    if exe in _NETWORK_TOKENS:
        return CommandCategory.NETWORK
    if exe == "git":
        sub, flags = git_subcommand(argv[1:])
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


def _split(command: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command)
    # Windows: keep backslashes in paths; strip surrounding quotes only.
    return [
        tok[1:-1] if len(tok) > 1 and tok[0] == tok[-1] == '"' else tok
        for tok in shlex.split(command, posix=False)
    ]


def parse_verification_command(command: str | list[str]) -> list[str]:
    """Turn a configured verification command into a safe argv.

    Accepts a string (split like a shell would, but never run by one) or an
    explicit argument list, which is the most portable form. The program is
    resolved on ``PATH`` (honouring ``PATHEXT`` on Windows).
    """
    argv = list(command) if isinstance(command, list) else _split(command)
    shown = command if isinstance(command, str) else " ".join(command)
    if not argv:
        raise PolicyViolationError("empty verification command")
    category = classify_command(argv)
    if category in (CommandCategory.DESTRUCTIVE, CommandCategory.NETWORK):
        raise PolicyViolationError(
            f"verification command {shown!r} is classified as {category.value}; "
            "only read-only and verification commands may run unattended",
        )
    if any(tok in _SHELL_OPERATORS for tok in argv):
        raise PolicyViolationError(
            f"verification command {shown!r} uses shell operators; commands run without a shell"
        )
    resolved = shutil.which(argv[0])
    if resolved:
        argv[0] = resolved
        if resolved.casefold().endswith((".cmd", ".bat")) and any(
            ch in _CMD_METACHARS for arg in argv[1:] for ch in arg
        ):
            # Windows runs batch files through cmd.exe, which would interpret these.
            raise PolicyViolationError(
                f"verification command {shown!r} runs a batch file with characters that "
                "cmd.exe interprets (& | < > ^ % ! quotes); simplify the arguments"
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


# Conventional "command not found / not executable" status used when spawning fails.
SPAWN_FAILED_EXIT_CODE = 127


_WINDOWS_ENV = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "HOMEDRIVE",
    "HOMEPATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def _minimal_env() -> dict[str, str]:
    # Pass a small allow-list rather than the full environment, so verification
    # commands do not inherit unrelated secrets. Project tools that need more can
    # read their own config files. Windows needs a few system variables to run
    # anything at all (e.g. SYSTEMROOT for sockets and the C runtime).
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
        *(_WINDOWS_ENV if os.name == "nt" else ()),
    )
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["CI"] = "1"
    return env


def _truncate(data: bytes, limit: int) -> tuple[str, bool]:
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    return data[:limit].decode("utf-8", errors="replace") + "\n…[truncated]", True


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
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env if env is not None else _minimal_env(),
            **spawn_kwargs(),
        )
    except OSError as exc:  # missing tool, permission denied, ...: record, don't crash the run
        return CommandResult(
            argv=redact_args(argv),
            cwd=str(cwd),
            category=category,
            exit_code=SPAWN_FAILED_EXIT_CODE,
            stdout="",
            stderr=redact(f"could not start command: {exc}"),
            duration_seconds=round(time.monotonic() - started, 3),
            timed_out=False,
            truncated=False,
        )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        await terminate_tree(proc)
        out, err = b"", b""
    except asyncio.CancelledError:
        await terminate_tree(proc)
        raise
    else:
        await terminate_tree(proc)  # reap background grandchildren, if any
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
