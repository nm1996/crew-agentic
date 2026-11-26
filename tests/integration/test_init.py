from pathlib import Path

import pytest

from crew.application.project import init_project
from crew.config.loader import ProjectPaths, load_config
from crew.domain.errors import PreconditionError
from crew.persistence.database import schema_status
from tests.conftest import git


def test_init_creates_project_layout(repo: Path):
    result = init_project(repo, provider="fake")
    paths = ProjectPaths(repo)
    assert paths.config_file.is_file()
    assert (paths.context_dir / "project.md").is_file()
    assert load_config(paths).provider.default == "fake"
    current, head = schema_status(paths.db_file)
    assert current == head is not None
    assert ".crew/" in (repo / ".git" / "info" / "exclude").read_text()
    assert result.created


def test_init_keeps_crew_state_out_of_git_status(repo: Path):
    init_project(repo)
    assert git(repo, "status", "--porcelain").strip() == ""


def test_init_is_idempotent_and_never_overwrites(repo: Path):
    init_project(repo, name="first")
    paths = ProjectPaths(repo)
    notes = paths.context_dir / "project.md"
    notes.write_text("my notes")
    original = paths.config_file.read_text()

    result = init_project(repo, name="second")

    assert paths.config_file.read_text() == original
    assert notes.read_text() == "my notes"
    assert not result.created
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.count(".crew/") == 1


def test_init_refuses_outside_git(tmp_path: Path):
    with pytest.raises(PreconditionError, match="not inside a Git repository"):
        init_project(tmp_path)


def test_init_from_subdirectory_uses_repo_root(repo: Path):
    sub = repo / "pkg"
    sub.mkdir()
    result = init_project(sub)
    assert result.root == repo
