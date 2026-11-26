"""Bounded repository context for the Director (docs/SECURITY.md §5).

Context is collected from the run's *base commit* (tracked files only, via the
director worktree), so untracked local files such as ``.env`` are never visible.
Files matching secret patterns are excluded from listings and never read.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from crew.workspace.git import Git

# Gitignore-style globs, also passed to the provider as Read() deny rules.
SECRET_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.kdbx",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/credentials*",
    "**/*secret*",
)

KEY_FILES = (
    "README.md",
    "README.rst",
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
)
KEY_FILE_MAX_CHARS = 6_000
PROJECT_NOTES_MAX_CHARS = 10_000


def is_excluded(path: str, extra: tuple[str, ...] = ()) -> bool:
    name = PurePosixPath(path).name
    for pattern in (*SECRET_GLOBS, *extra):
        bare = pattern.removeprefix("**/")
        if fnmatch(path, pattern) or fnmatch(name, bare) or fnmatch(path, bare):
            return True
    return False


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
    for name in KEY_FILES:
        candidate = workspace / name
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            key_files[name] = text[:KEY_FILE_MAX_CHARS]
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
