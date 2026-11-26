from pathlib import Path

from crew.application.doctor import Level, run_doctor
from crew.application.project import configure, init_project, open_project


def by_name(checks):
    return {c.name: c for c in checks}


def test_doctor_on_uninitialised_repo_explains_fix(repo: Path):
    checks = by_name(run_doctor(repo))
    assert checks["Repository"].level is Level.OK
    assert checks["CREW project"].level is Level.FAIL
    assert "crew init" in checks["CREW project"].fix


def test_doctor_reports_missing_claude_cli_with_fix(repo: Path):
    init_project(repo)
    configure(open_project(repo), ["provider.claude_code.executable=no-such-claude-binary"])
    checks = by_name(run_doctor(repo))
    assert checks["Claude Code CLI"].level is Level.FAIL
    assert "Install Claude Code" in checks["Claude Code CLI"].fix


def test_missing_claude_is_only_a_warning_with_fake_provider(repo: Path):
    init_project(repo, provider="fake")
    configure(open_project(repo), ["provider.claude_code.executable=no-such-claude-binary"])
    checks = by_name(run_doctor(repo))
    assert checks["Claude Code CLI"].level is Level.WARN
    assert not [c for c in checks.values() if c.level is Level.FAIL]


def test_doctor_warns_on_dirty_tree(repo: Path):
    init_project(repo, provider="fake")
    (repo / "app.py").write_text("changed\n")
    assert by_name(run_doctor(repo))["Working tree"].level is Level.WARN


def test_doctor_outside_git(tmp_path: Path):
    checks = by_name(run_doctor(tmp_path))
    assert checks["Repository"].level is Level.FAIL
