"""Builders for the per-role input payloads (the user prompt sent to an agent).

Payloads are Markdown with clearly labelled sections so agents can tell the
original request, approved plan, CREW-recorded evidence and other agents'
*claims* apart.
"""

from __future__ import annotations

import json
from typing import Any

from crew.persistence.models import CommandExecution, Run, Task

DIFF_MAX_CHARS = 120_000


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _truncate(text: str, limit: int) -> str:
    return (
        text if len(text) <= limit else text[:limit] + "\n…[diff truncated; inspect files directly]"
    )


def director_prompt(run: Run, context_md: str, previous_error: str | None = None) -> str:
    parts = [
        "# Original request",
        "",
        run.request,
        "",
        "# Repository context",
        "",
        context_md,
    ]
    if previous_error:
        parts += [
            "",
            "# Your previous plan was rejected by CREW validation",
            "",
            previous_error,
            "Return a corrected plan.",
        ]
    return "\n".join(parts)


def implementer_prompt(run: Run, task: Task, dependency_reports: dict[str, Any]) -> str:
    plan = run.plan or {}
    parts = [
        "# Original request",
        "",
        run.request,
        "",
        "# Approved approach",
        "",
        str(plan.get("approach", "")),
        "",
        f"# Your task: {task.key} — {task.title}",
        "",
        task.description,
        "",
        "## Acceptance criteria",
        *[f"- {c}" for c in task.acceptance_criteria],
    ]
    if task.expected_files:
        parts += ["", "## Expected areas/files", *[f"- {f}" for f in task.expected_files]]
    if task.rationale:
        parts += ["", "## Rationale", task.rationale]
    if dependency_reports:
        parts += [
            "",
            "## Completed dependency tasks (their changes are already in this worktree)",
            "",
            "```json",
            _json(dependency_reports),
            "```",
        ]
    return "\n".join(parts)


def _commands_md(commands: list[CommandExecution]) -> str:
    if not commands:
        return "_No verification commands are configured; no CREW-run test evidence exists._"
    out = []
    for c in commands:
        argv = " ".join(json.loads(c.argv_json))
        status = "TIMED OUT" if c.timed_out else f"exit {c.exit_code}"
        tail = (c.stdout or "")[-3000:] + ("\n" + c.stderr[-2000:] if c.stderr else "")
        out += [f"### `{argv}` — {status}", "", "```", tail.strip(), "```", ""]
    return "\n".join(out)


def reviewer_prompt(
    run: Run,
    tasks: list[Task],
    diff: str,
    commands: list[CommandExecution],
    previous_review: dict[str, Any] | None,
) -> str:
    parts = [
        "# Original request",
        "",
        run.request,
        "",
        "# Approved plan",
        "",
        "```json",
        _json(run.plan),
        "```",
        "",
        "# Task acceptance criteria",
    ]
    for t in tasks:
        parts += [f"## {t.key}: {t.title}", *[f"- {c}" for c in t.acceptance_criteria]]
    parts += [
        "",
        f"# Integrated diff (base {run.base_commit[:12]} → integration branch)",
        "",
        "```diff",
        _truncate(diff, DIFF_MAX_CHARS),
        "```",
        "",
        "# Verification evidence recorded by CREW",
        "",
        _commands_md(commands),
    ]
    if previous_review:
        parts += [
            "",
            "# Previous review cycle (check whether blocking findings were resolved)",
            "",
            "```json",
            _json(previous_review),
            "```",
        ]
    return "\n".join(parts)


def correction_description(findings: list[dict[str, Any]]) -> str:
    lines = ["Fix the following blocking review findings in the integrated change:", ""]
    for i, f in enumerate(findings, 1):
        loc = f.get("file") or "-"
        if f.get("line_start"):
            loc += f":{f['line_start']}"
        lines += [
            f"{i}. [{f.get('severity')}/{f.get('category')}] {f.get('title')} ({loc})",
            f"   Problem: {f.get('explanation', '')}",
            f"   Recommended fix: {f.get('recommendation', '')}",
        ]
    return "\n".join(lines)


def evaluator_prompt(
    run: Run,
    tasks: list[Task],
    diff: str,
    commands: list[CommandExecution],
    reviews: list[dict[str, Any]],
) -> str:
    claims = {t.key: t.report for t in tasks if t.report}
    return "\n".join(
        [
            "# Original request (authoritative — do not reinterpret)",
            "",
            run.request,
            "",
            "# Approved plan",
            "",
            "```json",
            _json(run.plan),
            "```",
            "",
            "# Integrated diff",
            "",
            "```diff",
            _truncate(diff, DIFF_MAX_CHARS),
            "```",
            "",
            "# Verification evidence recorded by CREW (trustworthy)",
            "",
            _commands_md(commands),
            "",
            "# Reviewer findings, all cycles (re-evaluate, do not copy)",
            "",
            "```json",
            _json(reviews),
            "```",
            "",
            "# Implementer reports (CLAIMS — verify against the diff and evidence)",
            "",
            "```json",
            _json(claims),
            "```",
        ]
    )
