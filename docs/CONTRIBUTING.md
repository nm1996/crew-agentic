# Contributing to CREW

## Engineering principles

- Prefer clarity over cleverness.
- Keep the MVP small and local-first.
- Preserve explicit human approval gates.
- Make failure modes observable and recoverable.
- Keep provider-specific code behind the provider interface.
- Test workflow invariants, not private implementation details.

## Local development

Use `uv` and the Python version selected after checking project compatibility.

Typical setup (adjust only if the project configuration requires it):

```bash
uv sync --dev
uv run crew --help
uv run pytest
```

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
