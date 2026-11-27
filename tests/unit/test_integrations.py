"""Integration adapters against a fake HTTP transport (no network, no credentials)."""

import json
from pathlib import Path

import pytest

from crew.config.schema import (
    ConfluenceSettings,
    CrewConfig,
    FolderDocsSettings,
    GitHubSettings,
    GitLabSettings,
    JiraSettings,
)
from crew.integrations import factory
from crew.integrations.adf import adf_to_text, text_to_adf
from crew.integrations.base import InlineComment
from crew.integrations.docs import ConfluenceDocs, FolderDocs
from crew.integrations.github import GitHubHost
from crew.integrations.gitlab import GitLabHost
from crew.integrations.http import HttpClient, HttpResponse, IntegrationError, urllib_transport
from crew.integrations.jira import JiraTracker
from crew.integrations.markup import markdown_to_storage, storage_to_text


class FakeTransport:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict, object]] = []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body) if body else None
        self.calls.append((method, url, headers, payload))
        for (m, suffix), (status, data) in self.routes.items():
            if m == method and url.endswith(suffix):
                return HttpResponse(status, json.dumps(data).encode())
        return HttpResponse(404, b'{"message": "not found"}')


@pytest.fixture
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-secret-token")


ISSUE = {
    "fields": {
        "summary": "Add health endpoint",
        "status": {"name": "To Do"},
        "labels": ["api"],
        "description": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Expose GET /health."}]},
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "returns 200"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "version",
                                            "marks": [{"type": "code"}],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ],
        },
    }
}


def test_jira_fetch_issue_converts_description(jira_env):
    transport = FakeTransport({("GET", "fields=summary,description,status,labels"): (200, ISSUE)})
    tracker = JiraTracker(JiraSettings(base_url="https://acme.atlassian.net"), transport=transport)
    issue = tracker.fetch_issue("proj-12")
    assert issue.key == "PROJ-12" and issue.title == "Add health endpoint"
    assert "Expose GET /health." in issue.description
    assert "- returns 200" in issue.description and "`version`" in issue.description
    assert issue.url == "https://acme.atlassian.net/browse/PROJ-12"
    assert transport.calls[0][2]["Authorization"].startswith("Basic ")
    assert "PROJ-12: Add health endpoint" in issue.as_request("also log it")


def test_jira_comment_sends_adf_and_transition_by_name(jira_env):
    transport = FakeTransport(
        {
            ("POST", "/comment"): (201, {}),
            ("GET", "/transitions"): (
                200,
                {
                    "transitions": [
                        {"id": "31", "name": "Start review", "to": {"name": "In Review"}}
                    ]
                },
            ),
            ("POST", "/transitions"): (204, None),
        }
    )
    tracker = JiraTracker(JiraSettings(base_url="https://acme.atlassian.net"), transport=transport)
    tracker.comment("PROJ-1", "Done.\n\n- PR: https://x")
    body = transport.calls[0][3]["body"]
    assert body["type"] == "doc" and body["content"][1]["type"] == "bulletList"
    tracker.transition("PROJ-1", "in review")  # matches the target status, case-insensitively
    assert transport.calls[-1][3] == {"transition": {"id": "31"}}
    with pytest.raises(IntegrationError, match="no transition 'Done'"):
        tracker.transition("PROJ-1", "Done")


def test_jira_requires_credentials_and_valid_keys(monkeypatch, jira_env):
    with pytest.raises(IntegrationError, match="not a Jira issue key"):
        JiraTracker(
            JiraSettings(base_url="https://a.atlassian.net"), transport=FakeTransport({})
        ).fetch_issue("x y")
    monkeypatch.delenv("JIRA_API_TOKEN")
    with pytest.raises(IntegrationError, match="JIRA_API_TOKEN"):
        JiraTracker(JiraSettings(base_url="https://a.atlassian.net"))


def test_http_errors_are_redacted_and_https_is_required():
    transport = FakeTransport({("GET", "/x"): (401, {"error": "bad token ghp_" + "a" * 30})})
    client = HttpClient("https://api.example.com", "Bearer t", transport=transport)
    with pytest.raises(IntegrationError, match="HTTP 401") as info:
        client.get("/x")
    assert "ghp_" not in info.value.message
    with pytest.raises(IntegrationError, match="non-HTTPS"):
        urllib_transport("GET", "http://example.com", {}, None)


def test_adf_round_trip_of_simple_text():
    doc = text_to_adf("Intro line\n\n- one\n- two\n\n```\ncode here\n```")
    text = adf_to_text(doc)
    assert "Intro line" in text and "- one" in text and "code here" in text


@pytest.fixture
def confluence_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_EMAIL", "dev@example.com")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "secret")


def test_confluence_fetch_page_by_url(confluence_env):
    page = {
        "title": "Architecture",
        "body": {
            "storage": {
                "value": "<h2>Layers</h2><p>Handlers call <code>services</code>.</p>"
                "<ul><li>no SQL in handlers</li></ul>"
            }
        },
        "_links": {"webui": "/spaces/ENG/pages/123/Architecture"},
    }
    transport = FakeTransport({("GET", "/wiki/api/v2/pages/123?body-format=storage"): (200, page)})
    docs = ConfluenceDocs(
        ConfluenceSettings(base_url="https://acme.atlassian.net"), transport=transport
    )
    got = docs.fetch_page("https://acme.atlassian.net/wiki/spaces/ENG/pages/123/Architecture")
    assert got.title == "Architecture"
    assert "## Layers" in got.body and "- no SQL in handlers" in got.body
    assert got.url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/123/Architecture"


def test_confluence_publish_converts_markdown(confluence_env):
    transport = FakeTransport(
        {("POST", "/wiki/api/v2/pages"): (200, {"id": "9", "_links": {"webui": "/x/9"}})}
    )
    settings = ConfluenceSettings(
        base_url="https://acme.atlassian.net", space_id="42", parent_id="7"
    )
    url = ConfluenceDocs(settings, transport=transport).publish_page(
        "Report", "# T\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    )
    payload = transport.calls[0][3]
    assert payload["spaceId"] == "42" and payload["parentId"] == "7"
    assert "<h1>T</h1>" in payload["body"]["value"] and "<td>1</td>" in payload["body"]["value"]
    assert url == "https://acme.atlassian.net/wiki/x/9"


def test_markup_code_blocks_survive_both_ways():
    storage = markdown_to_storage("```python\nprint('<hi>')\n```")
    assert "CDATA[print('<hi>')]]" in storage
    assert "print('<hi>')" in storage_to_text(storage)


def test_folder_docs_never_overwrite_and_stay_inside_repo(tmp_path: Path):
    docs = FolderDocs(FolderDocsSettings(path="docs/reports"), tmp_path)
    path = docs.publish_page("CREW report 1: Add X", "# Report\n")
    assert path == "docs/reports/crew-report-1-add-x.md"
    with pytest.raises(IntegrationError, match="not overwriting"):
        docs.publish_page("CREW report 1: Add X", "# Again\n")
    assert docs.fetch_page(path).title == "Report"
    with pytest.raises(IntegrationError):
        docs.fetch_page("../outside.md")
    with pytest.raises(IntegrationError, match="inside the repository"):
        FolderDocs(FolderDocsSettings(path="../elsewhere"), tmp_path)


def test_github_pull_request_review_and_comments():
    transport = FakeTransport(
        {
            ("POST", "/repos/acme/shop/pulls"): (
                201,
                {"number": 7, "html_url": "https://github.com/acme/shop/pull/7"},
            ),
            ("POST", "/pulls/7/reviews"): (200, {}),
            ("GET", "/pulls/7/reviews"): (
                200,
                [{"user": {"login": "ana"}, "body": "Please add tests"}],
            ),
            ("GET", "/pulls/7/comments"): (
                200,
                [{"user": {"login": "bo"}, "body": "Off by one", "path": "a.py", "line": 3}],
            ),
        }
    )
    host = GitHubHost(GitHubSettings(repo="acme/shop"), transport=transport, token="t0ken")
    pr = host.open_pull_request("crew/r/integration", "main", "Title", "x" * 70_000, draft=True)
    assert pr.number == 7
    sent = transport.calls[0][3]
    assert sent["head"] == "crew/r/integration" and sent["draft"] is True
    assert len(sent["body"]) < 61_000  # clipped to GitHub's limit
    host.post_review(7, "findings")
    assert transport.calls[1][3]["event"] == "COMMENT"
    comments = host.fetch_review_comments(7)
    assert [(c.author, c.path, c.line) for c in comments] == [
        ("ana", None, None),
        ("bo", "a.py", 3),
    ]


INLINE = [InlineComment("a.py", 3, "Off by one"), InlineComment("b.py", 9, "Typo")]


def test_github_inline_review_falls_back_when_a_line_is_not_in_the_diff():
    ok = FakeTransport({("POST", "/pulls/7/reviews"): (200, {})})
    host = GitHubHost(GitHubSettings(repo="acme/shop"), transport=ok, token="t")
    assert host.post_review(7, "body", INLINE) == 2
    sent = ok.calls[0][3]
    assert sent["comments"][0] == {"path": "a.py", "line": 3, "side": "RIGHT", "body": "Off by one"}

    class Unprocessable(FakeTransport):
        def __call__(self, method, url, headers, body):
            if body and "comments" in json.loads(body):
                self.calls.append((method, url, headers, json.loads(body)))
                return HttpResponse(422, b'{"message": "line must be part of the diff"}')
            return super().__call__(method, url, headers, body)

    rejected = Unprocessable({("POST", "/pulls/7/reviews"): (200, {})})
    host = GitHubHost(GitHubSettings(repo="acme/shop"), transport=rejected, token="t")
    assert host.post_review(7, "body", INLINE) == 0
    assert "comments" not in rejected.calls[-1][3] and rejected.calls[-1][3]["body"] == "body"


MR = "/projects/acme%2Fteam%2Fshop/merge_requests"


def test_gitlab_merge_request_review_and_notes():
    transport = FakeTransport(
        {
            ("POST", MR): (
                201,
                {"iid": 4, "web_url": "https://gitlab.com/acme/team/shop/-/merge_requests/4"},
            ),
            ("POST", f"{MR}/4/notes"): (201, {}),
            ("GET", f"{MR}/4"): (
                200,
                {"diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}},
            ),
            ("POST", f"{MR}/4/discussions"): (201, {}),
            ("GET", f"{MR}/4/notes?per_page=100"): (
                200,
                [
                    {"system": True, "body": "added 1 commit", "author": {"username": "bot"}},
                    {"body": "Please add tests", "author": {"username": "ana"}},
                    {
                        "body": "Off by one",
                        "author": {"username": "bo"},
                        "position": {"new_path": "a.py", "new_line": 3},
                    },
                ],
            ),
        }
    )
    settings = GitLabSettings(project="acme/team/shop")
    host = GitLabHost(settings, transport=transport, token="glpat-secret")
    pr = host.open_pull_request("crew/r/integration", "main", "Title", "body", draft=True)
    assert pr.number == 4 and pr.url.endswith("/merge_requests/4")
    sent = transport.calls[0][3]
    assert sent["source_branch"] == "crew/r/integration" and sent["title"] == "Draft: Title"
    assert transport.calls[0][2]["Authorization"] == "Bearer glpat-secret"
    assert host.post_review(4, "findings", INLINE) == 2
    discussion = transport.calls[-1][3]
    assert discussion["position"] == {
        "position_type": "text",
        "base_sha": "b",
        "start_sha": "s",
        "head_sha": "h",
        "new_path": "b.py",
        "new_line": 9,
    }
    comments = host.fetch_review_comments(4)
    assert [(c.author, c.path, c.line) for c in comments] == [
        ("ana", None, None),
        ("bo", "a.py", 3),
    ]


def test_gitlab_skips_inline_comments_outside_the_diff():
    class OutsideDiff(FakeTransport):
        def __call__(self, method, url, headers, body):
            if url.endswith("/discussions") and json.loads(body)["position"]["new_path"] == "a.py":
                self.calls.append((method, url, headers, json.loads(body)))
                return HttpResponse(400, b'{"message": "line_code can not be blank"}')
            return super().__call__(method, url, headers, body)

    transport = OutsideDiff(
        {
            ("POST", f"{MR}/4/notes"): (201, {}),
            ("GET", f"{MR}/4"): (
                200,
                {"diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}},
            ),
            ("POST", f"{MR}/4/discussions"): (201, {}),
        }
    )
    host = GitLabHost(GitLabSettings(project="acme/team/shop"), transport=transport, token="t")
    assert host.post_review(4, "findings", INLINE) == 1


def test_gitlab_needs_a_token_and_is_chosen_by_the_factory(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with pytest.raises(IntegrationError, match="GITLAB_TOKEN"):
        GitLabHost(GitLabSettings(project="acme/shop"))
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    config = CrewConfig.model_validate(
        {
            "project": {"name": "shop"},
            "integrations": {"code_host": {"kind": "gitlab", "project": "acme/shop"}},
        }
    )
    assert isinstance(factory.code_host(config), GitLabHost)
    with pytest.raises(ValueError):
        GitLabSettings(project="no-namespace")
