"""Use cases that read from integrations before a run starts."""

from __future__ import annotations

from dataclasses import dataclass, field

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


def gather_sources(
    project: Project,
    request: str | None,
    *,
    issue_key: str | None = None,
    doc_refs: list[str] | None = None,
    pr_comments: int | None = None,
) -> RunSources:
    """Turn CLI inputs (free text, issue, docs, PR review) into one run request."""
    text = (request or "").strip()
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
            "Nothing to do: give a request, --issue or --pr-comments.",
            hint='crew run "Add a health endpoint"  or  crew run --issue PROJ-123',
        )
    return sources
