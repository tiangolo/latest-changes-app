# Latest Changes

A GitHub App that updates release notes when a pull request is merged into a repository's default branch.

It looks for the first existing file in this order:

1. `release-notes.md`
2. `docs/release-notes.md`
3. `docs/en/docs/release-notes.md`
4. `CHANGELOG.md`

The file must contain `## Latest Changes`. That section may contain only the `###` sections listed below, with each section appearing at most once.

The first matching pull request label determines the section:

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

If none of these labels match, the entry is added directly under `## Latest Changes`.

Pull requests with the `release` label are skipped.

## Install

You can install the [GitHub App](https://github.com/apps/latest-changes).

It needs the following permissions:

| Permission | Access |
| --- | --- |
| Contents | Read and write |
| Pull requests | Read |
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
