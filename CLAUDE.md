# Instructions for Claude Code — CREW

You are working on **CREW**, a Python local-first AI development orchestration CLI. Specifications live in `docs/`: read `docs/PRODUCT_SPEC.md`, `docs/ARCHITECTURE.md`, `docs/WORKFLOW.md`, `docs/AGENT_ROLES.md`, `docs/TECHNICAL_SPEC.md`, `docs/SECURITY.md` and `docs/DECISIONS.md` before making architectural changes.

## Current state

M0–M9 in `docs/ROADMAP.md` are implemented (MVP, hardening, sandboxing, cost controls, integrations); see `CHANGELOG.md` for the current version. Commands:

```bash
uv sync --dev
uv run pytest                     # offline suite; must stay green
uv run ruff check . && uv run ruff format --check .
CREW_CLAUDE_SMOKE=1 uv run pytest -m claude -s   # opt-in, real Claude CLI
```

Code map: `docs/ARCHITECTURE.md#implemented-layout`. Record new non-obvious decisions in `docs/DECISIONS.md`. Schema changes need an Alembic revision (see `CONTRIBUTING.md`).

## Operating rules

1. Work milestone by milestone according to `docs/ROADMAP.md`.
2. First inspect the repository, current Git status, Python versions, and installed tooling. Do not assume the environment matches the documentation.
3. Before coding, present a short implementation plan for the current milestone and identify files to create/change.
4. Do not implement the entire product in one giant change. Build a runnable vertical slice.
5. Do not silently change product requirements or remove safety gates.
6. Keep Python code direct and maintainable. Avoid speculative abstractions, needless design patterns, excessive comments, and tests that only duplicate implementation details.
7. Use type hints where they improve clarity. Validate external/model data at boundaries.
8. Keep domain/workflow logic independent of Typer, Rich, and Claude-specific types.
9. Use the fake provider for normal tests. Never require API credentials for the default test suite.
10. Verify exact Claude Code CLI flags and output formats from the installed version's help/documentation; do not invent flags.
11. Do not use a blanket permission-bypass mode.
12. Never push, deploy, release, or merge into the developer's active branch.
13. Never overwrite, stash, reset, or clean the developer's uncommitted work.
14. Do not claim tests pass unless you ran them and observed the result.
15. At the end of each milestone, report changed files, design decisions, commands run, test results, known limitations, and the next milestone.

## Git and change management

- Inspect `git status` before modifying files.
- If the working tree contains unrelated changes, preserve them and ask before proceeding where necessary.
- Keep changes focused.
- Use Conventional Commits:
  - `feat:` new capability
  - `fix:` bug fix
  - `docs:` documentation
  - `test:` tests
  - `refactor:` behavior-preserving restructuring
  - `build:` packaging/dependency/build changes
  - `chore:` maintenance
- Do not create commits unless the developer asks or the repository workflow explicitly authorizes it.

## Definition of done for a change

- Requirements and acceptance criteria are met.
- No unrelated scope has been added.
- Relevant tests cover observable behavior and failure cases.
- Commands and test outcomes are reported accurately.
- Security and workflow invariants remain intact.
- Documentation is updated where behavior or commands changed.
