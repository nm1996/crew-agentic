# Installation

## Prerequisites

| Requirement | Version | Check | Notes |
|---|---|---|---|
| OS | Linux | – | macOS should work (POSIX process groups, `flock`); Windows is not supported in the MVP. |
| Python | ≥ 3.12 | `python3 --version` | `uv` can install it: `uv python install 3.12`. |
| uv | recent | `uv --version` | <https://docs.astral.sh/uv/getting-started/installation/> |
| Git | ≥ 2.25 | `git --version` | Needed for `git worktree`. |
| Claude Code CLI | 2.1.x tested | `claude --version` | Only for the real provider. Install per <https://docs.claude.com/claude-code>, then run `claude` once to log in (or export `ANTHROPIC_API_KEY`). |

CREW verifies all of this for you: `crew doctor`.

## Install the `crew` command

From a clone of this repository:

```bash
git clone <this-repo-url> crew && cd crew
uv tool install .            # installs `crew` into ~/.local/bin (isolated venv)
crew --version
```

Upgrade after pulling changes: `uv tool install --reinstall .`
Uninstall: `uv tool uninstall crew-cli`.

Alternatives:

```bash
pipx install .               # if you prefer pipx
uv run crew --help           # run from the clone without installing (development)
```

## Set up a project

CREW works on an existing Git repository with at least one commit.

```bash
cd ~/code/my-project
crew init                    # or: crew init --provider fake   (offline demo)
crew doctor
```

`crew init` creates, and never overwrites:

```text
.crew/
  config.yaml        validated project configuration
  context/project.md notes for the Director (conventions, architecture, off-limits areas)
  crew.db            SQLite state (migrated automatically)
  runs/              per-run Markdown artifacts
  logs/crew.log      debug log (secrets redacted)
```

It also adds `.crew/` to `.git/info/exclude` (a local, untracked file) so CREW state never
shows up in `git status` and your `.gitignore` is not touched.

Then configure how CREW verifies changes, for example for a Python project:

```bash
crew configure 'execution.verification_commands=["uv run pytest -q"]' \
               'provider.claude_code.implementer_bash_rules=["Bash(uv run pytest:*)"]'
```

See [CONFIGURATION.md](CONFIGURATION.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor`: *Claude Code CLI not found* | Install Claude Code, make sure `claude` is on `PATH`, or set `provider.claude_code.executable` to its absolute path. |
| `doctor`: *Claude CLI flags missing* | Update Claude Code (`claude update`). CREW refuses to run agents without `--restricted` and `--strict-mcp-config`. |
| `doctor`: *could not detect a login* | Run `claude` once interactively and log in, or export `ANTHROPIC_API_KEY`. Detection is best-effort; this is a warning only. |
| `approve-plan`: *working tree has uncommitted changes* | Commit your work. CREW never stashes or resets it for you. |
| `error: Another CREW process is currently driving this run` | Another terminal is running that run. Wait, or `crew stop <run-id>`. |
| A run was interrupted (Ctrl-C, crash, reboot) | `crew resume <run-id>`. See [USAGE.md](USAGE.md#interruption-and-recovery). |
