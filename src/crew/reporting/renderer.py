"""Markdown artifacts under ``.crew/runs/<run-id>/``.

Artifacts are human-readable evidence exports. The SQLite database remains the
authoritative state; every file here can be regenerated with ``crew report``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crew.domain.enums import ExecutionStatus, Role, RunState
from crew.persistence.models import CommandExecution, Run, Task
from crew.persistence.store import Store
from crew.security.redaction import redact


def _bullets(items: list[str] | None, empty: str = "_None._") -> list[str]:
    return [f"- {i}" for i in items] if items else [empty]


def render_request(run: Run) -> str:
    return "\n".join(
        [
            f"# Request — run {run.id}",
            "",
            run.request,
            "",
            f"- Base branch: `{run.base_branch or '(detached)'}`",
            f"- Base commit: `{run.base_commit}`",
            f"- Provider: `{run.provider}`",
            f"- Created: {run.created_at:%Y-%m-%d %H:%M:%S} UTC",
        ]
    )


def render_plan(plan: dict[str, Any]) -> str:
    lines = ["# Plan", "", "## Understanding", "", plan.get("understanding", ""), ""]
    if plan.get("blocking_ambiguity"):
        lines += ["> **Blocking ambiguity flagged by Director:** " + plan["blocking_ambiguity"], ""]
    lines += ["## Approach", "", plan.get("approach", ""), ""]
    for title, key in (
        ("Assumptions", "assumptions"),
        ("Open questions", "open_questions"),
        ("Scope", "scope"),
        ("Non-goals", "non_goals"),
        ("Risks", "risks"),
    ):
        lines += [f"## {title}", "", *_bullets(plan.get(key)), ""]
    lines += ["## Verification strategy", "", plan.get("verification_strategy") or "_None._", ""]
    lines += ["## Tasks", ""]
    for t in plan.get("tasks", []):
        deps = ", ".join(t.get("dependencies") or []) or "none"
        lines += [
            f"### `{t['id']}` — {t['title']}",
            "",
            t.get("description", ""),
            "",
            f"*Depends on:* {deps}",
            "",
            *[
                f"*Ordered by CREW after `{dep}`:* both change {', '.join(files)}\n"
                for dep, files in (t.get("crew_added_dependencies") or {}).items()
            ],
            "*Acceptance criteria:*",
            *_bullets(t.get("acceptance_criteria")),
            "",
        ]
    return "\n".join(lines)


def render_commands(commands: list[CommandExecution]) -> str:
    lines = ["# Test results (recorded by CREW)", ""]
    if not commands:
        lines.append("_No verification commands configured (`execution.verification_commands`)._")
        return "\n".join(lines)
    for c in commands:
        argv = " ".join(json.loads(c.argv_json))
        status = "TIMED OUT" if c.timed_out else f"exit {c.exit_code}"
        if c.sandbox and c.sandbox != "none":
            status += f", sandbox: {c.sandbox}"
        lines += [
            f"## Cycle {c.cycle}: `{argv}` — **{status}** ({c.duration_seconds}s)",
            "",
            "```",
            ((c.stdout or "") + ("\n" + c.stderr if c.stderr else "")).strip()[-6000:],
            "```",
            "",
        ]
    return "\n".join(lines)


def render_review(reviews: list[tuple[int, dict[str, Any]]]) -> str:
    lines = ["# Review", ""]
    for cycle, review in reviews:
        lines += [f"## Cycle {cycle}", "", review.get("assessment", ""), ""]
        findings = review.get("findings", [])
        if not findings:
            lines += ["_No findings._", ""]
        for f in findings:
            loc = f.get("file") or "-"
            if f.get("line_start"):
                loc += f":{f['line_start']}"
            flag = "BLOCKING" if f.get("blocking") else "non-blocking"
            lines += [
                f"### [{f['severity']}] {f['title']} ({flag})",
                "",
                f"- Category: {f['category']}",
                f"- Location: `{loc}`",
                f"- Explanation: {f.get('explanation', '')}",
                f"- Evidence: {f.get('evidence') or '-'}",
                f"- Recommendation: {f.get('recommendation') or '-'}",
                "",
            ]
        if review.get("checks_run"):
            lines += ["Checks run by reviewer:", *_bullets(review["checks_run"]), ""]
    return "\n".join(lines)


def render_evaluation(evaluation: dict[str, Any]) -> str:
    lines = [
        "# External evaluation",
        "",
        f"**Recommendation: {evaluation['recommendation']}** (advisory — the developer decides)",
        "",
        evaluation.get("explanation", ""),
        "",
        "| Requirement | Status | Evidence |",
        "|---|---|---|",
    ]
    for r in evaluation.get("requirements", []):
        cells = [
            str(r.get(k, "")).replace("|", "\\|").replace("\n", " ")
            for k in ("requirement", "status", "evidence")
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "## Unresolved risks", "", *_bullets(evaluation.get("unresolved_risks"))]
    lines += [
        "",
        "## Plan/implementation discrepancies",
        "",
        *_bullets(evaluation.get("discrepancies")),
    ]
    return "\n".join(lines)


def render_tasks(tasks: list[Task]) -> str:
    lines = [
        "| Task | Kind | Status | Attempts | Commit | Changed files |",
        "|---|---|---|---|---|---|",
    ]
    for t in tasks:
        files = ", ".join(f"`{f}`" for f in t.changed_files) or "-"
        commit = f"`{t.commit_sha[:10]}`" if t.commit_sha else "-"
        lines.append(f"| `{t.key}` | {t.kind} | {t.status} | {t.attempt} | {commit} | {files} |")
    return "\n".join(lines)


def render_linked_docs(pages: list) -> str:
    lines = ["## Linked documentation (provided by the developer)", ""]
    for page in pages:
        lines += [f"### {page.title}", "", f"Source: {page.url or page.ref}", "", page.body, ""]
    return "\n".join(lines)


def render_review_comment(review: dict[str, Any]) -> str:
    """Reviewer findings as a pull-request review body."""
    findings = review.get("findings", [])
    lines = ["**CREW Reviewer**", "", review.get("assessment", "")]
    if findings:
        lines += ["", "| Severity | Finding | Location | Blocking |", "|---|---|---|---|"]
        for f in findings:
            loc = f.get("file") or "-"
            if f.get("line_start"):
                loc += f":{f['line_start']}"
            title = str(f.get("title", "")).replace("|", "\\|")
            blocking = "yes" if f.get("blocking") else "no"
            lines.append(f"| {f.get('severity')} | {title} | `{loc}` | {blocking} |")
    return "\n".join(lines)


def render_issue_comment(run: Run) -> str:
    lines = [f"CREW run {run.id} was completed and accepted by the developer."]
    if run.pull_request_url:
        lines += ["", f"Pull request: {run.pull_request_url}"]
    plan = run.plan or {}
    if plan.get("approach"):
        lines += ["", "Approach:", str(plan["approach"])]
    return "\n".join(lines)


def render_usage(executions: list) -> str:
    """Cost, time, turns and tokens per role (provider-reported)."""
    if not executions:
        return "_No agent executions._"
    rows: dict[str, dict[str, float]] = {}
    for e in executions:
        r = rows.setdefault(
            e.role, {"n": 0, "cost": 0, "secs": 0, "turns": 0, "in": 0, "out": 0, "cache": 0}
        )
        usage = e.usage
        r["n"] += 1
        r["cost"] += e.cost_usd or 0
        r["secs"] += e.duration_seconds or 0
        r["turns"] += e.num_turns or 0
        r["in"] += usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        r["out"] += usage.get("output_tokens", 0)
        r["cache"] += usage.get("cache_read_input_tokens", 0)
    lines = [
        "| Role | Executions | Cost (USD) | Agent time (s) | Turns | Input tok | Output tok "
        "| Cache-read tok |",
        "|---|---|---|---|---|---|---|---|",
    ]
    total = {k: 0.0 for k in ("n", "cost", "secs", "turns", "in", "out", "cache")}
    for role in ("director", "implementer", "reviewer", "evaluator"):
        if role not in rows:
            continue
        r = rows[role]
        for k in total:
            total[k] += r[k]
        lines.append(
            f"| {role} | {r['n']:.0f} | {r['cost']:.4f} | {r['secs']:.0f} | {r['turns']:.0f} "
            f"| {r['in']:.0f} | {r['out']:.0f} | {r['cache']:.0f} |"
        )
    lines.append(
        f"| **total** | {total['n']:.0f} | **{total['cost']:.4f}** | {total['secs']:.0f} "
        f"| {total['turns']:.0f} | {total['in']:.0f} | {total['out']:.0f} | {total['cache']:.0f} |"
    )
    models = sorted({m for e in executions if e.model for m in e.model.split(",")})
    if models:
        lines += ["", "Models: " + ", ".join(f"`{m}`" for m in models)]
    return "\n".join(lines)


def render_final_report(store: Store, run: Run) -> str:
    tasks = store.tasks(run.id)
    approvals = store.approvals(run.id)
    verification = store.commands(run.id, purpose="verification")
    reviews = [
        e.output
        for e in store.executions(run.id, role=Role.REVIEWER, status=ExecutionStatus.SUCCEEDED)
    ]
    evaluation = store.latest_output(run.id, Role.EVALUATOR)
    executions = store.executions(run.id)
    cost = sum(e.cost_usd or 0 for e in executions)

    lines = [
        f"# CREW report — run {run.id}",
        "",
        f"**State:** {run.state}",
        "",
        "## Request",
        "",
        run.request,
        "",
        "## Run metadata",
        "",
        f"- Base: `{run.base_branch or '(detached)'}` @ `{run.base_commit[:12]}`",
        f"- Integration branch: `{run.integration_branch}`",
        f"- Provider: `{run.provider}`; agent executions: {len(executions)}; "
        f"reported cost: ${cost:.4f}",
        f"- Correction cycles used: {run.correction_cycle}",
    ]
    if run.failure_reason:
        lines += ["", f"> **Failure / stop reason:** {run.failure_reason}"]

    plan = run.plan
    if plan:
        lines += ["", "## Approved plan (summary)", "", plan.get("approach", "")]
    lines += ["", "## Tasks", "", render_tasks(tasks) if tasks else "_No tasks._"]
    failed = [t for t in tasks if t.failure_reason]
    if failed:
        lines += ["", "### Task failures", ""]
        lines += [f"- `{t.key}` (attempt {t.attempt}): {t.failure_reason}" for t in failed]

    lines += ["", "## Verification (commands run by CREW)", ""]
    if verification:
        for c in verification:
            argv = " ".join(json.loads(c.argv_json))
            status = "timed out" if c.timed_out else f"exit {c.exit_code}"
            lines.append(f"- cycle {c.cycle}: `{argv}` → {status}")
    else:
        lines.append("_No verification commands configured; no CREW-run test evidence._")

    lines += ["", "## Review findings", ""]
    if not reviews:
        lines.append("_No review performed._")
    for i, review in enumerate(reviews):
        if not review:
            continue
        blocking = [f for f in review.get("findings", []) if f.get("blocking")]
        lines.append(
            f"- cycle {i}: {len(review.get('findings', []))} finding(s), {len(blocking)} blocking"
        )
    last_review = reviews[-1] if reviews else None
    unresolved = [f for f in (last_review or {}).get("findings", []) if f.get("blocking")]
    if unresolved:
        lines += ["", "**Unresolved blocking findings:**"]
        lines += [
            f"- [{f['severity']}] {f['title']} (`{f.get('file') or '-'}`)" for f in unresolved
        ]

    lines += ["", "## External evaluation", ""]
    if evaluation:
        lines += [f"**{evaluation['recommendation']}** — {evaluation.get('explanation', '')}"]
        lines += ["", "Unresolved risks:", *_bullets(evaluation.get("unresolved_risks"))]
    else:
        lines.append("_Not performed._")

    lines += ["", "## Agent usage", "", render_usage(executions)]

    lines += ["", "## Developer decisions", ""]
    lines += [
        f"- {a.decided_at:%Y-%m-%d %H:%M} UTC — {a.kind}: **{a.decision}**"
        + (f" ({a.note})" if a.note else "")
        for a in approvals
    ] or ["_None yet._"]

    lines += ["", "## Next steps", ""]
    state = RunState(run.state)
    if state is RunState.AWAITING_PLAN_APPROVAL:
        lines.append(
            f"Review the plan, then `crew approve-plan {run.id}` or `crew reject-plan {run.id}`."
        )
    elif state is RunState.AWAITING_FINAL_APPROVAL:
        lines += [
            f"Inspect the change: `git diff {run.base_commit[:12]}...{run.integration_branch}`",
            f"Then `crew accept {run.id}` or `crew reject {run.id}`.",
        ]
    elif state is RunState.COMPLETED:
        lines += [
            "CREW never merges into your branch. To take the result:",
            "",
            "```bash",
            f"git merge --no-ff {run.integration_branch}",
            "```",
        ]
    elif state is RunState.FAILED and run.failed_state:
        lines += [
            f"The run failed during {run.failed_state}. After fixing the cause, retry from "
            f"there with `crew retry {run.id}`.",
            "",
            f"Worktrees and branches were preserved under `.crew/worktrees/{run.id}/`.",
        ]
    elif state in (RunState.FAILED, RunState.CANCELLED):
        lines.append(
            "Worktrees and branches were preserved for inspection under `.crew/worktrees/"
            f"{run.id}/`. Nothing in your working tree was modified."
        )
    return "\n".join(lines) + "\n"


def write_artifact(store: Store, run_dir: Path, run_id: str, name: str, content: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / name
    path.write_text(redact(content), encoding="utf-8")
    store.upsert_artifact(run_id, name, path)
    return path
