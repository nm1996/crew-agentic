"""Shared fixtures: temporary Git repositories and CREW projects using the fake provider."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from crew.application.project import build_project, init_project, make_orchestrator
from crew.config.loader import ProjectPaths, load_config, write_config
from crew.orchestrator.engine import Orchestrator, Project
from crew.providers.fake import FakeProvider


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# Sample project\n")
    (root / "app.py").write_text("def hello():\n    return 'hi'\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def project(repo: Path) -> Project:
    init_project(repo, provider="fake")
    paths = ProjectPaths(repo)
    return build_project(paths, load_config(paths))


@pytest.fixture
def configure_project(project: Project) -> Callable[..., Project]:
    """Return a function that updates config sections and rebuilds the project."""

    def apply(**sections: dict) -> Project:
        data = project.config.model_dump(mode="json")
        for section, values in sections.items():
            data[section].update(values)
        config = type(project.config).model_validate(data)
        write_config(project.paths, config)
        return build_project(project.paths, config)

    return apply


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def make_orch(project: Project, fake: FakeProvider) -> Callable[..., Orchestrator]:
    def factory(p: Project | None = None, provider: FakeProvider | None = None) -> Orchestrator:
        return make_orchestrator(p or project, provider=provider or fake)

    return factory
