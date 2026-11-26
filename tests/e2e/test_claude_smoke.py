"""Opt-in smoke test against the real Claude Code CLI.

Skipped unless CREW_CLAUDE_SMOKE=1 and `claude` is installed and logged in.
It runs the Director (read-only) on a tiny repository; this costs a small amount.

    CREW_CLAUDE_SMOKE=1 uv run pytest -m claude -s
"""

import os
import shutil
import subprocess

import pytest

from crew.application.project import configure, make_orchestrator, open_project
from crew.domain.enums import RunState

pytestmark = [
    pytest.mark.claude,
    pytest.mark.skipif(
        os.environ.get("CREW_CLAUDE_SMOKE") != "1" or not shutil.which("claude"),
        reason="set CREW_CLAUDE_SMOKE=1 with an authenticated Claude Code CLI",
    ),
]


def test_director_produces_a_valid_plan(project, repo):
    configure(open_project(repo), ["provider.default=claude_code"])
    real = open_project(repo)
    orch = make_orchestrator(real, notify=print)
    run = orch.start_run("Add a function goodbye() to app.py that returns 'bye', with a test.")
    assert run.state == RunState.AWAITING_PLAN_APPROVAL, run.failure_reason
    assert real.store.tasks(run.id)


def test_full_run_with_real_implementer(project, repo):
    """AC 4: the real adapter executes a task in a controlled worktree."""
    configure(open_project(repo), ["provider.default=claude_code"])
    real = open_project(repo)
    orch = make_orchestrator(real, notify=print)
    run = orch.start_run("Add a function goodbye() to app.py that returns the string 'bye'.")
    assert run.state == RunState.AWAITING_PLAN_APPROVAL, run.failure_reason
    run = orch.approve_plan(run.id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL, run.failure_reason
    tasks = real.store.tasks(run.id)
    assert all(t.changed_files for t in tasks if t.kind == "PLANNED")
    content = real.git.run("show", f"{run.integration_branch}:app.py")
    assert "def goodbye" in content
    assert "goodbye" not in (repo / "app.py").read_text()  # user tree untouched


def test_implementer_can_use_a_scoped_shell_rule(project, repo):
    """Scoped shell rules (with spaces) reach Claude intact and are honoured.

    POSIX uses a Bash(...) rule; Windows additionally grants PowerShell(...),
    whichever shell tool the installed Claude Code offers.
    """
    python = "python" if os.name == "nt" else "python3"
    rules = [f"Bash({python} --version:*)"]
    if os.name == "nt":
        rules.append(f"PowerShell({python} --version:*)")
    configure(
        open_project(repo),
        ["provider.default=claude_code", f"provider.claude_code.implementer_bash_rules={rules}"],
    )
    real = open_project(repo)
    orch = make_orchestrator(real, notify=print)
    run = orch.start_run(
        f"Run the shell command `{python} --version` and write its exact output into a new "
        "file version.txt. Do not change anything else."
    )
    assert run.state == RunState.AWAITING_PLAN_APPROVAL, run.failure_reason
    run = orch.approve_plan(run.id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL, run.failure_reason
    content = real.git.run("show", f"{run.integration_branch}:version.txt")
    actual = subprocess.run(
        [python, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert actual in content  # the agent ran the command rather than guessing


def test_permission_prompt_tool_with_the_real_cli(project, repo):
    """A command outside the rules reaches CREW's MCP approval tool and the developer."""
    python = "python" if os.name == "nt" else "python3"
    configure(
        open_project(repo),
        [
            "provider.default=claude_code",
            "approvals.permission_prompts=ask",
            "approvals.permission_timeout_seconds=120",
        ],
    )
    real = open_project(repo)
    asked: list[dict] = []

    def allow(request: dict) -> str:
        asked.append(request)
        return "allow"

    orch = make_orchestrator(real, notify=print, ask_permission=allow)
    run = orch.start_run(
        f'Run the shell command `{python} -c "print(6*7)"` and write its output into a new '
        "file answer.txt. Do not change anything else."
    )
    assert run.state == RunState.AWAITING_PLAN_APPROVAL, run.failure_reason
    run = orch.approve_plan(run.id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL, run.failure_reason
    assert asked, "the agent never asked for permission"
    assert any(r["tool_name"] in ("Bash", "PowerShell") for r in asked)
    assert "42" in real.git.run("show", f"{run.integration_branch}:answer.txt")
    assert any(a.kind == "PERMISSION" for a in real.store.approvals(run.id))
