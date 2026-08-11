# Latest Changes

A GitHub App that updates release notes when a pull request is merged into a repository's default branch.

It looks for the first existing file in this order:

1. `release-notes.md`
2. `docs/release-notes.md`
3. `docs/en/docs/release-notes.md`
4. `CHANGELOG.md`

If none of these files exists, the app creates `release-notes.md`.

An existing file must contain `## Latest Changes`. That section may contain only the `###` sections listed below, with each section appearing at most once.

Each pull request must have exactly one Latest Changes label: either one of the section labels below or `release`. A section label determines the section:

| Label | Section |
| --- | --- |
| `breaking` | Breaking Changes |
| `security` | Security Fixes |
| `feature` | Features |
| `bug` | Fixes |
| `refactor` | Refactors |
| `upgrade` | Upgrades |
| `docs` | Docs |
| `lang-all` | Translations |
| `infra` | Infrastructure |
| `internal` | Internal |

Pull requests with the `release` label are skipped. The `release` label is mutually exclusive with the section labels above.

For pull requests targeting the default branch, the app reports a commit status named `latest-changes/label`:

| Matching labels | Status |
| --- | --- |
| None | Pending |
| One section label or `release` | Success |
| More than one without automatic-label configuration | Failure |

You can require this status in a branch rule or ruleset. Configure the Latest Changes GitHub App as the expected source of the status.

## Automatic labels

Add `.github/latest-changes.yml` to enable automatic labeling. Each supported label maps to an ordered list of path rules. A string includes matching paths, while an `exclude` object excludes a matching path from that label. The first matching rule for one path and label wins.

```yaml
auto-labels:
  docs:
    - docs/en/docs/**
    - docs_src/**
  lang-all:
    - exclude: docs/*/**/_*.md
    - docs/*/docs/**
  internal:
    - .github/**
    - scripts/**
    - uv.lock
```

The app selects the highest-priority matching label only when every changed path is classified. An unclassified source or metadata change leaves the pull request pending for a manual label. Renames evaluate both the old and new paths.

Automatic labeling also understands dependency changes in the repository-root `pyproject.toml`. Changes to `project.dependencies` or `project.optional-dependencies` match `upgrade`, while changes to `dependency-groups` match `internal`. Other TOML keys remain unclassified. Use `auto-labels: {}` to enable only these built-in rules.

Automatic selection runs when a pull request is opened and when new commits are pushed. Replacing a supported label manually selects it without rerunning automatic classification. If multiple supported labels are added together, the highest-priority label wins deterministically. A later push may promote the selection to a higher-priority automatic candidate, but never downgrade it.

## Install

You can install the [GitHub App](https://github.com/apps/latest-changes).

It needs the following permissions:

| Permission | Access |
| --- | --- |
| Contents | Read and write |
| Pull requests | Read and write |
| Commit statuses | Read and write |
| Metadata | Read (automatically included) |

The app commits directly to the repository's default branch. If a ruleset prevents direct writes, add the GitHub App to its bypass list with **Always allow**.

## Self Host

If you prefer, you can self host it.

### Deploy

You can deploy it to [FastAPI Cloud](https://fastapicloud.dev):

```bash
uv run fastapi deploy
```

### Create a GitHub App

Create a new [GitHub App](https://github.com/settings/apps).

Enable webhooks, and set the URL to your app:

```text
https://your-app.fastapicloud.dev/webhooks/github
```

Create a webhook secret, e.g. with:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe())"
```

Save it in the webhooks secret field.

Subscribe to the **Pull request** event and configure the repository permissions listed above.

After creating the app, generate and download a private key.

Configure these values in your FastAPI Cloud dashboard:

| Name | Type | Value |
| --- | --- | --- |
| `GITHUB_CLIENT_ID` | Environment variable | The Client ID from the GitHub App settings |
| `GITHUB_APP_PRIVATE_KEY` | Secret | The complete contents of the downloaded private key |
| `GITHUB_WEBHOOK_SECRET` | Secret | The same generated value configured as the webhook secret |

### Install

You can go to your GitHub App settings, to the "Install App" section, and install it to your repositories.

## License

This project is licensed under the terms of the MIT license.
