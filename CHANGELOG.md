# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-26

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
