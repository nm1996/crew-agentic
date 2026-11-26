"""Bounded repository context for the Director (docs/SECURITY.md §5).

Context is collected from the run's *base commit* (tracked files only, via the
director worktree), so untracked local files such as ``.env`` are never visible.
Files matching secret patterns are excluded from listings and never read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crew.security.secret_files import SECRET_GLOBS, is_excluded
from crew.workspace.git import Git

__all__ = ["SECRET_GLOBS", "ProjectContext", "collect_context", "is_excluded"]

# Files that describe how a project is built and organised, in priority order.
# Excerpts go into the Director's context; it can open anything else itself.
KEY_FILES = (
    # Agent and project guidance
    "README.md",
    "README.rst",
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    # Build definitions
    "pyproject.toml",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "tsconfig.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
    "Makefile",
    # Runtime
    "Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
)
KEY_FILE_MAX_CHARS = 6_000
KEY_FILES_TOTAL_MAX_CHARS = 40_000  # keeps the Director prompt bounded in large repos
PROJECT_NOTES_MAX_CHARS = 10_000


@dataclass
class ProjectContext:
    files: list[str]
    total_files: int
    key_files: dict[str, str]
    recent_commits: str
    project_notes: str

    def to_markdown(self) -> str:
        lines = ["## Repository files (tracked, at base commit)", ""]
        lines += [f"- {f}" for f in self.files]
        if self.total_files > len(self.files):
            lines.append(f"- … {self.total_files - len(self.files)} more files not listed")
        lines += ["", "## Recent commits", "", "```", self.recent_commits.strip(), "```"]
        if self.project_notes.strip():
            lines += ["", "## Developer project notes (.crew/context)", "", self.project_notes]
        for name, content in self.key_files.items():
            lines += ["", f"## {name} (excerpt)", "", "```", content, "```"]
        return "\n".join(lines)


def collect_context(
    workspace: Path,
    *,
    notes_dir: Path,
    max_files: int,
    exclude: tuple[str, ...] = (),
) -> ProjectContext:
    git = Git(workspace)
    tracked = [f for f in git.ls_files() if not is_excluded(f, exclude)]
    key_files: dict[str, str] = {}
    budget = KEY_FILES_TOTAL_MAX_CHARS
    for name in KEY_FILES:
        candidate = workspace / name
        if budget <= 0 or not candidate.is_file() or is_excluded(name, exclude):
            continue
        text = candidate.read_text(encoding="utf-8", errors="replace")
        excerpt = text[: min(KEY_FILE_MAX_CHARS, budget)]
        key_files[name] = excerpt
        budget -= len(excerpt)
    commits = git.run("log", "--oneline", "-n", "15", check=False)
    notes = []
    if notes_dir.is_dir():
        for note in sorted(notes_dir.glob("*.md")):
            notes.append(note.read_text(encoding="utf-8", errors="replace"))
    return ProjectContext(
        files=tracked[:max_files],
        total_files=len(tracked),
        key_files=key_files,
        recent_commits=commits,
        project_notes="\n\n".join(notes)[:PROJECT_NOTES_MAX_CHARS],
    )
