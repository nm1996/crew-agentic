# Contributing to CREW

## Engineering principles

- Prefer clarity over cleverness.
- Keep the MVP small and local-first.
- Preserve explicit human approval gates.
- Make failure modes observable and recoverable.
- Keep provider-specific code behind the provider interface.
- Test workflow invariants, not private implementation details.

## Local development

Requirements: Python 3.12+ (`.python-version` pins 3.12, the oldest supported), `uv`, Git 2.25+.

```bash
uv sync --dev                      # create .venv with runtime + dev dependencies
uv run crew --help                 # run the CLI from source
uv run pytest                      # full offline suite (~20 s)
uv run pytest tests/unit -q        # fast subset
uv run ruff check . && uv run ruff format --check .
make check                         # lint + format check + tests
```

Real Claude Code smoke tests (cost a few cents, need a logged-in `claude`):

```bash
CREW_CLAUDE_SMOKE=1 uv run pytest -m claude -s
```

Try the full workflow without an API key in any scratch repository:

```bash
git init demo && cd demo && echo hi > README.md && git add . && git commit -m init
uv run --project /path/to/crew crew init --provider fake
uv run --project /path/to/crew crew run "demo"
```

### Database migrations

Schema changes need an Alembic revision (CREW applies migrations automatically at startup):

```bash
mkdir -p .crew
uv run alembic -c alembic.ini upgrade head
uv run alembic -c alembic.ini revision --autogenerate -m "describe change" --rev-id 0002
# review the generated file in src/crew/persistence/migrations/versions/
```

### Where things live

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#implemented-layout). Decisions that are not
obvious from the code are recorded in [docs/DECISIONS.md](docs/DECISIONS.md); update it
when you change one.

## Code conventions

- Follow PEP 8 and the project's configured formatter/linter.
- Add type hints to public interfaces.
- Keep functions focused.
- Prefer explicit names and domain terms.
- Avoid broad exception handling unless errors are translated at a clear boundary.
- Never log secrets or full environment variables.
- Do not add dependencies without a concrete need.

## Tests

- Unit tests should be fast and offline.
- Use the fake provider for orchestration tests.
- Use temporary Git repositories for worktree/integration tests.
- Mark real Claude CLI tests as opt-in.
- Include failure-path coverage for timeouts, malformed output, cancellation, and recovery.

## Commit messages

Use Conventional Commits, for example:

```text
feat: add project initialization command
fix: prevent dispatch of tasks with pending dependencies
docs: clarify worktree recovery behavior
test: cover interrupted provider execution
```

## Pull request expectations

Describe:
- problem and intended behavior;
- design decisions;
- tests and commands run;
- security/workflow implications;
- limitations and follow-up work.
