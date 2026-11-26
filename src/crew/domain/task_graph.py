"""Task-graph validation and readiness rules (docs/WORKFLOW.md §3-4)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from crew.domain.enums import TaskStatus
from crew.domain.errors import PlanValidationError

# Task ids become part of Git branch names, so keep them ref-safe.
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Ids CREW generates itself (correction tasks) start with this; plans may not use it.
RESERVED_PREFIX = "crew-"


@dataclass(frozen=True)
class Node:
    id: str
    dependencies: tuple[str, ...]


def validate_graph(nodes: Sequence[Node]) -> list[str]:
    """Validate ids/dependencies and return a topological order.

    Raises :class:`PlanValidationError` listing every problem found.
    """
    problems: list[str] = []
    ids = [n.id for n in nodes]
    if not nodes:
        problems.append("plan contains no tasks")
    seen: set[str] = set()
    for node_id in ids:
        if not TASK_ID_RE.match(node_id):
            problems.append(
                f"task id {node_id!r} must match {TASK_ID_RE.pattern} (lowercase, ref-safe)"
            )
        if node_id.startswith(RESERVED_PREFIX):
            problems.append(f"task id {node_id!r} uses the reserved prefix {RESERVED_PREFIX!r}")
        if node_id in seen:
            problems.append(f"duplicate task id {node_id!r}")
        seen.add(node_id)
    for node in nodes:
        for dep in node.dependencies:
            if dep == node.id:
                problems.append(f"task {node.id!r} depends on itself")
            elif dep not in seen:
                problems.append(f"task {node.id!r} depends on unknown task {dep!r}")
    if problems:
        raise PlanValidationError("Invalid task graph: " + "; ".join(problems))

    order = topological_order({n.id: n.dependencies for n in nodes})
    if order is None:
        raise PlanValidationError("Invalid task graph: dependency cycle detected")
    return order


def topological_order(deps: Mapping[str, Iterable[str]]) -> list[str] | None:
    """Kahn's algorithm preserving input order among peers. ``None`` on a cycle."""
    remaining = {k: set(v) for k, v in deps.items()}
    order: list[str] = []
    while remaining:
        ready = [k for k, v in remaining.items() if not v]
        if not ready:
            return None
        for k in ready:
            order.append(k)
            del remaining[k]
        for v in remaining.values():
            v.difference_update(ready)
    return order


def is_ready(dependencies: Iterable[str], statuses: Mapping[str, TaskStatus]) -> bool:
    """A task is READY only when every dependency is COMPLETED."""
    return all(statuses.get(dep) is TaskStatus.COMPLETED for dep in dependencies)


def is_blocked(dependencies: Iterable[str], statuses: Mapping[str, TaskStatus]) -> bool:
    """A task is BLOCKED when any dependency can no longer complete."""
    dead = {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    return any(statuses.get(dep) in dead for dep in dependencies)
