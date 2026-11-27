"""REVIEWING, CHANGES_REQUESTED and EXTERNAL_EVALUATION.

A mixin of :class:`crew.orchestrator.engine.Orchestrator`; split out of engine.py.
"""

from __future__ import annotations

from typing import Any

from crew.agents import payloads
from crew.agents.contracts import EvaluationReport, ReviewReport
from crew.agents.runtime import AgentRuntime
from crew.config.schema import CrewConfig
from crew.domain.enums import (
    ExecutionStatus,
    Role,
    RunState,
    TaskKind,
    TaskStatus,
)
from crew.domain.task_graph import RESERVED_PREFIX
from crew.orchestrator.support import (
    Notify,
    Project,
    expect_type,
)
from crew.persistence.models import JsonMixin, Run, Task
from crew.persistence.store import Store
from crew.reporting import renderer


class ReviewStage:
    # Provided by Orchestrator (engine.py).
    p: Project
    store: Store
    config: CrewConfig
    runtime: AgentRuntime
    notify: Notify

    def _integrated_diff(self, run: Run) -> str:
        """The integrated change as markdown, in the configured review_context mode."""
        git, branch = self.p.git, self.p.workspace.integration_branch(run.id)
        patch = git.diff(run.base_commit, branch)
        mode = self.config.execution.review_context
        if mode == "full":
            return payloads.diff_section(run.base_commit, patch, "", [], mode="full")
        stat = git.run("diff", "--stat", f"{run.base_commit}...{branch}")
        files = git.changed_files(run.base_commit, branch)
        return payloads.diff_section(run.base_commit, patch, stat, files, mode="summary")

    def _cycle_commands(self, run: Run) -> list:
        """Latest result per verification command in the current cycle.

        A stage re-run after a crash records commands again; only the newest
        result for each command is evidence.
        """
        latest: dict[str, Any] = {}
        for c in self.store.commands(run.id, purpose="verification"):
            if c.cycle == run.correction_cycle:
                latest[c.argv_json] = c
        return list(latest.values())

    def _snapshot(self, run: Run, purpose: str) -> Any:
        tip = self.p.git.rev_parse(self.p.workspace.integration_branch(run.id))
        return self.p.workspace.ensure_snapshot_worktree(run.id, purpose, tip)

    def _review(self, run: Run) -> None:
        self.notify("Reviewer is inspecting the integrated diff…")
        previous = (
            self.store.latest_output(run.id, Role.REVIEWER, cycle=run.correction_cycle - 1)
            if run.correction_cycle
            else None
        )
        review = self._invoke(
            Role.REVIEWER,
            run,
            payloads.reviewer_prompt(
                run,
                self.store.tasks(run.id),
                self._integrated_diff(run),
                self._cycle_commands(run),
                previous,
            ),
            self._snapshot(run, "review"),
        )
        review = expect_type(review, ReviewReport)
        self._write_review_artifact(run)
        blocking = review.blocking_findings
        if not blocking:
            self._transition(run.id, RunState.EXTERNAL_EVALUATION, "no blocking findings")
        elif run.correction_cycle < self.config.execution.max_correction_cycles:
            self._transition(
                run.id, RunState.CHANGES_REQUESTED, f"{len(blocking)} blocking finding(s)"
            )
        else:
            msg = (
                f"correction cycle limit ({self.config.execution.max_correction_cycles}) reached "
                f"with {len(blocking)} unresolved blocking finding(s); developer decision required"
            )
            self.store.add_event(run.id, "correction_limit_reached", msg)
            self.notify(f"[warning] {msg}")
            self._transition(run.id, RunState.EXTERNAL_EVALUATION, msg)

    def _request_changes(self, run: Run) -> None:
        review = self.store.latest_output(run.id, Role.REVIEWER, cycle=run.correction_cycle) or {}
        blocking = [f for f in review.get("findings", []) if f.get("blocking")]
        cycle = run.correction_cycle + 1
        task = Task(
            key=f"{RESERVED_PREFIX}fix-{cycle}",
            kind=TaskKind.CORRECTION,
            cycle=cycle,
            seq=0,
            title=f"Address blocking review findings (cycle {cycle})",
            description=payloads.correction_description(blocking),
            rationale="Reviewer reported blocking findings",
            dependencies_json="[]",
            acceptance_json=JsonMixin.dumps([f"Resolved: {f['title']}" for f in blocking]),
            expected_files_json=JsonMixin.dumps(
                sorted({f["file"] for f in blocking if f.get("file")})
            ),
            status=TaskStatus.PENDING,
        )
        self._transition(
            run.id,
            RunState.IMPLEMENTING,
            f"correction cycle {cycle} with {len(blocking)} finding(s)",
            new_tasks=[task],
            correction_cycle=cycle,
        )

    def _evaluation_skip_reason(self, run: Run) -> str | None:
        limit = self.config.execution.skip_evaluation_max_lines
        if limit is None:
            return None
        commands = self._cycle_commands(run)
        if not commands or any(c.exit_code != 0 or c.timed_out for c in commands):
            return None
        if any(e.type == "correction_limit_reached" for e in self.store.events(run.id)):
            return None
        lines = self.p.git.changed_lines(
            run.base_commit, self.p.workspace.integration_branch(run.id)
        )
        if lines > limit:
            return None
        return (
            f"{lines} changed line(s) (limit {limit}), {len(commands)} verification "
            "command(s) passed, no blocking review findings"
        )

    def _evaluate(self, run: Run) -> None:
        if reason := self._evaluation_skip_reason(run):
            self.store.add_event(run.id, "evaluation_skipped", reason)
            renderer.write_artifact(
                self.store,
                self._run_dir(run),
                run.id,
                "external-evaluation.md",
                "# External evaluation\n\n_Skipped (`execution.skip_evaluation_max_lines`): "
                f"{reason}._\n",
            )
            self.notify(f"External evaluation skipped: {reason}")
            self._transition(run.id, RunState.AWAITING_FINAL_APPROVAL, "evaluation skipped")
            return
        self.notify("External Evaluator is assessing the result…")
        reviews = [
            e.output
            for e in self.store.executions(
                run.id, role=Role.REVIEWER, status=ExecutionStatus.SUCCEEDED
            )
            if e.output
        ]
        evaluation = self._invoke(
            Role.EVALUATOR,
            run,
            payloads.evaluator_prompt(
                run,
                self.store.tasks(run.id),
                self._integrated_diff(run),
                self._cycle_commands(run),
                reviews,
            ),
            self._snapshot(run, "evaluate"),
        )
        evaluation = expect_type(evaluation, EvaluationReport)
        renderer.write_artifact(
            self.store,
            self._run_dir(run),
            run.id,
            "external-evaluation.md",
            renderer.render_evaluation(evaluation.model_dump(mode="json")),
        )
        self._transition(
            run.id,
            RunState.AWAITING_FINAL_APPROVAL,
            f"evaluator recommends {evaluation.recommendation}",
        )

    def _write_review_artifact(self, run: Run) -> None:
        reviews = [
            (e.cycle, e.output)
            for e in self.store.executions(
                run.id, role=Role.REVIEWER, status=ExecutionStatus.SUCCEEDED
            )
            if e.output
        ]
        renderer.write_artifact(
            self.store, self._run_dir(run), run.id, "review.md", renderer.render_review(reviews)
        )
