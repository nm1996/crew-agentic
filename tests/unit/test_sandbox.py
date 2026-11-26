"""Sandbox argv construction and, where available, real isolation."""

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from crew.config.loader import parse_config
from crew.domain.errors import ConfigError, PreconditionError
from crew.security.sandbox import SandboxSettings, check_available, wrap

BWRAP = SandboxSettings(mode="bwrap")
bwrap_works = check_available(BWRAP) is None
docker_works = (
    shutil.which("docker") is not None
    and check_available(SandboxSettings(mode="docker", image="alpine:3.20")) is None
)


def test_none_is_passthrough(tmp_path):
    assert wrap(["pytest"], tmp_path, SandboxSettings()) == ["pytest"]


def test_bwrap_argv_has_no_network_and_readonly_root(tmp_path):
    argv = wrap(["pytest", "-q"], tmp_path, BWRAP)
    assert argv[0] == "bwrap" and "--unshare-all" in argv and "--share-net" not in argv
    assert argv[argv.index("--ro-bind") + 1 : argv.index("--ro-bind") + 3] == ["/", "/"]
    assert argv[-3:] == ["--", "pytest", "-q"]
    assert "--share-net" in wrap(["x"], tmp_path, SandboxSettings(mode="bwrap", network=True))


def test_docker_argv_is_locked_down(tmp_path):
    argv = wrap(["pytest"], tmp_path, SandboxSettings(mode="docker", image="python:3.12"))
    joined = " ".join(argv)
    for expected in ("--network none", "--cap-drop ALL", "--read-only", "no-new-privileges"):
        assert expected in joined
    assert argv[-2:] == ["python:3.12", "pytest"]


def test_docker_requires_an_image(tmp_path):
    with pytest.raises(PreconditionError):
        wrap(["x"], tmp_path, SandboxSettings(mode="docker"))
    with pytest.raises(ConfigError, match="sandbox_image"):
        parse_config({"version": 1, "project": {"name": "t"}, "execution": {"sandbox": "docker"}})


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.skipif(not bwrap_works, reason="bubblewrap not usable here")
def test_bwrap_confines_writes_and_network(tmp_path):
    work = tmp_path / "worktree"
    work.mkdir()
    in_tmp = tmp_path / "outside.txt"  # lands in the sandbox's private /tmp, not the host's
    in_home = Path.home() / f".crew-sandbox-probe-{uuid.uuid4().hex}"  # read-only in sandbox
    code = (
        "import pathlib, socket, sys\n"
        "pathlib.Path('inside.txt').write_text('ok')\n"
        f"pathlib.Path({str(in_tmp)!r}).write_text('private tmp')\n"
        f"try:\n    pathlib.Path({str(in_home)!r}).write_text('escaped')\n"
        "except OSError:\n    print('outside-blocked')\n"
        "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "except OSError:\n    print('network-blocked')\n"
    )
    try:
        result = _run(wrap([sys.executable, "-c", code], work, BWRAP), work)
        assert result.returncode == 0, result.stderr
        assert (work / "inside.txt").read_text() == "ok"
        assert not in_tmp.exists() and not in_home.exists()  # nothing reached the host
    finally:
        in_home.unlink(missing_ok=True)
    assert "outside-blocked" in result.stdout and "network-blocked" in result.stdout


@pytest.mark.skipif(not bwrap_works, reason="bubblewrap not usable here")
def test_bwrap_hides_credential_directories(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE")
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "w"
    work.mkdir()
    code = f"import os; print(os.listdir({str(home / '.ssh')!r}))"
    result = _run(wrap([sys.executable, "-c", code], work, BWRAP), work)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


@pytest.mark.skipif(not docker_works, reason="docker daemon not available")
def test_docker_confines_the_container(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    settings = SandboxSettings(mode="docker", image="alpine:3.20")
    listing = _run(wrap(["ls", "/workspace"], tmp_path, settings), tmp_path)
    assert listing.returncode == 0 and "file.txt" in listing.stdout
    escape = _run(wrap(["touch", "/etc/escaped"], tmp_path, settings), tmp_path)
    assert escape.returncode != 0  # read-only root filesystem
    if hasattr(os, "getuid"):
        whoami = _run(wrap(["id", "-u"], tmp_path, settings), tmp_path)
        assert whoami.stdout.strip() == str(os.getuid())
