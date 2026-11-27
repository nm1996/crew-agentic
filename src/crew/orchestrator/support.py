"""Types, constants and helpers shared by the orchestrator modules."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from crew.config.loader import ProjectPaths
from crew.config.schema import CrewConfig
from crew.domain.errors import CrewError
from crew.persistence.store import Store
from crew.workspace.git import Git
from crew.workspace.worktrees import WorkspaceManager

Notify = Callable[[str], None]


# Shown a permission request (dict from the permission server); returns
# "allow" (once), "allow_run" (for the rest of the run) or "deny".
AskPermission = Callable[[dict[str, Any]], str]


T = TypeVar("T")


# Linked documentation (Confluence pages, …) added to the Director's context.
LINKED_DOCS_MAX_CHARS = 30_000


# How often running stages check for a developer cancellation request.
CANCEL_POLL_SECONDS = 0.5


# The run whose work is in progress; lets the git recorder attribute commands.
CURRENT_RUN: ContextVar[str | None] = ContextVar("crew_current_run", default=None)


class RunCancelled(Exception):
    """Raised inside a stage when the developer requested cancellation."""


PERMISSION_POLL_SECONDS = 0.3


async def stop_task(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def read_json_list(path: Path) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def expect_type[M](value: object, cls: type[M]) -> M:
    if not isinstance(value, cls):
        raise CrewError(f"expected {cls.__name__} from agent runtime, got {type(value).__name__}")
    return value


@dataclass
class CleanupReport:
    removed_worktrees: list[str] = field(default_factory=list)
    kept_worktrees: list[str] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    kept_branches: list[str] = field(default_factory=list)


@dataclass
class Project:
    """Everything a use case needs about one CREW-initialised repository."""

    paths: ProjectPaths
    config: CrewConfig
    store: Store
    git: Git
    workspace: WorkspaceManager
