"""Opt-in smoke test against the real Claude Code CLI.

Skipped unless CREW_CLAUDE_SMOKE=1 and `claude` is installed and logged in.
It runs the Director (read-only) on a tiny repository; this costs a small amount.

    CREW_CLAUDE_SMOKE=1 uv run pytest -m claude -s
"""

import os
import shutil

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
