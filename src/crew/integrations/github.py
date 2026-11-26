"""GitHub code-host adapter (REST API).

Token: the environment variable named in ``integrations.code_host.token_env``
(default ``GITHUB_TOKEN``), falling back to ``gh auth token`` when the GitHub
CLI is installed and logged in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from urllib.parse import quote

from crew.config.schema import GitHubSettings
from crew.integrations.base import PullRequest, ReviewComment
from crew.integrations.http import HttpClient, IntegrationError, Transport

MAX_BODY_CHARS = 60_000  # GitHub limits PR/review bodies to 65,536 characters


def _gh_cli_token() -> str | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    proc = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, check=False)
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


def _clip(text: str) -> str:
    if len(text) <= MAX_BODY_CHARS:
        return text
    return text[:MAX_BODY_CHARS] + "\n\n…(truncated; see the full CREW report)"


class GitHubHost:
    name = "github"

    def __init__(
        self,
        settings: GitHubSettings,
        *,
        transport: Transport | None = None,
        token: str | None = None,
    ) -> None:
        self.settings = settings
        token = token or os.environ.get(settings.token_env) or _gh_cli_token()
        if not token:
            raise IntegrationError(
                f"GitHub needs a token in {settings.token_env} (or a logged-in `gh` CLI)",
                hint=f"export {settings.token_env}=… or run `gh auth login`",
            )
        self.http = HttpClient(settings.api_url, f"Bearer {token}", transport=transport)
        self._repo = "/repos/" + "/".join(quote(p) for p in settings.repo.split("/"))

    def open_pull_request(
        self, branch: str, base: str, title: str, body: str, *, draft: bool = False
    ) -> PullRequest:
        data = self.http.post(
            f"{self._repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": _clip(body), "draft": draft},
        )
        return PullRequest(number=int(data["number"]), url=str(data["html_url"]), branch=branch)

    def post_review(self, number: int, body: str) -> None:
        self.http.post(
            f"{self._repo}/pulls/{number}/reviews", {"body": _clip(body), "event": "COMMENT"}
        )

    def fetch_review_comments(self, number: int) -> list[ReviewComment]:
        comments: list[ReviewComment] = []
        for review in self.http.get(f"{self._repo}/pulls/{number}/reviews") or []:
            if (review.get("body") or "").strip():
                comments.append(
                    ReviewComment(
                        author=(review.get("user") or {}).get("login", "?"), body=review["body"]
                    )
                )
        for c in self.http.get(f"{self._repo}/pulls/{number}/comments") or []:
            comments.append(
                ReviewComment(
                    author=(c.get("user") or {}).get("login", "?"),
                    body=c.get("body", ""),
                    path=c.get("path"),
                    line=c.get("line") or c.get("original_line"),
                )
            )
        return comments
