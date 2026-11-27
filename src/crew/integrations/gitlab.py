"""GitLab code-host adapter (REST API v4, gitlab.com or self-managed).

Token: a personal, project or group access token with the ``api`` scope in the
environment variable named in ``integrations.code_host.token_env`` (default
``GITLAB_TOKEN``), sent as a Bearer token.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from crew.config.schema import GitLabSettings
from crew.integrations.base import InlineComment, PullRequest, ReviewComment
from crew.integrations.http import HttpClient, IntegrationError, Transport

MAX_BODY_CHARS = 60_000  # GitLab allows more; keep the same bound as GitHub


def _clip(text: str) -> str:
    if len(text) <= MAX_BODY_CHARS:
        return text
    return text[:MAX_BODY_CHARS] + "\n\n…(truncated; see the full CREW report)"


class GitLabHost:
    name = "gitlab"

    def __init__(
        self,
        settings: GitLabSettings,
        *,
        transport: Transport | None = None,
        token: str | None = None,
    ) -> None:
        self.settings = settings
        token = token or os.environ.get(settings.token_env)
        if not token:
            raise IntegrationError(
                f"GitLab needs an access token with the api scope in {settings.token_env}",
                hint=f"export {settings.token_env}=…",
            )
        self.http = HttpClient(settings.api_url, f"Bearer {token}", transport=transport)
        self._project = "/projects/" + quote(settings.project, safe="")

    def _mr(self, number: int) -> str:
        return f"{self._project}/merge_requests/{number}"

    def open_pull_request(
        self, branch: str, base: str, title: str, body: str, *, draft: bool = False
    ) -> PullRequest:
        data = self.http.post(
            f"{self._project}/merge_requests",
            {
                "source_branch": branch,
                "target_branch": base,
                "title": f"Draft: {title}" if draft else title,
                "description": _clip(body),
            },
        )
        return PullRequest(number=int(data["iid"]), url=str(data["web_url"]), branch=branch)

    def post_review(self, number: int, body: str, comments: Sequence[InlineComment] = ()) -> int:
        self.http.post(f"{self._mr(number)}/notes", {"body": _clip(body)})
        if not comments:
            return 0
        refs = (self.http.get(self._mr(number)) or {}).get("diff_refs") or {}
        if not all(refs.get(k) for k in ("base_sha", "start_sha", "head_sha")):
            return 0
        placed = 0
        for c in comments:
            position = {
                "position_type": "text",
                "base_sha": refs["base_sha"],
                "start_sha": refs["start_sha"],
                "head_sha": refs["head_sha"],
                "new_path": c.path,
                "new_line": c.line,
            }
            try:
                self.http.post(
                    f"{self._mr(number)}/discussions",
                    {"body": _clip(c.body), "position": position},
                )
                placed += 1
            except IntegrationError as exc:
                if exc.status != 400:  # 400: the line is not part of the diff
                    raise
        return placed

    def fetch_review_comments(self, number: int) -> list[ReviewComment]:
        comments: list[ReviewComment] = []
        notes: list[dict[str, Any]] = self.http.get(f"{self._mr(number)}/notes?per_page=100") or []
        for note in notes:
            if note.get("system") or not (note.get("body") or "").strip():
                continue
            position = note.get("position") or {}
            comments.append(
                ReviewComment(
                    author=(note.get("author") or {}).get("username", "?"),
                    body=note["body"],
                    path=position.get("new_path"),
                    line=position.get("new_line"),
                )
            )
        return comments
