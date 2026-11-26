"""Use cases that do not drive a run: opening a project, ``init``, ``configure``."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from crew.config.loader import (
    ProjectPaths,
    find_project_root,
    load_config,
    parse_config,
    write_config,
)
from crew.config.schema import CrewConfig, ProjectSettings, ProviderSettings
from crew.domain.errors import ConfigError, PreconditionError
from crew.orchestrator.engine import Notify, Orchestrator, Project
from crew.persistence.database import migrate
from crew.persistence.store import Store
from crew.providers.base import AgentProvider
from crew.providers.claude_code.adapter import ClaudeCodeProvider
from crew.providers.fake import FakeProvider
from crew.security.redaction import RedactingFilter
from crew.workspace.git import Git
from crew.workspace.worktrees import WorkspaceManager

PROJECT_NOTES_TEMPLATE = """\
# Project notes for CREW agents

Everything in `.crew/context/*.md` is given to the Director as developer-written
context. Keep it short and factual. Never put secrets here.

## Conventions
- (e.g. "Use pytest; tests live in tests/; run `uv run pytest`")

## Architecture notes
- (e.g. "HTTP handlers live in app/api/, business logic in app/services/")

## Off-limits
- (e.g. "Do not touch migrations/ without asking")
"""

EXCLUDE_ENTRY = ".crew/"


def open_project(start: Path | None = None) -> Project:
    root = find_project_root(start)
    paths = ProjectPaths(root)
    config = load_config(paths)
    migrate(paths.db_file)  # cheap no-op when already at head
    return build_project(paths, config)


def build_project(paths: ProjectPaths, config: CrewConfig) -> Project:
    worktree_root = paths.root / config.git.worktree_directory
    git = Git(paths.root, owned_roots=(worktree_root,))
    store = Store.open(paths.db_file)
    workspace = WorkspaceManager(
        git, paths.root, config.git.worktree_directory, config.git.branch_prefix
    )
    project = Project(paths=paths, config=config, store=store, git=git, workspace=workspace)
    _configure_file_logging(paths)
    return project


def make_provider(config: CrewConfig) -> AgentProvider:
    if config.provider.default == "fake":
        return FakeProvider()
    return ClaudeCodeProvider(config.provider.claude_code)


def make_orchestrator(
    project: Project, notify: Notify | None = None, provider: AgentProvider | None = None
) -> Orchestrator:
    return Orchestrator(project, provider or make_provider(project.config), notify)


def _configure_file_logging(paths: ProjectPaths) -> None:
    logger = logging.getLogger("crew")
    if any(getattr(h, "_crew_file", False) for h in logger.handlers):
        return
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(paths.logs_dir / "crew.log", encoding="utf-8")
    handler._crew_file = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


# ---- init -------------------------------------------------------------------


@dataclass
class InitResult:
    root: Path
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def init_project(
    directory: Path, *, name: str | None = None, provider: str = "claude_code"
) -> InitResult:
    """Create ``.crew/`` in the Git repository containing ``directory``.

    Idempotent and non-destructive: existing files are kept untouched.
    """
    git = Git(directory)
    if not git.is_repository():
        raise PreconditionError(
            f"{directory} is not inside a Git repository.",
            hint="CREW works on Git repositories: run `git init` (and commit) first.",
        )
    root = git.toplevel()
    paths = ProjectPaths(root)
    result = InitResult(root=root)

    for directory_path in (paths.crew_dir, paths.runs_dir, paths.context_dir, paths.logs_dir):
        directory_path.mkdir(parents=True, exist_ok=True)

    if paths.config_file.exists():
        load_config(paths)  # validate, but never overwrite
        result.kept.append(str(paths.config_file.relative_to(root)))
    else:
        config = CrewConfig(
            project=ProjectSettings(name=name or root.name),
            provider=ProviderSettings(default=provider),  # type: ignore[arg-type]
        )
        write_config(paths, config)
        result.created.append(str(paths.config_file.relative_to(root)))

    notes = paths.context_dir / "project.md"
    if notes.exists():
        result.kept.append(str(notes.relative_to(root)))
    else:
        notes.write_text(PROJECT_NOTES_TEMPLATE, encoding="utf-8")
        result.created.append(str(notes.relative_to(root)))

    existed = paths.db_file.exists()
    migrate(paths.db_file)
    (result.kept if existed else result.created).append(str(paths.db_file.relative_to(root)))

    # Keep CREW state out of `git status` without editing a tracked .gitignore:
    # .git/info/exclude is local to this clone.
    exclude = git.git_dir() / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if EXCLUDE_ENTRY not in current.splitlines():
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(("" if current.endswith("\n") or not current else "\n") + EXCLUDE_ENTRY + "\n")
        result.created.append(".git/info/exclude entry for .crew/")
    return result


# ---- configure --------------------------------------------------------------


def _coerce(value: str) -> Any:
    """Interpret a CLI value as YAML scalar/list (``2``, ``true``, ``[a, b]``)."""
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


def configure(project: Project, assignments: list[str]) -> CrewConfig:
    """Apply ``dotted.key=value`` assignments, validate, then write atomically."""
    data = project.config.model_dump(mode="json")
    for assignment in assignments:
        if "=" not in assignment:
            raise ConfigError(f"expected key=value, got {assignment!r}")
        key, raw = assignment.split("=", 1)
        parts = key.strip().split(".")
        node = data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                raise ConfigError(f"unknown configuration section {key!r}")
            node = node[part]
        if parts[-1] not in node:
            raise ConfigError(f"unknown configuration key {key!r}")
        node[parts[-1]] = _coerce(raw)
    config = parse_config(data, source="configure")
    write_config(project.paths, config)
    project.config = config
    return config


def config_as_json(config: CrewConfig) -> str:
    return json.dumps(config.model_dump(mode="json"), indent=2)
