# CREW

**Local-first AI development orchestration CLI.** CREW coordinates specialised AI agents to
plan, implement, review and independently evaluate changes inside your Git repository,
while you keep control through explicit approval gates.

```text
crew run "Add health checks to the API"
   │
   ├─ Director plans ─────────────► you approve the plan        (crew approve-plan)
   ├─ ≤2 Implementers in isolated Git worktrees, dependency-aware
   ├─ CREW integrates task branches + runs your test commands
   ├─ Reviewer finds defects ─────► bounded correction cycles
   ├─ External Evaluator checks the result against your original request
   └─ you accept or reject ───────► CREW never merges, pushes or deploys for you
```

- **Human-controlled** – nothing is implemented before you approve the plan, nothing is
  complete before you accept the result. These gates cannot be turned off in config.
- **Safe by construction** – agents only ever run in CREW-owned worktrees; your working
  tree and branch are never modified, stashed, reset or cleaned. Permissions are enforced
  by CLI flags, not prompts.
- **Observable** – every state transition, agent execution and command is recorded in
  SQLite, with Markdown evidence reports per run.
- **Recoverable** – kill the process at any point and `crew resume` continues safely.

## Quick start

Requirements: Linux (macOS likely works), Python ≥ 3.12, Git ≥ 2.25,
[uv](https://docs.astral.sh/uv/), and the [Claude Code CLI](https://docs.claude.com/claude-code)
installed and logged in (not needed for the offline demo).

```bash
# install the CLI (from a clone of this repository)
uv tool install .

# in the repository you want to work on
cd ~/code/my-project
crew init                  # creates .crew/ (never overwrites anything)
crew doctor                # checks Git, Claude CLI, config, database

crew run "Add a /health endpoint with a test"
crew plan <run-id>         # read the Director's plan
crew approve-plan <run-id> # implementation → integration → review → evaluation
crew report <run-id>       # read the evidence
crew accept <run-id>       # or: crew reject <run-id>

git merge --no-ff crew/<run-id>/integration   # you merge, when you choose to
```

Try it offline with the deterministic fake provider: `crew init --provider fake`.

## Documentation

| Document | Contents |
|---|---|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Installing CREW, prerequisites, troubleshooting |
| [docs/USAGE.md](docs/USAGE.md) | Walkthrough, every command, exit codes, recovery |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `.crew/config.yaml` reference and hard limits |
| [docs/README.md](docs/README.md) | Index of all specs: product, architecture, workflow, roles, security, technical spec, decisions, roadmap |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, conventions, tests |
| [CLAUDE.md](CLAUDE.md) | Instructions for Claude Code working on this repository |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

## Development

```bash
uv sync --dev
uv run pytest              # offline: no Claude credentials or network needed
uv run ruff check . && uv run ruff format --check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Status

`0.1.0` — MVP. Linux-first, single developer, Claude Code CLI as the only real provider.
See [docs/ROADMAP.md](docs/ROADMAP.md) and the known limitations in
[docs/DECISIONS.md](docs/DECISIONS.md#known-limitations).
