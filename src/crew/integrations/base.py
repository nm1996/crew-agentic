"""Provider-neutral integration contracts and data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Issue:
    key: str
    title: str
    description: str  # plain text / Markdown
    url: str
    status: str | None = None
    labels: tuple[str, ...] = ()

    def as_request(self, extra: str = "") -> str:
        """The run request built from this issue (plus optional developer text)."""
        parts = [f"{self.key}: {self.title}", "", self.description.strip()]
        if extra.strip():
            parts += ["", "Additional instructions from the developer:", extra.strip()]
        parts += ["", f"Issue: {self.url}"]
        return "\n".join(parts).strip()


@dataclass(frozen=True)
class DocPage:
    ref: str  # id, URL or path as given
    title: str
    body: str  # plain text / Markdown
    url: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str


@dataclass(frozen=True)
class ReviewComment:
    author: str
    body: str
    path: str | None = None
    line: int | None = None


@dataclass
class PublishResult:
    """What `crew publish` did, for the CLI and the event log."""

    pushed_branch: str | None = None
    pull_request: PullRequest | None = None
    review_posted: bool = False
    issue_commented: bool = False
    issue_transitioned: str | None = None
    doc_url: str | None = None
    notes: list[str] = field(default_factory=list)


class IssueTracker(Protocol):
    name: str

    def fetch_issue(self, key: str) -> Issue: ...

    def comment(self, key: str, text: str) -> None: ...

    def transition(self, key: str, state: str) -> None: ...


class CodeHost(Protocol):
    name: str

    def open_pull_request(
        self, branch: str, base: str, title: str, body: str, *, draft: bool = False
    ) -> PullRequest: ...

    def post_review(self, number: int, body: str) -> None: ...

    def fetch_review_comments(self, number: int) -> list[ReviewComment]: ...


class DocsTarget(Protocol):
    name: str

    def fetch_page(self, ref: str) -> DocPage: ...

    def publish_page(self, title: str, markdown: str) -> str:
        """Create or update a page; returns its URL or path."""
        ...
