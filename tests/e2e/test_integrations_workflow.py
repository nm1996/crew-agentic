"""Issue/doc-driven runs and `crew publish`, with fake adapters and a local remote."""

from pathlib import Path

import pytest

from crew.application.integrations import gather_sources
from crew.config.schema import GitHubSettings
from crew.domain.enums import RunState
from crew.domain.errors import InvalidTransitionError, PolicyViolationError
from crew.integrations.base import DocPage, Issue, PullRequest, ReviewComment
from tests.conftest import git


class FakeTracker:
    name = "fake-jira"

    def __init__(self):
        self.comments, self.transitions = [], []

    def fetch_issue(self, key):
        return Issue(
            key=key, title="Add part files", description="Create the parts.", url=f"https://j/{key}"
        )

    def comment(self, key, text):
        self.comments.append((key, text))

    def transition(self, key, state):
        self.transitions.append((key, state))


class FakeHost:
    name = "fake-github"

    def __init__(self):
        self.prs, self.reviews = [], []

    def open_pull_request(self, branch, base, title, body, *, draft=False):
        self.prs.append((branch, base, title, body, draft))
        return PullRequest(number=5, url="https://gh/acme/shop/pull/5", branch=branch)

    def post_review(self, number, body):
        self.reviews.append((number, body))

    def fetch_review_comments(self, number):
        return [ReviewComment(author="ana", body="Rename x to count", path="app.py", line=2)]


class FakeDocs:
    name = "fake-docs"

    def __init__(self):
        self.published = []

    def fetch_page(self, ref):
        return DocPage(
            ref=ref, title="Conventions", body="Always log in JSON.", url="https://wiki/1"
        )

    def publish_page(self, title, markdown):
        self.published.append((title, markdown))
        return "https://wiki/2"


@pytest.fixture
def fakes(monkeypatch):
    tracker, host, docs = FakeTracker(), FakeHost(), FakeDocs()
    monkeypatch.setattr("crew.integrations.factory.issue_tracker", lambda *a, **k: tracker)
    monkeypatch.setattr("crew.integrations.factory.code_host", lambda *a, **k: host)
    monkeypatch.setattr("crew.integrations.factory.docs_target", lambda *a, **k: docs)
    return tracker, host, docs


def test_run_from_issue_with_linked_docs(make_orch, fake, project, fakes):
    sources = gather_sources(project, "keep it small", issue_key="PROJ-7", doc_refs=["123"])
    assert sources.request.startswith("PROJ-7: Add part files")
    assert "keep it small" in sources.request
    run = make_orch().start_run(
        sources.request, issue_key=sources.issue_key, linked_docs=sources.docs
    )
    assert run.issue_key == "PROJ-7"
    director_prompt = fake.calls[0].prompt
    assert "Always log in JSON." in director_prompt and "Linked documentation" in director_prompt


def test_run_from_pr_review_comments(project, fakes):
    sources = gather_sources(project, None, pr_comments=5)
    assert "review comments on pull request #5" in sources.request
    assert "ana (app.py:2): Rename x to count" in sources.request


@pytest.fixture
def remote(repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    return bare


def test_publish_pushes_only_the_integration_branch_and_updates_everything(
    make_orch, configure_project, fakes, remote
):
    tracker, host, docs = fakes
    project = configure_project(
        integrations={
            "code_host": GitHubSettings(repo="acme/shop").model_dump(),
            "issue_tracker": {
                "base_url": "https://j.atlassian.net",
                "transition_on_publish": "In Review",
            },
        }
    )
    orch = make_orch(project)
    run = orch.start_run("PROJ-9: add parts", issue_key="PROJ-9")
    run = orch.accept(orch.approve_plan(run.id).id)
    with pytest.raises(InvalidTransitionError):
        make_orch(project).publish(orch.start_run("not done").id, code_host=host, open_pr=True)

    result = orch.publish(
        run.id, code_host=host, open_pr=True, post_review=True, issue_tracker=tracker, docs=docs
    )
    assert result.pull_request.url == "https://gh/acme/shop/pull/5"
    pushed = git(remote, "branch", "--list").split()
    assert pushed == [run.integration_branch]  # nothing else, and never the user's branch
    head, base, title, body, _ = host.prs[0]
    assert head == run.integration_branch
    assert base == "main" and title.startswith("PROJ-9") and "CREW report" in body
    assert host.reviews and host.reviews[0][0] == 5
    assert tracker.comments[0][0] == "PROJ-9" and "pull/5" in tracker.comments[0][1]
    assert tracker.transitions == [("PROJ-9", "In Review")]
    assert docs.published and run.id in docs.published[0][0]
    events = [e.type for e in orch.store.events(run.id)]
    for expected in (
        "published_branch",
        "pull_request_opened",
        "review_posted",
        "issue_commented",
        "issue_transitioned",
        "report_published",
    ):
        assert expected in events
    assert orch.store.get_run(run.id).pull_request_url == "https://gh/acme/shop/pull/5"


def test_push_primitive_refuses_foreign_branches(project, remote):
    with pytest.raises(PolicyViolationError):
        project.git.push_owned_branch("origin", "main", "crew/r1/")
    with pytest.raises(PolicyViolationError):
        project.git.push_owned_branch("origin", "crew/r1/../../main", "crew/r1/")
    assert git(remote, "branch", "--list").strip() == ""


def test_publish_requires_completed_runs(make_orch, fakes):
    orch = make_orch()
    run = orch.start_run("x")
    assert run.state == RunState.AWAITING_PLAN_APPROVAL
    with pytest.raises(InvalidTransitionError):
        orch.publish(run.id, docs=fakes[2])
