"""Optional sandbox for verification commands (``execution.sandbox``).

Verification commands run code that agents may have written (tests, build
scripts). The sandbox limits what that code can do:

``bwrap`` (Linux, bubblewrap)
    The whole filesystem is mounted **read-only**; only the integration worktree,
    a private ``/tmp`` and explicitly configured ``sandbox_writable_paths`` are
    writable. Well-known credential directories in ``$HOME`` are hidden behind
    empty tmpfs mounts. All namespaces are unshared, so **there is no network**
    unless ``sandbox_network`` is true. The sandbox dies with CREW.

``docker`` (any OS with Docker)
    The worktree is mounted at ``/workspace`` in ``sandbox_image``; the container
    runs as your UID (POSIX), with all capabilities dropped, no new privileges, a
    read-only root filesystem and no network unless ``sandbox_network`` is true.
    The command must exist *inside* the image.

``none`` (default)
    No sandbox; see the security note in docs/CONFIGURATION.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from crew.domain.errors import PreconditionError

SandboxMode = Literal["none", "bwrap", "docker"]

# Hidden inside the bwrap sandbox even though the rest of $HOME stays readable.
CREDENTIAL_DIRS = (
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".kube",
    ".docker",
    ".config/gh",
    ".config/gcloud",
    ".claude",
    ".netrc",
    ".npmrc",
    ".pypirc",
)
DOCKER_WORKDIR = "/workspace"
# A fresh, private tmpfs *inside* the sandbox, not the host's shared /tmp.
SANDBOX_TMP = "/tmp"  # noqa: S108


@dataclass(frozen=True)
class SandboxSettings:
    mode: SandboxMode = "none"
    image: str | None = None
    network: bool = False
    writable_paths: tuple[Path, ...] = ()


def resolves_on_host(mode: SandboxMode) -> bool:
    """Whether a command's program should be resolved on the host ``PATH``."""
    return mode != "docker"


def wrap(argv: list[str], cwd: Path, settings: SandboxSettings) -> list[str]:
    """Return the argv that runs ``argv`` inside the configured sandbox."""
    if settings.mode == "none":
        return argv
    if settings.mode == "bwrap":
        return _bwrap(argv, cwd, settings)
    return _docker(argv, cwd, settings)


def _bwrap(argv: list[str], cwd: Path, settings: SandboxSettings) -> list[str]:
    cwd = cwd.resolve()
    out = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if settings.network:
        out.append("--share-net")
    out += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", SANDBOX_TMP]
    home = Path.home()
    for name in CREDENTIAL_DIRS:
        target = home / name
        if target.is_dir():
            out += ["--tmpfs", str(target)]
        elif target.is_file():
            out += ["--ro-bind", "/dev/null", str(target)]
    for path in settings.writable_paths:
        resolved = path.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        out += ["--bind", str(resolved), str(resolved)]
    # The worktree's .git file points into the main repository; keep it readable
    # (it is, via the read-only root) but make the worktree itself writable.
    out += ["--bind", str(cwd), str(cwd), "--chdir", str(cwd), "--", *argv]
    return out


def _docker(argv: list[str], cwd: Path, settings: SandboxSettings) -> list[str]:
    if not settings.image:
        raise PreconditionError(
            "execution.sandbox is 'docker' but execution.sandbox_image is not set",
            hint="Set an image that contains your toolchain, e.g. python:3.12-slim.",
        )
    out = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--network",
        "bridge" if settings.network else "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        SANDBOX_TMP,
        "--env",
        "CI=1",
        "--env",
        f"HOME={SANDBOX_TMP}",
        "--volume",
        f"{cwd.resolve()}:{DOCKER_WORKDIR}",
        "--workdir",
        DOCKER_WORKDIR,
    ]
    if hasattr(os, "getuid"):
        out += ["--user", f"{os.getuid()}:{os.getgid()}"]
    return [*out, settings.image, *argv]


def check_available(settings: SandboxSettings) -> str | None:
    """Return a problem description if the sandbox cannot run here, else ``None``."""
    if settings.mode == "none":
        return None
    if settings.mode == "bwrap":
        if os.name == "nt" or not shutil.which("bwrap"):
            return "bubblewrap (bwrap) is not installed (Linux only)"
        probe = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--", "true"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            return f"bwrap cannot create a sandbox: {probe.stderr.strip()[:200]}"
        return None
    if not shutil.which("docker"):
        return "docker is not installed"
    if not settings.image:
        return "execution.sandbox_image is not set"
    info = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if info.returncode != 0:
        return "the Docker daemon is not reachable (is it running, and may you use it?)"
    return None
