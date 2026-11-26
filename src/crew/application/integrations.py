"""Use cases that read from integrations before a run starts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from crew.domain.errors import PreconditionError
from crew.integrations import factory
from crew.integrations.base import DocPage, ReviewComment
from crew.orchestrator.engine import Project


@dataclass
class RunSources:
    request: str
    issue_key: str | None = None
    docs: list[DocPage] = field(default_factory=list)


def review_comments_request(number: int, comments: list[ReviewComment], extra: str) -> str:
    lines = [f"Address the code review comments on pull request #{number}:", ""]
    for c in comments:
        where = f" ({c.path}:{c.line})" if c.path and c.line else f" ({c.path})" if c.path else ""
        lines.append(f"- {c.author}{where}: {c.body.strip()}")
    if extra.strip():
        lines += ["", "Additional instructions from the developer:", extra.strip()]
    return "\n".join(lines)


REQUEST_FILE_MAX_CHARS = 100_000


def read_request_file(path: Path) -> str:
    """The request text from a file (Markdown or plain text)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PreconditionError(f"request file {path} not found") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise PreconditionError(f"cannot read request file {path}: {exc}") from exc
    if len(text) > REQUEST_FILE_MAX_CHARS:
        raise PreconditionError(
            f"request file {path} is {len(text):,} characters; the limit is "
            f"{REQUEST_FILE_MAX_CHARS:,}",
            hint="Keep the request focused; pass long specifications with --doc instead.",
        )
    return text.strip()


def gather_sources(
    project: Project,
    request: str | None,
    *,
    request_file: Path | None = None,
    issue_key: str | None = None,
    doc_refs: list[str] | None = None,
    pr_comments: int | None = None,
) -> RunSources:
    """Turn CLI inputs (text, request file, issue, docs, PR review) into one run request.

    A request file and free text combine: the file first, then the text as
    additional instructions.
    """
    text = (request or "").strip()
    if request_file is not None:
        from_file = read_request_file(request_file)
        text = from_file if not text else f"{from_file}\n\nAdditional instructions:\n{text}"
    sources = RunSources(request=text)
    if issue_key:
        issue = factory.issue_tracker(project.config).fetch_issue(issue_key)
        sources.request = issue.as_request(text)
        sources.issue_key = issue.key
    if pr_comments is not None:
        comments = factory.code_host(project.config).fetch_review_comments(pr_comments)
        if not comments:
            raise PreconditionError(f"pull request #{pr_comments} has no review comments")
        sources.request = review_comments_request(pr_comments, comments, sources.request)
    if doc_refs:
        docs = factory.docs_target(project.config, project.paths.root)
        sources.docs = [docs.fetch_page(ref) for ref in doc_refs]
    if not sources.request:
        raise PreconditionError(
            "Nothing to do: give a request, --file, --issue or --pr-comments.",
            hint='crew run "Add a health endpoint"  or  crew run --issue PROJ-123',
        )
    return sources
