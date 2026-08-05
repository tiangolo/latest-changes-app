# Latest Changes

A GitHub App that updates release notes when a pull request is merged into a repository's default branch. It replaces the `tiangolo/latest-changes` GitHub Action.

It looks for the first existing file in this order:

1. `release-notes.md`
2. `docs/release-notes.md`
3. `docs/en/docs/release-notes.md`
4. `CHANGELOG.md`

The file must contain `## Latest Changes`. The first matching pull request label determines the section:

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

Pull requests with the `release` label are skipped.

The App verifies each webhook signature, requests a short-lived installation token for only the webhook repository, and updates the file through GitHub's Contents API. It requests `contents: write`; GitHub also includes the required `metadata: read` permission. It never clones or executes repository code.

## Development

Set the environment variables:

```dotenv
GITHUB_CLIENT_ID=Iv23exampleClientId
GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=replace-with-a-random-secret
```

Run it locally:

```console
uv run fastapi dev
```

## Deploy

You can deploy to [FastAPI Cloud](https://fastapicloud.dev) or any other hosting provider that supports Python and FastAPI.

```bash
uv run fastapi deploy
```

Then make sure to set the secrets and env vars in the FastAPI Cloud dashboard.

## GitHub App

Configure the App with `Contents: Read and write`, `Pull requests: Read`, the `Pull request` event, and this webhook URL:

```text
https://your-app.fastapicloud.dev/webhooks/github
```

Install it only on the repositories it should update. Add the App to the repository ruleset bypass list when needed.

## License

This project is licensed under the terms of the MIT license.
