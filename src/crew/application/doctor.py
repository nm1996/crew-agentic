"""``crew doctor``: diagnose prerequisites with actionable fixes."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from crew.config.loader import CREW_DIR, ProjectPaths, load_config
from crew.config.schema import CrewConfig
from crew.domain.errors import CrewError, ProviderError
from crew.persistence.database import schema_status
from crew.providers.claude_code.adapter import resolve_cli_command
from crew.security.sandbox import SandboxSettings
from crew.security.sandbox import check_available as sandbox_problem
from crew.workspace.git import Git

MIN_PYTHON = (3, 12)
MIN_GIT = (2, 25)  # reliable `git worktree` support
REQUIRED_CLAUDE_FLAGS = (
    "--print",
    "--output-format",
    "--json-schema",
    "--permission-mode",
    "--permission-prompts",
    "--tools",
    "--allowedTools",
    "--no-session-persistence",
    "--append-system-prompt",
    "--restricted",
    "--strict-mcp-config",
)


class Level(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str
    fix: str = ""


def _run(argv: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_python() -> Check:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] >= MIN_PYTHON:
        return Check("Python", Level.OK, version)
    return Check(
        "Python", Level.FAIL, version, "Install Python 3.12+ (e.g. `uv python install 3.12`)."
    )


def check_git() -> Check:
    if not shutil.which("git"):
        return Check(
            "Git", Level.FAIL, "not found", "Install Git 2.25+ (e.g. `sudo apt install git`)."
        )
    proc = _run(["git", "--version"])
    text = proc.stdout.strip() if proc else "unknown"
    try:
        numbers = tuple(int(x) for x in text.split()[2].split(".")[:2])
    except (IndexError, ValueError):
        return Check("Git", Level.WARN, text, "Could not parse the Git version.")
    if numbers < MIN_GIT:
        return Check("Git", Level.FAIL, text, "Upgrade Git to 2.25 or newer for worktree support.")
    return Check("Git", Level.OK, text)


def check_repository(cwd: Path) -> list[Check]:
    git = Git(cwd)
    if not git.is_repository():
        return [
            Check("Repository", Level.FAIL, f"{cwd} is not a Git repository", "Run `git init`.")
        ]
    checks = [Check("Repository", Level.OK, str(git.toplevel()))]
    head = git.head()
    if head is None:
        checks.append(Check("Commits", Level.FAIL, "no commits", "Create an initial commit."))
    else:
        checks.append(
            Check(
                "Commits",
                Level.OK,
                f"HEAD {head[:12]} on {git.current_branch() or 'detached HEAD'}",
            )
        )
    status = git.status()
    if status.is_clean:
        checks.append(Check("Working tree", Level.OK, "clean"))
    else:
        checks.append(
            Check(
                "Working tree",
                Level.WARN,
                f"{len(status.dirty_paths)} uncommitted change(s)",
                "Implementation requires a clean tree; commit your work first.",
            )
        )
    return checks


def check_project(cwd: Path) -> tuple[list[Check], CrewConfig | None]:
    git = Git(cwd)
    root = git.toplevel() if git.is_repository() else cwd
    paths = ProjectPaths(root)
    if not paths.config_file.exists():
        return [
            Check("CREW project", Level.FAIL, f"no {CREW_DIR}/config.yaml", "Run `crew init`.")
        ], None
    try:
        config = load_config(paths)
    except CrewError as exc:
        return [Check("Configuration", Level.FAIL, exc.message, "Fix .crew/config.yaml.")], None
    checks = [Check("Configuration", Level.OK, f"provider={config.provider.default}")]
    checks.append(check_sandbox(config))
    checks += check_integrations(config)
    current, head = schema_status(paths.db_file)
    if current == head:
        checks.append(Check("Database", Level.OK, f"schema {current}"))
    else:
        checks.append(
            Check(
                "Database",
                Level.WARN,
                f"schema {current or 'missing'} (latest {head})",
                "Run any crew command (e.g. `crew status`) to migrate, or `crew init`.",
            )
        )
    for label, directory in (
        ("Runs directory", paths.runs_dir),
        ("Logs directory", paths.logs_dir),
    ):
        if directory.is_dir() and os.access(directory, os.W_OK):
            checks.append(Check(label, Level.OK, "writable"))
        else:
            checks.append(
                Check(label, Level.FAIL, f"{directory} missing or not writable", "Run `crew init`.")
            )
    return checks, config


def check_integrations(config: CrewConfig) -> list[Check]:
    """Configured integrations and their credential variables (no network calls)."""
    integrations = config.integrations
    checks: list[Check] = []
    pairs = [
        ("Issue tracker", integrations.issue_tracker),
        ("Code host", integrations.code_host),
        ("Docs", integrations.docs),
    ]
    for label, settings in pairs:
        if settings is None:
            continue
        envs = [getattr(settings, a) for a in ("email_env", "token_env") if hasattr(settings, a)]
        missing = [e for e in envs if not os.environ.get(e)]
        if label == "Code host" and missing and shutil.which("gh"):
            missing = []  # GitHub falls back to `gh auth token`
        if missing:
            checks.append(
                Check(
                    label,
                    Level.WARN,
                    f"{settings.kind}: {', '.join(missing)} not set",
                    f"export {missing[0]}=… before `crew run --issue` / `crew publish`.",
                )
            )
        else:
            checks.append(Check(label, Level.OK, f"{settings.kind} configured"))
    return checks


def check_sandbox(config: CrewConfig) -> Check:
    execution = config.execution
    settings = SandboxSettings(
        mode=execution.sandbox,
        image=execution.sandbox_image,
        network=execution.sandbox_network,
    )
    if settings.mode == "none":
        level = Level.WARN if execution.verification_commands else Level.OK
        return Check(
            "Verification sandbox",
            level,
            "none" + (" (verification runs agent-written code unsandboxed)" if level else ""),
            "Set execution.sandbox=bwrap (Linux) or docker." if level is Level.WARN else "",
        )
    problem = sandbox_problem(settings)
    if problem:
        return Check(
            "Verification sandbox",
            Level.FAIL,
            f"{settings.mode}: {problem}",
            "Install/enable it, or set execution.sandbox=none.",
        )
    net = "network on" if settings.network else "no network"
    return Check("Verification sandbox", Level.OK, f"{settings.mode} ({net})")


def check_claude(config: CrewConfig | None) -> list[Check]:
    required = config is None or config.provider.default == "claude_code"
    miss = Level.FAIL if required else Level.WARN
    settings = config.provider.claude_code if config else None
    executable = settings.executable if settings else "claude"
    path = shutil.which(executable)
    if not path:
        return [
            Check(
                "Claude Code CLI",
                miss,
                f"'{executable}' not found on PATH",
                "Install Claude Code (see https://docs.claude.com/claude-code), then run `claude` "
                "once to log in. Or set provider.default=fake to try CREW offline.",
            )
        ]
    try:
        command = resolve_cli_command(path)
    except ProviderError as exc:
        return [Check("Claude Code CLI", miss, exc.message, exc.hint or "")]
    version = _run([*command, "--version"])
    checks = [
        Check(
            "Claude Code CLI", Level.OK, f"{path} ({(version.stdout.strip() if version else '?')})"
        )
    ]

    help_proc = _run([*command, "--help"])
    help_text = help_proc.stdout if help_proc else ""
    flags = list(REQUIRED_CLAUDE_FLAGS)
    if settings and (
        settings.profile != "default" or any(r.effort for r in settings.roles.values())
    ):
        flags.append("--effort")
    missing = [f for f in flags if f not in help_text]
    if missing:
        checks.append(
            Check(
                "Claude CLI flags",
                miss,
                "missing: " + ", ".join(missing),
                "Update Claude Code (`claude update`). CREW requires these flags to isolate "
                "agents and will not run without them.",
            )
        )
    else:
        checks.append(Check("Claude CLI flags", Level.OK, "all required flags supported"))

    # Authentication cannot be verified without an API call; report what is detectable.
    home = Path.home()
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        checks.append(Check("Claude auth", Level.OK, "credentials found in environment"))
    elif (home / ".claude" / ".credentials.json").exists() or (home / ".claude.json").exists():
        checks.append(Check("Claude auth", Level.OK, "local Claude Code login detected"))
    else:
        checks.append(
            Check(
                "Claude auth",
                Level.WARN,
                "could not detect a login",
                "Run `claude` once and log in, or export ANTHROPIC_API_KEY.",
            )
        )
    return checks


def check_platform(cwd: Path) -> Check:
    detail = f"{platform.system()} {platform.release()} ({platform.machine()})"
    resolved = str(cwd.resolve())
    if "onedrive" in resolved.casefold() or resolved.startswith("\\\\"):
        return Check(
            "Platform",
            Level.WARN,
            detail,
            "The project is on a synced/network folder; SQLite and git worktrees are unreliable "
            "there. Move it to a local disk.",
        )
    return Check("Platform", Level.OK, detail)


def run_doctor(cwd: Path) -> list[Check]:
    checks = [check_platform(cwd), check_python(), check_git()]
    if checks[-1].level is Level.FAIL:
        return checks
    checks += check_repository(cwd)
    project_checks, config = check_project(cwd)
    checks += project_checks
    checks += check_claude(config)
    return checks
