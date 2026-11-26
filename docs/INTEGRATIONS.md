# Integrations

CREW can read work from, and report results to, the systems around your code:

| Kind | Adapter | Reads | Writes (only on `crew publish`) |
|---|---|---|---|
| Issue tracker | Jira Cloud (REST v3) | issue title + description → run request | comment, transition |
| Code host | GitHub (REST) | PR review comments → run request | push the CREW branch, pull request, review |
| Documentation | Confluence Cloud (REST v2) | pages → Director context | report as a new page |
| Documentation | Markdown folder in the repo | files → Director context | report as a new file |

Principles:

- **Explicit only.** Nothing is sent anywhere during a run. Reading happens when you pass
  `--issue`, `--doc` or `--pr-comments` to `crew run`; writing happens only in
  `crew publish`, only for COMPLETED runs, after you confirm (or pass `--yes`).
- **Credentials from the environment.** The config names environment variables; values
  are never written to `.crew/`, the database, logs or reports.
- **CREW branches only.** `crew publish --pr` pushes exactly `crew/<run>/integration`
  (prefix-checked, never forced). Your branches are never pushed or merged.
- **Recorded.** Every outbound action becomes a workflow event and the publish decision an
  approval; `crew show <run>` lists them.

## Configuration

```yaml
integrations:
  issue_tracker:
    kind: jira
    base_url: https://acme.atlassian.net
    email_env: JIRA_EMAIL            # defaults shown
    token_env: JIRA_API_TOKEN
    comment_on_publish: true         # comment with the PR link and approach
    transition_on_publish: null      # e.g. "In Review" (transition or target status name)
  code_host:
    kind: github
    repo: acme/shop
    remote: origin
    base_branch: null                # default: the run's base branch
    token_env: GITHUB_TOKEN          # falls back to `gh auth token`
  docs:
    kind: confluence                 # or: kind: folder, path: docs/crew-reports
    base_url: https://acme.atlassian.net
    email_env: CONFLUENCE_EMAIL
    token_env: CONFLUENCE_API_TOKEN
    space_id: "123456"               # needed to publish
    parent_id: null
```

`crew doctor` shows each configured integration and warns about missing credential
variables (it makes no network calls).

Credentials:

| Service | What to create | Variables |
|---|---|---|
| Jira / Confluence Cloud | an [Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens) | `JIRA_EMAIL`, `JIRA_API_TOKEN` (and/or `CONFLUENCE_…`) |
| GitHub | a fine-grained token with *Contents: read/write* and *Pull requests: read/write* on the repo, or `gh auth login` | `GITHUB_TOKEN` |
| Markdown folder | nothing | – |

Pushing uses your normal Git credentials for the remote (SSH key or credential helper).

## Starting runs from other systems

```bash
crew run --issue PROJ-123                          # request = issue title + description
crew run --issue PROJ-123 "keep the API backwards compatible"   # plus your notes
crew run "Add rate limiting" --doc 987654 --doc https://acme.atlassian.net/wiki/spaces/ENG/pages/123/Conventions
crew run --pr-comments 42                          # address the review comments on PR #42
```

Linked pages are converted to text and added to the Director's context (bounded to
30 kB). With the folder adapter, `--doc docs/architecture.md` reads a file from the repo.

## Publishing a completed run

```bash
crew publish <run-id> --pr                 # push crew/<run>/integration, open a PR (body: report)
crew publish <run-id> --pr --draft --review   # draft PR + the Reviewer's findings as a review
crew publish <run-id> --docs               # the final report as a Confluence page / Markdown file
crew publish <run-id> --pr --no-issue      # do not touch the linked issue
```

CREW lists what it is about to do and asks before doing it. If the run was started with
`--issue`, the issue gets a comment with the PR link (and the configured transition).
The PR is a normal PR: review and merge it in GitHub as usual.

## Adding an adapter

Adapters implement the small protocols in `src/crew/integrations/base.py`
(`IssueTracker`, `CodeHost`, `DocsTarget`), use `HttpClient` from
`src/crew/integrations/http.py` (inject a fake transport in tests), and are created in
`src/crew/integrations/factory.py` from a new `kind` in the config schema. See
`tests/unit/test_integrations.py` for the testing pattern; no network or credentials are
needed.
