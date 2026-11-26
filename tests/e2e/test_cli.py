"""CLI behaviour: commands, output and exit codes (fake provider, no network)."""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crew.cli.app import app

runner = CliRunner()


@pytest.fixture
def in_repo(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    return repo


def invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


def run_id_from(output: str) -> str:
    match = re.search(r"Run (\d{8}-\d{6}-[0-9a-f]{4})", output)
    assert match, output
    return match.group(1)


def test_version():
    result = invoke("--version")
    assert result.exit_code == 0 and "crew 0.3.0" in result.output


def test_commands_before_init_exit_with_config_code(in_repo):
    result = invoke("status")
    assert result.exit_code == 2
    assert "crew init" in result.output


def test_full_cli_flow(in_repo):
    assert invoke("init", "--provider", "fake").exit_code == 0
    result = invoke("run", "Add a thing")
    assert result.exit_code == 0, result.output
    run_id = run_id_from(result.output)
    assert "AWAITING_PLAN_APPROVAL" in result.output

    assert "Create part A" in invoke("plan", run_id).output
    result = invoke("approve-plan", run_id)
    assert result.exit_code == 0 and "AWAITING_FINAL_APPROVAL" in result.output
    assert "part-c" in invoke("tasks", run_id).output

    shown = json.loads(invoke("show", run_id, "--json").output)
    assert shown["state"] == "AWAITING_FINAL_APPROVAL"
    assert "final-report.md" in shown["artifacts"]

    result = invoke("accept", run_id, "-m", "looks good")
    assert result.exit_code == 0 and "git merge --no-ff" in result.output
    assert run_id in invoke("history").output
    assert "COMPLETED" in invoke("status").output
    assert invoke("report", run_id, "--path").output.strip().endswith("final-report.md")


def test_wrong_gate_returns_transition_exit_code(in_repo):
    invoke("init", "--provider", "fake")
    run_id = run_id_from(invoke("run", "x").output)
    result = invoke("accept", run_id)
    assert result.exit_code == 4
    assert "expected AWAITING_FINAL_APPROVAL" in result.output


def test_unknown_run_returns_not_found(in_repo):
    invoke("init", "--provider", "fake")
    assert invoke("show", "nope").exit_code == 5


def test_configure_validates_before_writing(in_repo):
    invoke("init", "--provider", "fake")
    before = (in_repo / ".crew" / "config.yaml").read_text()
    result = invoke("configure", "execution.max_implementers=5")
    assert result.exit_code == 2
    assert (in_repo / ".crew" / "config.yaml").read_text() == before
    result = invoke("configure", "execution.max_implementers=1")
    assert result.exit_code == 0
    assert '"max_implementers": 1' in result.output


def test_doctor_exit_code_reflects_failures(in_repo):
    assert invoke("doctor").exit_code == 1  # not initialised
    invoke("init", "--provider", "fake")
    invoke("configure", "provider.claude_code.executable=no-such-claude")
    assert invoke("doctor").exit_code == 0  # fake provider: Claude CLI is only a warning
