import sys

import pytest

from crew.domain.enums import CommandCategory
from crew.domain.errors import PolicyViolationError
from crew.security.execution_policy import (
    classify_command,
    parse_verification_command,
    run_command,
)


@pytest.mark.parametrize(
    ("argv", "category"),
    [
        (["git", "status"], CommandCategory.READ_ONLY),
        (["git", "-C", "x", "diff"], CommandCategory.READ_ONLY),
        (["git", "reset", "--hard"], CommandCategory.DESTRUCTIVE),
        (["git", "clean", "-fd"], CommandCategory.DESTRUCTIVE),
        (["git", "stash"], CommandCategory.DESTRUCTIVE),
        (["git", "push", "origin", "main"], CommandCategory.NETWORK),
        (["git", "commit", "-m", "x"], CommandCategory.WORKTREE_WRITE),
        (["rm", "-rf", "/"], CommandCategory.DESTRUCTIVE),
        (["sudo", "ls"], CommandCategory.DESTRUCTIVE),
        (["bash", "-c", "echo hi"], CommandCategory.DESTRUCTIVE),
        (["curl", "https://example.com"], CommandCategory.NETWORK),
        (["pytest", "-q"], CommandCategory.VERIFICATION),
        (["ls"], CommandCategory.READ_ONLY),
    ],
)
def test_classification(argv, category):
    assert classify_command(argv) is category


@pytest.mark.parametrize(
    "command", ["rm -rf build", "curl http://x", "pytest | tee out", "sh -c 'pytest'"]
)
def test_unsafe_verification_commands_are_refused(command):
    with pytest.raises(PolicyViolationError):
        parse_verification_command(command)


def test_verification_command_is_parsed_without_shell():
    assert parse_verification_command("uv run pytest -q") == ["uv", "run", "pytest", "-q"]


async def test_run_command_captures_exit_code(tmp_path):
    result = await run_command(
        [sys.executable, "-c", "import sys; print('ok'); sys.exit(3)"],
        cwd=tmp_path,
        timeout_seconds=10,
        output_limit_bytes=1000,
    )
    assert result.exit_code == 3
    assert result.stdout.strip() == "ok"
    assert not result.ok


async def test_run_command_times_out_and_kills(tmp_path):
    result = await run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout_seconds=0.5,
        output_limit_bytes=1000,
    )
    assert result.timed_out
    assert result.exit_code is None
    assert result.duration_seconds < 10


async def test_run_command_truncates_and_redacts(tmp_path):
    code = "print('x' * 5000); print('ghp_' + 'a' * 30)"
    result = await run_command(
        [sys.executable, "-c", code], cwd=tmp_path, timeout_seconds=10, output_limit_bytes=1000
    )
    assert result.truncated
    assert len(result.stdout) < 1200
    result = await run_command(
        [sys.executable, "-c", "print('ghp_' + 'a' * 30)"],
        cwd=tmp_path,
        timeout_seconds=10,
        output_limit_bytes=1000,
    )
    assert "ghp_" not in result.stdout


async def test_run_command_does_not_leak_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "do-not-leak")
    result = await run_command(
        [sys.executable, "-c", "import os; print(os.environ.get('SUPER_SECRET_TOKEN'))"],
        cwd=tmp_path,
        timeout_seconds=10,
        output_limit_bytes=1000,
    )
    assert "do-not-leak" not in result.stdout
