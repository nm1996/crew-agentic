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


SUMMARY_PATCH_CHARS = 12_000


def diff_section(base: str, patch: str, stat: str, files: list[str], *, mode: str = "full") -> str:
    """The integrated change as given to Reviewer and Evaluator.

    ``full`` embeds the whole patch (bounded by DIFF_MAX_CHARS). ``summary`` gives
    stats, the changed-file list and only the start of the patch, and tells the
    agent to open files itself: far fewer prompt tokens on large changes.
    """
    header = f"# Integrated change (base {base[:12]} → integration branch)"
    if mode == "full":
        return "\n".join([header, "", "```diff", _truncate(patch, DIFF_MAX_CHARS), "```"])
    head = patch[:SUMMARY_PATCH_CHARS]
    more = len(patch) > SUMMARY_PATCH_CHARS
    return "\n".join(
        [
            header,
            "",
            "Your working directory contains the integrated result. The patch below is",
            "abridged; open the changed files with your Read tool to inspect them.",
            "",
            "## Diff stat",
            "",
            "```",
            stat.strip(),
            "```",
            "",
            "## Changed files",
            "",
            *[f"- {f}" for f in files],
            "",
            "## Patch (beginning)" + (" — truncated" if more else ""),
            "",
            "```diff",
            head,
            "```",
        ]
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
        *(
            ["# Project notes from the Director", "", *[f"- {n}" for n in notes], ""]
            if (notes := plan.get("project_notes"))
            else []
        ),
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
        if c.sandbox and c.sandbox != "none":
            status += f", sandbox {c.sandbox}"
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
        diff,
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


def conflict_prompt(
    run: Run, into: str, ref: str, files: list[str], reports: dict[str, Any]
) -> str:
    return "\n".join(
        [
            "# Resolve a merge conflict",
            "",
            f"CREW merged `{ref}` into {into} in this worktree and Git stopped with",
            "conflicts. Edit the files below so that every conflict is resolved:",
            "",
            *[f"- {f}" for f in files],
            "",
            "Rules:",
            "- Remove every `<<<<<<<`, `=======` and `>>>>>>>` marker.",
            "- Keep the intent of both sides. If both sides made the same change, keep it once.",
            "- Do not change, create or delete any other file and do not refactor; CREW",
            "  rejects the resolution if anything else changed. CREW commits the result.",
            "- Do not run `git add`, `git commit` or `git merge` yourself.",
            "",
            "# Original request",
            "",
            run.request,
            "",
            "# What each side was doing (task reports)",
            "",
            "```json",
            _json(reports),
            "```",
        ]
    )


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
            diff,
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
