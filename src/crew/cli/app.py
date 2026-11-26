"""Typer CLI. Thin layer: parse arguments, call application services, render.

Exit codes: 0 success; 1 generic CREW error; 2 configuration/not initialised;
3 precondition; 4 invalid transition; 5 not found; 6 run locked; 7 policy
violation; 8 git error; 9 provider error; 10 invalid plan; 130 interrupted.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from crew import __version__
from crew.application import doctor as doctor_service
from crew.application.project import (
    config_as_json,
    init_project,
    make_orchestrator,
    open_project,
)
from crew.application.project import (
    configure as configure_service,
)
from crew.domain.enums import GATE_STATES, TERMINAL_STATES, RunState
from crew.domain.errors import CrewError, NotFoundError
from crew.persistence.models import Run
from crew.system.processes import install_termination_handlers

app = typer.Typer(
    name="crew",
    help="CREW — local-first AI development orchestration with human approval gates.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()
err = Console(stderr=True)

STATE_STYLE = {
    RunState.COMPLETED: "green",
    RunState.AWAITING_PLAN_APPROVAL: "yellow",
    RunState.AWAITING_FINAL_APPROVAL: "yellow",
    RunState.FAILED: "red",
    RunState.CANCELLED: "red",
    RunState.REJECTED: "red",
    RunState.PLAN_REJECTED: "red",
}

RunIdArg = Annotated[str, typer.Argument(help="Run id (see `crew history`).")]
NoteOpt = Annotated[
    str | None, typer.Option("--note", "-m", help="Reason recorded with the decision.")
]


def handle_errors[F: Callable[..., Any]](fn: F) -> F:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except CrewError as exc:
            err.print(f"[bold red]error:[/] {exc.message}", soft_wrap=True)
            if exc.hint:
                err.print(f"[dim]hint:[/] {exc.hint}", soft_wrap=True)
            raise typer.Exit(exc.exit_code) from None
        except KeyboardInterrupt:
            err.print(
                "\n[yellow]Interrupted.[/] Run state was preserved; continue with "
                "`crew resume <run-id>`."
            )
            raise typer.Exit(130) from None

    return wrapper  # type: ignore[return-value]


def _notify(message: str) -> None:
    style = (
        "yellow" if message.startswith("[warning]") else "cyan" if message.startswith("→") else ""
    )
    console.print(message, style=style, markup=False, highlight=False)


def _state(run: Run) -> str:
    style = STATE_STYLE.get(RunState(run.state), "cyan")
    return f"[{style}]{run.state}[/]"


def _resolve_run(project: Any, run_id: str | None) -> Run:
    if run_id:
        return project.store.get_run(run_id)
    run = project.store.latest_run()
    if run is None:
        raise NotFoundError("No runs yet.", hint='Start one with `crew run "<request>"`.')
    return run


def _after_drive(run: Run) -> None:
    console.print(f"\nRun [bold]{run.id}[/] is {_state(run)}")
    state = RunState(run.state)
    if state is RunState.AWAITING_PLAN_APPROVAL:
        console.print(
            f"Review the plan: [bold]crew plan {run.id}[/]\n"
            f"Then: [bold]crew approve-plan {run.id}[/] or [bold]crew reject-plan {run.id}[/]"
        )
    elif state is RunState.AWAITING_FINAL_APPROVAL:
        console.print(
            f"Review the evidence: [bold]crew report {run.id}[/]\n"
            f"Then: [bold]crew accept {run.id}[/] or [bold]crew reject {run.id}[/]"
        )
    elif state is RunState.COMPLETED:
        console.print(
            "CREW does not merge into your branch. To take the result:\n"
            f"  [bold]git merge --no-ff {run.integration_branch}[/]"
        )
    elif run.failure_reason:
        console.print(f"[red]Reason:[/] {run.failure_reason}")
    if state is RunState.FAILED:
        raise typer.Exit(1)


def version_callback(value: bool) -> None:
    if value:
        console.print(f"crew {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", callback=version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """CREW orchestrates Director, Implementer, Reviewer and External Evaluator agents."""


# ---- setup ------------------------------------------------------------------


@app.command()
@handle_errors
def init(
    name: Annotated[
        str | None, typer.Option(help="Project name (default: directory name).")
    ] = None,
    provider: Annotated[
        str, typer.Option(help="Provider backend: claude_code or fake (offline demo).")
    ] = "claude_code",
) -> None:
    """Initialise CREW in the current Git repository (never overwrites files)."""
    if provider not in ("claude_code", "fake"):
        raise CrewError("provider must be 'claude_code' or 'fake'")
    result = init_project(Path.cwd(), name=name, provider=provider)
    console.print(f"CREW project at [bold]{result.root}[/]")
    for item in result.created:
        console.print(f"  [green]created[/] {item}")
    for item in result.kept:
        console.print(f"  [dim]kept[/]    {item}")
    console.print("Next: [bold]crew doctor[/]")


@app.command()
@handle_errors
def doctor() -> None:
    """Check Python, Git, the repository, CREW config and the Claude Code CLI."""
    checks = doctor_service.run_doctor(Path.cwd())
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Details")
    table.add_column("How to fix", style="dim")
    colors = {"OK": "green", "WARN": "yellow", "FAIL": "red"}
    for c in checks:
        table.add_row(c.name, f"[{colors[c.level]}]{c.level}[/]", c.detail, c.fix)
    console.print(table)
    if any(c.level is doctor_service.Level.FAIL for c in checks):
        raise typer.Exit(1)


@app.command()
@handle_errors
def configure(
    assignments: Annotated[
        list[str] | None,
        typer.Argument(help="dotted.key=value pairs, e.g. execution.max_implementers=1"),
    ] = None,
) -> None:
    """Show configuration, or set values (validated before writing)."""
    project = open_project()
    if assignments:
        configure_service(project, assignments)
        console.print("[green]Configuration updated.[/]")
    console.print_json(config_as_json(project.config))


# ---- workflow ---------------------------------------------------------------


@app.command()
@handle_errors
def run(
    request: Annotated[str, typer.Argument(help="What you want done, in plain language.")],
) -> None:
    """Start a run: analyse, plan, and stop at the plan-approval gate."""
    project = open_project()
    result = make_orchestrator(project, _notify).start_run(request)
    _after_drive(result)


@app.command("approve-plan")
@handle_errors
def approve_plan(
    run_id: RunIdArg,
    note: NoteOpt = None,
    no_continue: Annotated[
        bool,
        typer.Option("--no-continue", help="Record approval only; continue later with resume."),
    ] = False,
) -> None:
    """Approve the plan and execute implementation, review and evaluation."""
    project = open_project()
    result = make_orchestrator(project, _notify).approve_plan(run_id, note, proceed=not no_continue)
    _after_drive(result)


@app.command("reject-plan")
@handle_errors
def reject_plan(run_id: RunIdArg, note: NoteOpt = None) -> None:
    """Reject the proposed plan (run ends as PLAN_REJECTED)."""
    _after_drive(make_orchestrator(open_project(), _notify).reject_plan(run_id, note))


@app.command()
@handle_errors
def accept(run_id: RunIdArg, note: NoteOpt = None) -> None:
    """Final approval: accept the result (CREW still never merges for you)."""
    _after_drive(make_orchestrator(open_project(), _notify).accept(run_id, note))


@app.command()
@handle_errors
def reject(run_id: RunIdArg, note: NoteOpt = None) -> None:
    """Final decision: reject the result."""
    _after_drive(make_orchestrator(open_project(), _notify).reject(run_id, note))


@app.command()
@handle_errors
def resume(run_id: RunIdArg) -> None:
    """Resume an interrupted run after re-validating repository and worktree state."""
    _after_drive(make_orchestrator(open_project(), _notify).resume(run_id))


@app.command()
@handle_errors
def stop(run_id: RunIdArg) -> None:
    """Cancel a run (cooperatively if another process is driving it)."""
    run, immediate = make_orchestrator(open_project(), _notify).stop(run_id)
    if immediate:
        console.print(f"Run {run.id} is {_state(run)}. Worktrees were preserved.")
    else:
        console.print(
            f"Cancellation requested for {run.id}; the driving process will stop active agents."
        )


# ---- inspection -------------------------------------------------------------


@app.command()
@handle_errors
def status(
    run_id: Annotated[str | None, typer.Argument(help="Run id (default: latest).")] = None,
) -> None:
    """Show the state of a run (default: the latest)."""
    project = open_project()
    run = _resolve_run(project, run_id)
    tasks = project.store.tasks(run.id)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    console.print(f"Run [bold]{run.id}[/]: {_state(run)}")
    console.print(f"Request: {run.request}")
    console.print(
        "Tasks: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none yet")
    )
    if run.cancel_requested and RunState(run.state) not in TERMINAL_STATES:
        console.print("[yellow]Cancellation requested.[/]")
    if run.failure_reason:
        console.print(f"[red]Reason:[/] {run.failure_reason}")
    if RunState(run.state) in GATE_STATES:
        _after_drive(run)


@app.command()
@handle_errors
def tasks(run_id: RunIdArg) -> None:
    """List the tasks of a run."""
    project = open_project()
    project.store.get_run(run_id)
    table = Table(show_header=True, header_style="bold")
    for col in ("Task", "Cycle", "Status", "Deps", "Attempt", "Commit", "Title"):
        table.add_column(col)
    for t in project.store.tasks(run_id):
        table.add_row(
            t.key,
            str(t.cycle),
            t.status,
            ", ".join(t.dependencies) or "-",
            str(t.attempt),
            (t.commit_sha or "-")[:10],
            t.title,
        )
    console.print(table)


@app.command()
@handle_errors
def history(limit: Annotated[int, typer.Option(help="Number of runs to show.")] = 20) -> None:
    """List previous runs."""
    project = open_project()
    table = Table(show_header=True, header_style="bold")
    for col in ("Run", "Created (UTC)", "State", "Request"):
        table.add_column(col)
    for r in project.store.list_runs(limit):
        request = r.request if len(r.request) <= 70 else r.request[:67] + "…"
        table.add_row(r.id, f"{r.created_at:%Y-%m-%d %H:%M}", _state(r), request)
    console.print(table)


@app.command()
@handle_errors
def plan(run_id: RunIdArg) -> None:
    """Show the Director's plan for a run."""
    project = open_project()
    run = project.store.get_run(run_id)
    plan_file = project.paths.run_dir(run_id) / "plan.md"
    if not run.plan or not plan_file.exists():
        raise NotFoundError(f"Run {run_id} has no plan yet (state {run.state}).")
    console.print(Markdown(plan_file.read_text(encoding="utf-8")))


@app.command()
@handle_errors
def show(
    run_id: RunIdArg,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show run details: metadata, events, agent executions and artifacts."""
    project = open_project()
    store = project.store
    run = store.get_run(run_id)
    events = store.events(run_id)
    executions = store.executions(run_id)
    artifacts = store.artifacts(run_id)
    if as_json:
        payload = {
            "id": run.id,
            "state": run.state,
            "request": run.request,
            "base_commit": run.base_commit,
            "integration_branch": run.integration_branch,
            "correction_cycle": run.correction_cycle,
            "failure_reason": run.failure_reason,
            "tasks": [
                {"key": t.key, "status": t.status, "commit": t.commit_sha, "files": t.changed_files}
                for t in store.tasks(run_id)
            ],
            "events": [
                {
                    "at": e.at.isoformat(),
                    "type": e.type,
                    "from": e.from_state,
                    "to": e.to_state,
                    "task": e.task_key,
                    "message": e.message,
                }
                for e in events
            ],
            "executions": [
                {
                    "id": e.id,
                    "role": e.role,
                    "status": e.status,
                    "cost_usd": e.cost_usd,
                    "error": e.error,
                }
                for e in executions
            ],
            "artifacts": {a.name: a.path for a in artifacts},
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return
    console.print(f"Run [bold]{run.id}[/]: {_state(run)}")
    console.print(f"Request: {run.request}")
    console.print(f"Base: {run.base_branch or '(detached)'} @ {run.base_commit[:12]}")
    console.print(f"Integration branch: {run.integration_branch}")
    ev = Table(title="Events", show_header=True, header_style="bold")
    for col in ("Time (UTC)", "Type", "Task", "Transition", "Message"):
        ev.add_column(col)
    for e in events:
        transition = f"{e.from_state or ''} → {e.to_state}" if e.to_state else ""
        ev.add_row(f"{e.at:%H:%M:%S}", e.type, e.task_key or "", transition, e.message)
    console.print(ev)
    ex = Table(title="Agent executions", show_header=True, header_style="bold")
    for col in ("Id", "Role", "Status", "Cost $", "Error"):
        ex.add_column(col)
    for e in executions:
        ex.add_row(e.id, e.role, e.status, f"{e.cost_usd or 0:.4f}", (e.error or "")[:80])
    console.print(ex)
    for a in artifacts:
        console.print(f"  artifact {a.name}: {a.path}")


@app.command()
@handle_errors
def report(
    run_id: RunIdArg,
    path_only: Annotated[bool, typer.Option("--path", help="Only print the report path.")] = False,
) -> None:
    """Regenerate and display the Markdown report for a run."""
    project = open_project()
    content = make_orchestrator(project).write_report(run_id)
    path = project.paths.run_dir(run_id) / "final-report.md"
    if path_only:
        console.print(str(path))
        return
    console.print(Markdown(content))
    console.print(f"\n[dim]{path}[/]")


def main() -> None:
    # Unicode arrows/dashes must not crash output on legacy Windows code pages.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")
    install_termination_handlers()
    app()


if __name__ == "__main__":
    main()
