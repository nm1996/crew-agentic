"""Context collected for the Director."""

from crew.context.collector import KEY_FILES_TOTAL_MAX_CHARS, collect_context
from tests.conftest import git


def test_build_files_are_included_and_bounded(repo, tmp_path):
    (repo / "pom.xml").write_text("<project><artifactId>shop</artifactId></project>")
    (repo / "build.gradle.kts").write_text("plugins { java }")
    (repo / "package.json").write_text("{" + '"x": 1,' * 5000 + '"y": 2}')
    (repo / ".env").write_text("SECRET=1")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "build files")
    ctx = collect_context(repo, notes_dir=tmp_path / "none", max_files=100)
    assert "pom.xml" in ctx.key_files and "artifactId" in ctx.key_files["pom.xml"]
    assert "build.gradle.kts" in ctx.key_files
    assert len(ctx.key_files["package.json"]) <= 6_000  # per-file cap
    assert sum(len(v) for v in ctx.key_files.values()) <= KEY_FILES_TOTAL_MAX_CHARS
    assert ".env" not in ctx.files  # secrets never listed


def test_project_notes_are_included(repo, tmp_path):
    notes = tmp_path / "context"
    notes.mkdir()
    (notes / "project.md").write_text("Use Java 21. Never touch db/migrations.")
    ctx = collect_context(repo, notes_dir=notes, max_files=100)
    assert "Never touch db/migrations." in ctx.to_markdown()
