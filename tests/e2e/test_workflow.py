"""End-to-end workflow tests with the fake provider in temporary repositories.

These map to the MVP acceptance criteria in docs/PRODUCT_SPEC.md §7.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from crew.domain.enums import ExecutionStatus, Role, RunState, TaskStatus
from crew.domain.errors import InvalidTransitionError, PreconditionError, RunLockedError
from crew.orchestrator.lock import RunLock
from crew.providers.base import AgentRequest
from crew.providers.fake import FakeProvider, default_director, default_implementer
from tests.conftest import git


def plan_with(*tasks: tuple[str, list[str]]):
    def director(_req: AgentRequest) -> dict:
        plan = default_director(_req)
        plan["tasks"] = [
            {
                "id": key,
                "title": f"Task {key}",
                "description": f"Implement {key} carefully",
                "dependencies": deps,
                "acceptance_criteria": [f"{key} done"],
            }
            for key, deps in tasks
        ]
        return plan

    return director


def blocking_review(_req: AgentRequest) -> dict:
    return {
        "assessment": "needs work",
        "findings": [
            {
                "severity": "HIGH",
                "category": "correctness",
                "title": "Missing edge case",
                "file": "crew_fake/part-a.md",
                "line_start": 1,
                "explanation": "edge case not handled",
                "recommendation": "handle it",
                "blocking": True,
            }
        ],
    }


# ---- AC 3, 6, 13: full run, gates, no automatic merge -----------------------


def test_full_run_reaches_completion_without_touching_user_branch(make_orch, fake, repo: Path):
    head_before = git(repo, "rev-parse", "HEAD").strip()
    orch = make_orch()
    run = orch.start_run("Add a feature")
    assert run.state == RunState.AWAITING_PLAN_APPROVAL
    assert not [c for c in fake.calls if c.role is Role.IMPLEMENTER]

    run = orch.approve_plan(run.id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL
    run = orch.accept(run.id)
    assert run.state == RunState.COMPLETED

    # The user's branch, HEAD and working tree are untouched (AC 13).
    assert git(repo, "rev-parse", "HEAD").strip() == head_before
    assert git(repo, "status", "--porcelain").strip() == ""
    assert not (repo / "crew_fake").exists()
    # The result lives on the CREW integration branch.
    files = git(repo, "ls-tree", "-r", "--name-only", run.integration_branch).split()
    assert {"crew_fake/part-a.md", "crew_fake/part-b.md", "crew_fake/part-c.md"} <= set(files)
    # Report and stage artifacts exist (AC 14).
    run_dir = orch.p.paths.run_dir(run.id)
    for name in (
        "request.md",
        "plan.md",
        "implementation.md",
        "review.md",
        "external-evaluation.md",
        "test-results.md",
        "final-report.md",
    ):
        assert (run_dir / name).is_file(), name
    report = (run_dir / "final-report.md").read_text()
    assert "COMPLETED" in report and "git merge --no-ff" in report


def test_every_agent_runs_in_a_crew_owned_worktree(make_orch, fake, repo: Path):
    orch = make_orch()
    run = orch.approve_plan(orch.start_run("x").id)
    worktrees = (repo / ".crew" / "worktrees").resolve()
    for call in fake.calls:
        assert worktrees in call.cwd.resolve().parents, call.cwd
    assert run.state == RunState.AWAITING_FINAL_APPROVAL


def test_plan_rejection_never_implements(make_orch, fake):
    orch = make_orch()
    run = orch.reject_plan(orch.start_run("x").id, "not what I want")
    assert run.state == RunState.PLAN_REJECTED
    assert not [c for c in fake.calls if c.role is Role.IMPLEMENTER]
    with pytest.raises(InvalidTransitionError):
        orch.approve_plan(run.id)


def test_final_rejection(make_orch):
    orch = make_orch()
    run = orch.approve_plan(orch.start_run("x").id)
    assert orch.reject(run.id, "no").state == RunState.REJECTED


def test_approval_refused_on_dirty_tree(make_orch, repo: Path):
    orch = make_orch()
    run = orch.start_run("x")
    (repo / "app.py").write_text("my uncommitted work\n")
    with pytest.raises(PreconditionError):
        orch.approve_plan(run.id)
    assert orch.store.get_run(run.id).state == RunState.AWAITING_PLAN_APPROVAL
    assert (repo / "app.py").read_text() == "my uncommitted work\n"


def test_final_completion_requires_explicit_accept(make_orch):
    orch = make_orch()
    run = orch.approve_plan(orch.start_run("x").id)
    assert orch.drive(run.id).state == RunState.AWAITING_FINAL_APPROVAL  # driving does not pass
    assert orch.resume(run.id).state == RunState.AWAITING_FINAL_APPROVAL  # the gate on its own
    with pytest.raises(InvalidTransitionError):
        orch.approve_plan(run.id)  # wrong gate
    assert orch.store.get_run(run.id).state == RunState.AWAITING_FINAL_APPROVAL


# ---- AC 7, 8: concurrency limit and dependencies ----------------------------


def test_at_most_two_implementers_run_concurrently(make_orch):
    fake = FakeProvider(delay_seconds=0.3)
    fake.on(Role.DIRECTOR, plan_with(("a", []), ("b", []), ("c", [])))
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL
    assert fake.max_active[Role.IMPLEMENTER] == 2


def test_configured_limit_of_one_serialises(make_orch, configure_project):
    project = configure_project(execution={"max_implementers": 1})
    fake = FakeProvider(delay_seconds=0.1)
    fake.on(Role.DIRECTOR, plan_with(("a", []), ("b", [])))
    orch = make_orch(project, fake)
    orch.approve_plan(orch.start_run("x").id)
    assert fake.max_active[Role.IMPLEMENTER] == 1


def test_dependencies_are_respected(make_orch):
    timeline: list[tuple[str, str]] = []

    async def implementer(req: AgentRequest) -> dict:
        timeline.append(("start", req.task_key))
        await asyncio.sleep(0.2)
        result = default_implementer(req)
        timeline.append(("end", req.task_key))
        return result

    fake = FakeProvider()
    fake.on(Role.DIRECTOR, plan_with(("base", []), ("child", ["base"]), ("other", [])))
    fake.on(Role.IMPLEMENTER, implementer)
    orch = make_orch(provider=fake)
    orch.approve_plan(orch.start_run("x").id)
    assert timeline.index(("end", "base")) < timeline.index(("start", "child"))
    # 'other' has no dependency, so it runs alongside 'base'.
    assert timeline.index(("start", "other")) < timeline.index(("end", "base"))


# ---- AC 5: director output validation ---------------------------------------


def test_invalid_director_graph_is_retried_then_fails(make_orch):
    fake = FakeProvider()
    fake.on(Role.DIRECTOR, plan_with(("a", ["b"]), ("b", ["a"])))
    orch = make_orch(provider=fake)
    run = orch.start_run("x")
    assert run.state == RunState.FAILED
    assert "cycle" in run.failure_reason
    assert len([c for c in fake.calls if c.role is Role.DIRECTOR]) == 2


def test_director_is_corrected_after_feedback(make_orch):
    calls = {"n": 0}

    def director(req: AgentRequest) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return plan_with(("Bad Id", []))(req)
        assert "rejected by CREW validation" in req.prompt
        return plan_with(("good", []))(req)

    orch = make_orch(provider=FakeProvider().on(Role.DIRECTOR, director))
    assert orch.start_run("x").state == RunState.AWAITING_PLAN_APPROVAL


def test_malformed_director_output_fails_run(make_orch):
    orch = make_orch(provider=FakeProvider().on(Role.DIRECTOR, lambda _r: {"nonsense": True}))
    run = orch.start_run("x")
    assert run.state == RunState.FAILED
    assert "schema validation" in run.failure_reason
    executions = orch.store.executions(run.id, role=Role.DIRECTOR)
    assert executions[0].status == ExecutionStatus.FAILED


# ---- AC 9, 10: review corrections and independent evaluation ---------------


def test_blocking_review_triggers_one_correction_cycle(make_orch):
    reviews = iter([blocking_review, lambda _r: {"assessment": "fixed now", "findings": []}])
    fake = FakeProvider().on(Role.REVIEWER, lambda r: next(reviews)(r))
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL
    assert run.correction_cycle == 1
    fix = orch.store.get_task(run.id, "crew-fix-1")
    assert fix.status == TaskStatus.COMPLETED and fix.integrated
    implementer_prompts = [c.prompt for c in fake.calls if c.task_key == "crew-fix-1"]
    assert "Missing edge case" in implementer_prompts[0]
    # Second review saw the first review's findings.
    reviewer_prompts = [c.prompt for c in fake.calls if c.role is Role.REVIEWER]
    assert "Previous review cycle" in reviewer_prompts[1]


def test_correction_cycles_are_bounded(make_orch, configure_project):
    project = configure_project(execution={"max_correction_cycles": 1})
    fake = FakeProvider().on(Role.REVIEWER, blocking_review)
    orch = make_orch(project, fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL  # developer decides
    assert run.correction_cycle == 1
    assert len([c for c in fake.calls if c.role is Role.REVIEWER]) == 2
    events = [e.type for e in orch.store.events(run.id)]
    assert "correction_limit_reached" in events
    report = (orch.p.paths.run_dir(run.id) / "final-report.md").read_text()
    assert "Unresolved blocking findings" in report


def test_evaluator_receives_request_plan_diff_and_labelled_claims(make_orch, fake):
    orch = make_orch()
    orch.approve_plan(orch.start_run("Add the frobnicator").id)
    prompt = next(c.prompt for c in fake.calls if c.role is Role.EVALUATOR)
    assert "Add the frobnicator" in prompt
    assert "Approved plan" in prompt
    assert "+# part-a" in prompt  # actual diff content, not just summaries
    assert "CLAIMS" in prompt


def test_verification_commands_are_recorded_as_evidence(make_orch, configure_project):
    listing = [sys.executable, "-c", "import os; print(sorted(os.listdir('crew_fake')))"]
    project = configure_project(execution={"verification_commands": [listing]})
    fake = FakeProvider()
    orch = make_orch(project, fake)
    run = orch.approve_plan(orch.start_run("x").id)
    commands = orch.store.commands(run.id, purpose="verification")
    assert len(commands) == 1 and commands[0].exit_code == 0
    assert "part-a.md" in commands[0].stdout
    reviewer_prompt = next(c.prompt for c in fake.calls if c.role is Role.REVIEWER)
    assert "— exit 0" in reviewer_prompt and "part-a.md" in reviewer_prompt


# ---- failures, conflicts (AC 12) --------------------------------------------


def test_failing_task_is_retried_then_blocks_dependents(make_orch):
    def implementer(req: AgentRequest) -> dict:
        if req.task_key == "a":
            return {"summary": ""}  # invalid: summary too short
        return default_implementer(req)

    fake = FakeProvider()
    fake.on(Role.DIRECTOR, plan_with(("a", []), ("b", ["a"]), ("c", [])))
    fake.on(Role.IMPLEMENTER, implementer)
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    statuses = {t.key: t.status for t in orch.store.tasks(run.id)}
    assert statuses == {"a": "FAILED", "b": "BLOCKED", "c": "COMPLETED"}
    assert orch.store.get_task(run.id, "a").attempt == 2  # retried once


def test_merge_conflict_stops_safely_and_preserves_work(make_orch, configure_project, repo: Path):
    project = configure_project(execution={"conflict_resolution": "fail"})

    def implementer(req: AgentRequest) -> dict:
        (req.cwd / "app.py").write_text(f"conflicting change from {req.task_key}\n")
        return {"summary": "edited app.py", "changed_files": ["app.py"]}

    fake = FakeProvider()
    fake.on(Role.DIRECTOR, plan_with(("a", []), ("b", [])))
    fake.on(Role.IMPLEMENTER, implementer)
    orch = make_orch(project, fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    assert "conflicted in: app.py" in run.failure_reason
    assert (repo / "app.py").read_text() == "def hello():\n    return 'hi'\n"
    for key in ("a", "b"):
        task = orch.store.get_task(run.id, key)
        assert Path(task.worktree_path).exists()  # preserved for inspection
    integration = orch.p.workspace.integration_path(run.id)
    assert git(integration, "status", "--porcelain").strip() == ""  # merge was aborted


# ---- AC 11: interruption, recovery, cancellation, locking --------------------


def test_interrupted_run_resumes_with_fresh_attempt(make_orch, fake):
    orch = make_orch()
    run = orch.approve_plan(orch.start_run("x").id, proceed=False)
    assert run.state == RunState.IMPLEMENTING

    # Simulate a crash mid-task: task claimed, worktree created, process gone.
    task = orch.store.get_task(run.id, "part-a")
    claimed = orch.store.claim_task(task, "crashed-exec")
    orch.store.start_execution(
        run_id=run.id,
        role=Role.IMPLEMENTER,
        provider="fake",
        cwd="/nowhere",
        prompt="",
        cycle=0,
        task=claimed,
        execution_id="crashed-exec",
    )
    ws = orch.p.workspace
    ws.ensure_integration_worktree(run.id, run.base_commit)
    stale, _ = ws.create_task_worktree(run.id, "part-a", 1, ws.integration_branch(run.id))
    (stale / "partial.txt").write_text("half-done work")

    run = make_orch().resume(run.id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL
    task = orch.store.get_task(run.id, "part-a")
    assert task.status == TaskStatus.COMPLETED and task.attempt == 2
    assert (stale / "partial.txt").exists()  # old attempt preserved, not reused
    crashed = next(e for e in orch.store.executions(run.id) if e.id == "crashed-exec")
    assert crashed.status == ExecutionStatus.INTERRUPTED
    files = git(orch.p.paths.root, "ls-tree", "-r", "--name-only", run.integration_branch)
    assert "partial.txt" not in files


def test_resume_of_terminal_run_is_refused(make_orch):
    orch = make_orch()
    run = orch.reject_plan(orch.start_run("x").id)
    with pytest.raises(InvalidTransitionError):
        orch.resume(run.id)


def test_stop_without_active_driver_cancels_immediately(make_orch):
    orch = make_orch()
    run = orch.start_run("x")
    run, immediate = orch.stop(run.id)
    assert immediate and run.state == RunState.CANCELLED


def test_cooperative_cancellation_during_implementation(make_orch):
    holder = {}

    async def implementer(req: AgentRequest) -> dict:
        holder["orch"].store.request_cancel(req.run_id)
        await asyncio.sleep(5)  # would be killed by cancellation
        return default_implementer(req)

    fake = FakeProvider().on(Role.IMPLEMENTER, implementer)
    orch = make_orch(provider=fake)
    holder["orch"] = orch
    started = time.monotonic()
    run = orch.approve_plan(orch.start_run("x").id)
    assert time.monotonic() - started < 4
    assert run.state == RunState.CANCELLED
    statuses = {t.status for t in orch.store.tasks(run.id)}
    assert TaskStatus.RUNNING not in statuses
    assert TaskStatus.CANCELLED in statuses


def test_second_driver_is_locked_out(make_orch):
    orch = make_orch()
    run = orch.start_run("x")
    lock = RunLock(orch.p.paths.run_dir(run.id) / ".lock")
    assert lock.try_acquire()
    try:
        with pytest.raises(RunLockedError):
            orch.approve_plan(run.id)
        _, immediate = orch.stop(run.id)
        assert not immediate  # cancellation requested from the active driver instead
        assert orch.store.get_run(run.id).cancel_requested
    finally:
        lock.release()


def test_cancel_requested_during_planning_does_not_survive_the_gate(make_orch):
    holder = {}

    def director(req: AgentRequest) -> dict:
        holder["orch"].store.request_cancel(req.run_id)  # `crew stop` while planning
        return default_director(req)

    orch = make_orch(provider=FakeProvider().on(Role.DIRECTOR, director))
    holder["orch"] = orch
    run = orch.start_run("x")
    assert run.state == RunState.CANCELLED  # honoured now, not after a later approval


def test_cancellation_interrupts_a_running_reviewer(make_orch):
    holder = {}

    async def reviewer(req: AgentRequest) -> dict:
        holder["orch"].store.request_cancel(req.run_id)
        await asyncio.sleep(10)
        return {"assessment": "never returned", "findings": []}

    orch = make_orch(provider=FakeProvider().on(Role.REVIEWER, reviewer))
    holder["orch"] = orch
    started = time.monotonic()
    run = orch.approve_plan(orch.start_run("x").id)
    assert time.monotonic() - started < 6
    assert run.state == RunState.CANCELLED
    reviewer_exec = orch.store.executions(run.id, role=Role.REVIEWER)[0]
    assert reviewer_exec.status == ExecutionStatus.CANCELLED


def test_stop_after_driver_died_reconciles_leftovers(make_orch):
    orch = make_orch()
    run = orch.approve_plan(orch.start_run("x").id, proceed=False)
    task = orch.store.get_task(run.id, "part-a")
    claimed = orch.store.claim_task(task, "dead-exec")
    orch.store.start_execution(
        run_id=run.id,
        role=Role.IMPLEMENTER,
        provider="fake",
        cwd="/nowhere",
        prompt="",
        cycle=0,
        task=claimed,
        execution_id="dead-exec",
    )
    run, immediate = orch.stop(run.id)
    assert immediate and run.state == RunState.CANCELLED
    dead = next(e for e in orch.store.executions(run.id) if e.id == "dead-exec")
    assert dead.status == ExecutionStatus.INTERRUPTED
    assert orch.store.get_task(run.id, "part-a").status == TaskStatus.CANCELLED


def test_unexpected_error_in_one_task_fails_it_cleanly(make_orch):
    def implementer(req: AgentRequest) -> dict:
        if req.task_key == "a":
            raise OSError("disk on fire")
        return default_implementer(req)

    fake = FakeProvider()
    fake.on(Role.DIRECTOR, plan_with(("a", []), ("b", [])))
    fake.on(Role.IMPLEMENTER, implementer)
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    statuses = {t.key: t.status for t in orch.store.tasks(run.id)}
    assert statuses["b"] == TaskStatus.COMPLETED
    assert statuses["a"] == TaskStatus.FAILED  # provider crash -> normal retry -> FAILED
    executions = orch.store.executions(run.id, role=Role.IMPLEMENTER)
    assert all(e.status != ExecutionStatus.RUNNING for e in executions)


def test_git_commands_are_recorded_per_run(make_orch):
    orch = make_orch()
    run = orch.approve_plan(orch.start_run("x").id)
    git_cmds = orch.store.commands(run.id, purpose="git")
    assert git_cmds
    assert any('"merge"' in c.argv_json for c in git_cmds)


def test_reviewer_and_evaluator_cannot_dirty_the_integration_worktree(make_orch):
    def messy_reviewer(req: AgentRequest) -> dict:
        (req.cwd / "review-cache.txt").write_text("tool output")  # e.g. a test cache
        return {"assessment": "looks fine", "findings": []}

    fake = FakeProvider().on(Role.REVIEWER, messy_reviewer)
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL
    integration = orch.p.workspace.integration_path(run.id)
    assert not (integration / "review-cache.txt").exists()
    reviewer_cwd = next(c.cwd for c in fake.calls if c.role is Role.REVIEWER)
    assert reviewer_cwd != integration


def test_worktrees_removed_on_failure_when_not_preserving(make_orch, configure_project):
    project = configure_project(execution={"preserve_worktrees_on_failure": False})
    fake = FakeProvider().on(Role.IMPLEMENTER, lambda _r: {"summary": ""})  # always invalid
    orch = make_orch(project, fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    assert not orch.p.workspace.run_dir(run.id).exists()  # all were clean, so all removed


def test_run_budget_stops_spending(make_orch, configure_project):
    project = configure_project(execution={"max_run_budget_usd": 0.5})
    fake = FakeProvider(cost_per_call=0.3)
    orch = make_orch(project, fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    assert "budget exhausted" in run.failure_reason
    # Director (0.3) + one implementer (0.3) exhaust the budget; nothing else ran.
    assert [c.role for c in fake.calls] == [Role.DIRECTOR, Role.IMPLEMENTER]
    cap = fake.calls[1].max_budget_usd
    assert cap is not None and cap <= 0.1 + 1e-9  # (0.5 - 0.3) split over 2 implementers
    assert orch.store.get_task(run.id, "part-b").attempt <= 1  # budget errors are not retried


def test_unlimited_budget_by_default(make_orch):
    fake = FakeProvider(cost_per_call=5.0)
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL
    assert orch.store.run_cost(run.id) == pytest.approx(5.0 * len(fake.calls))


def test_failed_run_can_be_retried_after_fixing_the_cause(make_orch):
    broken = {"on": True}

    def implementer(req: AgentRequest) -> dict:
        if broken["on"] and req.task_key == "part-b":
            return {"summary": ""}  # invalid output: a "provider problem"
        return default_implementer(req)

    fake = FakeProvider().on(Role.IMPLEMENTER, implementer)
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED and run.failed_state == RunState.IMPLEMENTING

    broken["on"] = False
    run = orch.retry(run.id, "provider fixed")
    assert run.state == RunState.AWAITING_FINAL_APPROVAL
    part_b = orch.store.get_task(run.id, "part-b")
    assert part_b.status == TaskStatus.COMPLETED and part_b.attempt == 3  # fresh attempt
    assert orch.store.get_task(run.id, "part-a").attempt == 1  # completed work kept
    kinds = [a.kind for a in orch.store.approvals(run.id)]
    assert kinds == ["PLAN", "RETRY"]


def test_retry_after_budget_increase(make_orch, configure_project):
    project = configure_project(execution={"max_run_budget_usd": 0.5})
    fake = FakeProvider(cost_per_call=0.3)
    run = make_orch(project, fake).approve_plan(make_orch(project, fake).start_run("x").id)
    assert run.state == RunState.FAILED
    bigger = configure_project(execution={"max_run_budget_usd": 100.0})
    run = make_orch(bigger, fake).retry(run.id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL


def test_only_failed_runs_can_be_retried(make_orch):
    orch = make_orch()
    run = orch.start_run("x")
    with pytest.raises(InvalidTransitionError):
        orch.retry(run.id)


def test_cleanup_removes_worktrees_and_merged_branches_only(make_orch, repo: Path):
    orch = make_orch()
    run = orch.accept(orch.approve_plan(orch.start_run("x").id).id)
    report = orch.cleanup(run.id, branches=True)
    branches = git(repo, "branch", "--list", f"crew/{run.id}/*").split()
    # Task branches are inside the integration branch: deleted.
    # The integration branch is not merged into main yet: kept.
    assert branches == [run.integration_branch]
    assert any("not merged" in b for b in report.kept_branches)

    git(repo, "merge", "--no-ff", "-q", "-m", "take result", run.integration_branch)
    orch.cleanup(run.id, branches=True)
    assert git(repo, "branch", "--list", f"crew/{run.id}/*").strip() == ""
    assert not orch.p.workspace.run_dir(run.id).exists()


def test_cleanup_keeps_dirty_worktrees_and_failed_run_branches(make_orch):
    fake = FakeProvider().on(Role.IMPLEMENTER, lambda _r: {"summary": ""})
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    dirty = Path(orch.store.get_task(run.id, "part-a").worktree_path)
    (dirty / "wip.txt").write_text("keep me")
    report = orch.cleanup(run.id, branches=True)
    assert str(dirty) in report.kept_worktrees and (dirty / "wip.txt").exists()
    assert not report.deleted_branches


def test_cleanup_refuses_active_runs(make_orch):
    orch = make_orch()
    run = orch.start_run("x")
    with pytest.raises(InvalidTransitionError):
        orch.cleanup(run.id)


@pytest.mark.skipif(
    __import__("crew.security.sandbox", fromlist=["x"]).check_available(
        __import__("crew.security.sandbox", fromlist=["x"]).SandboxSettings(mode="bwrap")
    )
    is not None,
    reason="bubblewrap not usable here",
)
def test_verification_runs_inside_the_sandbox(make_orch, configure_project):
    probe = [
        sys.executable,
        "-c",
        "import socket\n"
        "try:\n socket.create_connection(('1.1.1.1', 53), 2)\n"
        "except OSError:\n print('offline')",
    ]
    project = configure_project(execution={"verification_commands": [probe], "sandbox": "bwrap"})
    orch = make_orch(project, FakeProvider())
    run = orch.approve_plan(orch.start_run("x").id)
    command = orch.store.commands(run.id, purpose="verification")[0]
    assert command.sandbox == "bwrap" and command.exit_code == 0
    assert "offline" in command.stdout


def test_unavailable_sandbox_fails_closed(make_orch, configure_project, monkeypatch):
    monkeypatch.setattr("crew.orchestrator.engine.check_sandbox", lambda _s: "not installed")
    project = configure_project(
        execution={"verification_commands": [[sys.executable, "-V"]], "sandbox": "bwrap"}
    )
    orch = make_orch(project, FakeProvider())
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    assert "sandbox 'bwrap' is unavailable" in run.failure_reason
    assert orch.store.commands(run.id, purpose="verification") == []  # nothing ran unsandboxed


def test_report_contains_usage_by_role(make_orch):
    fake = FakeProvider(cost_per_call=0.25)
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    report = (orch.p.paths.run_dir(run.id) / "final-report.md").read_text()
    assert "## Agent usage" in report
    assert "| implementer | 3 | 0.7500 |" in report
    assert "**1.5000**" in report  # 6 executions x 0.25


def test_summary_review_context_keeps_prompts_small(make_orch, configure_project):
    def big_implementer(req: AgentRequest) -> dict:
        (req.cwd / "big.txt").write_text("line of generated content\n" * 5000)  # ~130 kB
        return default_implementer(req)

    fake = FakeProvider().on(Role.IMPLEMENTER, big_implementer)
    project = configure_project(execution={"review_context": "summary"})
    orch = make_orch(project, fake)
    orch.approve_plan(orch.start_run("x").id)
    reviewer = next(c.prompt for c in fake.calls if c.role is Role.REVIEWER)
    evaluator = next(c.prompt for c in fake.calls if c.role is Role.EVALUATOR)
    for prompt in (reviewer, evaluator):
        assert "## Changed files" in prompt and "- big.txt" in prompt
        assert "open the changed files with your Read tool" in prompt
        assert len(prompt) < 30_000  # the full diff alone would be > 120 kB


def _same_change_with_different_comments(req: AgentRequest) -> dict:
    """Parallel tasks making the same edit differently: the reference-run failure."""
    if "Resolve a merge conflict" in req.prompt:
        text = (req.cwd / "app.py").read_text()
        resolved = [ln for ln in text.splitlines() if not ln.startswith(("<<<<<<<", ">>>>>>>"))]
        resolved = [ln for ln in resolved if ln != "=======" and "# from b" not in ln]
        (req.cwd / "app.py").write_text("\n".join(resolved) + "\n")
        return {"summary": "kept the plugin once"}
    if req.task_key in ("a", "b"):
        with (req.cwd / "app.py").open("a") as fh:
            fh.write(f"# from {req.task_key}\nPLUGIN = 'failsafe'\n")
    return default_implementer(req)


def test_dependency_conflict_is_resolved_by_an_agent(make_orch):
    fake = FakeProvider()
    fake.on(Role.DIRECTOR, plan_with(("a", []), ("b", []), ("c", ["a", "b"])))
    fake.on(Role.IMPLEMENTER, _same_change_with_different_comments)
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL, run.failure_reason
    events = [e.type for e in orch.store.events(run.id)]
    assert "merge_conflict" in events and "merge_conflict_resolved" in events
    content = git(orch.p.paths.root, "show", f"{run.integration_branch}:app.py")
    assert content.count("PLUGIN = 'failsafe'") == 1 and "<<<<<<<" not in content


def test_unresolved_conflict_fails_without_retry(make_orch):
    def stubborn(req: AgentRequest) -> dict:
        if "Resolve a merge conflict" in req.prompt:
            return {"summary": "did nothing"}  # markers remain
        return _same_change_with_different_comments(req)

    fake = FakeProvider()
    fake.on(Role.DIRECTOR, plan_with(("a", []), ("b", []), ("c", ["a", "b"])))
    fake.on(Role.IMPLEMENTER, stubborn)
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    assert "conflict markers remain in: app.py" in run.failure_reason
    assert orch.store.get_task(run.id, "c").attempt == 1  # deterministic: not retried


def test_changes_outside_the_plan_are_reported(make_orch):
    def implementer(req: AgentRequest) -> dict:
        (req.cwd / "pom.xml").write_text(f"<project>{req.task_key}</project>")
        return default_implementer(req)

    fake = FakeProvider().on(Role.IMPLEMENTER, implementer)
    fake.on(
        Role.DIRECTOR, lambda r: {**default_director(r), "tasks": default_director(r)["tasks"][:1]}
    )
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    expanded = [e for e in orch.store.events(run.id) if e.type == "scope_expanded"]
    assert expanded and "pom.xml" in expanded[0].message
    assert (
        "Outside the plan: pom.xml"
        in (orch.p.paths.run_dir(run.id) / "implementation.md").read_text()
    )


def test_usage_limit_stops_the_run_without_retries_and_is_resumable(make_orch):
    from crew.domain.errors import ProviderError

    limited = {"on": True}

    def implementer(req: AgentRequest) -> dict:
        if limited["on"] and req.task_key == "part-b":
            raise ProviderError("Claude usage limit reached: resets 5pm", kind="rate_limited")
        return default_implementer(req)

    fake = FakeProvider().on(Role.IMPLEMENTER, implementer)
    fake.on(Role.DIRECTOR, plan_with(("part-a", []), ("part-b", []), ("part-c", ["part-b"])))
    orch = make_orch(provider=fake)
    run = orch.approve_plan(orch.start_run("x").id)
    assert run.state == RunState.FAILED
    assert "usage limit" in run.failure_reason and "crew retry" in run.failure_reason
    assert orch.store.get_task(run.id, "part-b").attempt == 1  # not retried into the limit

    limited["on"] = False  # the limit reset
    run = orch.retry(run.id)
    assert run.state == RunState.AWAITING_FINAL_APPROVAL
