from pathlib import Path

import pytest

from crew.domain.errors import PolicyViolationError, PreconditionError
from crew.orchestrator.engine import Project
from tests.conftest import git


def test_destructive_git_refused_in_user_worktree(project: Project):
    for args in (("reset", "--hard"), ("clean", "-fd"), ("stash",), ("push", "origin", "main")):
        with pytest.raises(PolicyViolationError):
            project.git.run(*args)


def test_dirty_user_tree_is_detected(project: Project, repo: Path):
    project.workspace.require_clean_user_worktree()
    (repo / "new.txt").write_text("untracked")
    with pytest.raises(PreconditionError, match="uncommitted"):
        project.workspace.require_clean_user_worktree()
    assert (repo / "new.txt").read_text() == "untracked"  # never touched


def test_task_worktree_includes_dependency_work(project: Project, repo: Path):
    ws = project.workspace
    base = project.git.head()
    ws.ensure_integration_worktree("r1", base)
    dep_path, dep_branch = ws.create_task_worktree("r1", "dep", 1, ws.integration_branch("r1"), [])
    (dep_path / "dep.txt").write_text("dependency output")
    project.git.at(dep_path).commit_all("dep work")

    path, _ = ws.create_task_worktree("r1", "child", 1, ws.integration_branch("r1"), [dep_branch])
    assert (path / "dep.txt").read_text() == "dependency output"
    assert project.git.head() == base  # user branch untouched
    assert not (repo / "dep.txt").exists()


def test_task_worktree_slots_are_never_reused(project: Project):
    ws = project.workspace
    base = project.git.head()
    ws.create_task_worktree("r1", "t", 1, base, [])
    with pytest.raises(PreconditionError, match="already exists"):
        ws.create_task_worktree("r1", "t", 1, base, [])


def test_dirty_worktree_is_preserved_on_cleanup(project: Project):
    ws = project.workspace
    path, _ = ws.create_task_worktree("r1", "t", 1, project.git.head(), [])
    (path / "wip.txt").write_text("uncommitted")
    assert ws.remove_worktree_if_clean(path) is False
    assert (path / "wip.txt").exists()


def test_merge_conflict_is_detected_and_aborted(project: Project, repo: Path):
    ws = project.workspace
    base = project.git.head()
    a, a_branch = ws.create_task_worktree("r1", "a", 1, base, [])
    b, b_branch = ws.create_task_worktree("r1", "b", 1, base, [])
    (a / "app.py").write_text("version a\n")
    (b / "app.py").write_text("version b\n")
    project.git.at(a).commit_all("a")
    project.git.at(b).commit_all("b")
    integration = ws.ensure_integration_worktree("r1", base)
    igit = project.git.at(integration)
    assert igit.merge_no_ff(a_branch, "merge a")
    assert igit.merge_no_ff(b_branch, "merge b") is False
    assert igit.conflicted_files() == ["app.py"]
    igit.merge_abort()
    assert git(integration, "status", "--porcelain").strip() == ""


def test_writes_are_refused_in_the_user_worktree(project: Project, repo: Path):
    (repo / "new.txt").write_text("user work")
    for args in (("add", "--all"), ("commit", "-m", "x"), ("merge", "main"), ("rm", "app.py")):
        with pytest.raises(PolicyViolationError, match="developer's worktree"):
            project.git.run(*args)
    assert git(repo, "status", "--porcelain").strip() == "?? new.txt"


def test_commit_all_keeps_new_secret_files_out(project: Project):
    path, _ = project.workspace.create_task_worktree("r1", "t", 1, project.git.head(), [])
    (path / ".env").write_text("API_KEY=very-secret")
    (path / ".env.example").write_text("API_KEY=")
    (path / "feature.py").write_text("x = 1\n")
    result = project.git.at(path).commit_all("work")
    assert result.sha is not None
    assert result.excluded == [".env"]
    committed = git(path, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == [".env.example", "feature.py"]
    assert (path / ".env").exists()  # left in place for inspection


def test_repository_hooks_never_run(project: Project, repo: Path):
    hook = repo / ".git" / "hooks" / "pre-commit"
    marker = repo.parent / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    path, _ = project.workspace.create_task_worktree("r1", "t", 1, project.git.head(), [])
    (path / "f.txt").write_text("x")
    git_wt = project.git.at(path)
    git_wt.run("add", "f.txt")
    git_wt.run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "x")
    assert not marker.exists()


def test_status_handles_unicode_and_renames(project: Project, repo: Path):
    git(repo, "mv", "app.py", "aplikacija čćž.py")
    status = project.git.status()
    assert status.dirty_paths == ["aplikacija čćž.py"]
