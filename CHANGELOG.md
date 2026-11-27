# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `execution.skip_evaluation_max_lines`: skip the External Evaluator for small changes
  whose verification commands all passed and that have no blocking findings (D26).
- Director `project_notes` (build/test commands, layout, conventions) are passed to every
  Implementer and shown in `plan.md`, so Implementers do not rediscover the project; the
  Director is told about the fixed per-task cost and not to over-split (D26).

### Changed
- `orchestrator/engine.py` (≈1,400 lines) is split into stage modules: `implementation`,
  `integration`, `review`, `permissions` and `support`; behaviour is unchanged.
- Git commands of parallel tasks run off the event loop, so one task's merge or commit no
  longer stalls the other Implementer (D26).

## [0.6.0] - 2026-11-27

### Added
- `execution.serialize_shared_files` (default on): planned tasks whose expected files
  overlap get a dependency on each other, so they never run in parallel on the same file.
  The added ordering is shown in `plan.md` before approval (D25).
- The Director is asked to list shared files (build manifests, routers, changelogs) in
  `expected_files`.

### Fixed
- In a diamond (`c` depends on `a` and `b`), a conflict between `a` and `b` was resolved
  in `c`'s worktree and then again at integration; the branch now arrives through `c`.
- A `=======` Markdown heading underline in a conflicted file was taken for a leftover
  conflict marker and failed the run.
- Files the conflict resolver changed outside the conflict were left uncommitted in the
  worktree; the resolution is now rejected instead.
- Documentation: README and specs said CREW never pushes, although `crew publish --pr`
  pushes the run's integration branch; architecture layout, roadmap status and command
  list caught up with 0.3–0.5.

## [0.5.1] - 2026-11-26

### Added
- `crew run --file request.md`: read the request from a file.
- Build and runtime files (`pom.xml`, Gradle, `requirements.txt`, `Dockerfile`, …) in
  the Director's key-file context, within a 40 kB budget.
- "Giving the Director input" in docs/USAGE.md.

## [0.5.0] - 2026-11-26

Integrations and interactive permissions (M9).

### Added
- Integration adapters: Jira Cloud (issues, comments, transitions), GitHub (PRs, reviews,
  review comments), Confluence Cloud and a Markdown folder (context pages, report pages).
- `crew run --issue KEY`, `--doc REF` (repeatable) and `--pr-comments N`.
- `crew publish <run> [--pr --draft --review --docs --no-issue --yes]` for COMPLETED runs;
  the only push CREW performs, limited to the run's integration branch.
- `approvals.permission_prompts: ask`: agents can ask for permissions outside their
  rules through CREW's MCP approval tool; destructive and network commands are always
  denied; answers are recorded.
- `crew doctor` checks configured integrations and credential variables.
- docs/INTEGRATIONS.md; decisions D23–D24; exit code 13 (integration error).

## [0.4.0] - 2026-11-26

Cost controls and robustness, driven by a measured reference run (Spring Boot CRUD).

### Added
- Token usage, turns and model per agent execution; "Agent usage" table in reports.
- Measured reference example: Spring Boot CRUD service (`docs/examples/`).
- M9 roadmap: permission prompts and integration adapters (Jira, code host, review,
  Confluence).
- Cost profiles (`provider.claude_code.profile: default|balanced|economy`), per-role model
  and `--effort` overrides (`provider.claude_code.roles`), and
  `execution.review_context: summary`. Measured on the reference run: default $3.34,
  balanced $2.87, economy $1.28, all independently verified.
- Merge conflicts are resolved by an Implementer (`execution.conflict_resolution`);
  changes outside a task's planned files are reported.

### Fixed
- Run ids were truncated in `crew history` in narrow terminals.
- Claude usage/rate limits were retried immediately; they now stop the run cleanly and
  `crew retry` continues after the reset.

## [0.3.0] - 2026-11-26

### Added
- `execution.sandbox` (`none` | `bwrap` | `docker`) isolates verification commands, which
  run agent-written code: read-only filesystem except the worktree, hidden credential
  directories, no network by default. Fails closed when unavailable; recorded per command;
  checked by `crew doctor`.
- Windows Job Objects: agent and verification process trees are always stopped together,
  including grandchildren of exited parents, and die with CREW.
- Opt-in real-CLI smoke test for scoped shell rules, and a manually triggered
  "Claude smoke" workflow for Linux, macOS and Windows (needs `ANTHROPIC_API_KEY`).
- Tag-driven release workflow publishing the wheel, sdist and changelog notes.

### Changed
- CI installs bubblewrap so sandbox isolation is tested for real on Linux.

## [0.2.0] - 2026-11-26

Hardening and cross-platform release, driven by an independent review of 0.1.0.

### Added
- Windows and macOS support; CI on Linux, macOS and Windows with Python 3.12/3.13.
- `crew retry <run-id>`: retry a FAILED run from the stage it failed in.
- `crew cleanup <run-id>|--all [--branches]`: remove finished runs' worktrees and merged
  CREW branches.
- `execution.max_run_budget_usd`: per-run spending limit; cost shown in `crew status`.
- Verification commands may be argv lists; scoped `PowerShell(...)` rules.
- `crew.system` platform layer (psutil-based process trees, portable file lock).
- MIT license.

### Changed
- `--restricted` is mandatory and `--strict-mcp-config` is always passed; security flags
  precede free-text arguments.
- Reviewer and Evaluator run in disposable snapshot worktrees.
- Correction tasks are named `crew-fix-<n>`; the `crew-` prefix is reserved in plans.
- Git commands are recorded per run; all git calls run with hooks disabled.

### Fixed
- Secret redaction could corrupt stored JSON and break a run's report.
- A cancel requested during planning fired only after the plan was approved.
- Agent processes could outlive a terminated CREW process; `stop` did not reconcile them.
- Unexpected errors left tasks or executions stuck in RUNNING.
- Non-UTF-8 git output could fail a run; renamed paths were mis-parsed.
- Git write commands were not blocked in the developer's worktree.
- New secret files created by agents could be committed to CREW branches.
- `info/exclude` was written to the wrong place in linked worktrees.
- `execution.preserve_worktrees_on_failure` was ignored.

## [0.1.0] - 2026-11-26 (initial MVP)

### Added
- `crew` CLI: `init`, `doctor`, `configure`, `run`, `plan`, `approve-plan`, `reject-plan`,
  `accept`, `reject`, `status`, `tasks`, `history`, `show`, `resume`, `stop`, `report`.
- Validated run state machine with plan and final approval gates that cannot be disabled.
- Director / Implementer / Reviewer / External Evaluator roles with schema-validated output.
- Dependency-aware scheduler with at most two concurrent Implementers.
- Per-task Git worktrees and merge-based integration into a CREW-owned branch; conflict
  detection; the developer's branch and working tree are never modified.
- CREW-run verification commands recorded as test evidence.
- Bounded review/correction cycles.
- SQLite persistence (WAL, Alembic migrations), append-only workflow events.
- Recovery (`resume`) after interruption, cooperative cancellation, per-run locking.
- Claude Code CLI adapter (restricted, no permission bypass) and an offline fake provider.
- Secret redaction for logs, database and reports; secret-file exclusions for agents.
- Markdown run artifacts and final report.
