from datetime import UTC, datetime, timedelta

import httpx
import jwt

from app.config import Settings
from app.models import Installation, InstallationToken, Repository

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"


class GitHubAPIError(RuntimeError):
    pass


def create_app_jwt(settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iat": now - timedelta(seconds=60),
            "exp": now + timedelta(minutes=9),
            "iss": settings.github_client_id,
        },
        settings.github_app_private_key.get_secret_value(),
        algorithm="RS256",
    )


def issue_installation_token(
    repository: Repository,
    settings: Settings,
    client: httpx.Client,
) -> str:
    try:
        app_headers = github_headers(create_app_jwt(settings))
        installation_response = client.get(
            f"/repos/{repository.full_name}/installation",
            headers=app_headers,
        )
        installation_response.raise_for_status()
        installation = Installation.model_validate_json(installation_response.content)

        token_response = client.post(
            f"/app/installations/{installation.id}/access_tokens",
            headers=app_headers,
            json={
                "repository_ids": [repository.id],
                "permissions": {"contents": "write"},
            },
        )
        token_response.raise_for_status()
        token = InstallationToken.model_validate_json(token_response.content)
        if (
            token.permissions != {"contents": "write"}
            or token.repository_selection != "selected"
            or [item.id for item in token.repositories] != [repository.id]
        ):
            raise ValueError("Unexpected installation token scope")
        return token.token
    except (httpx.HTTPError, ValueError) as error:
        raise GitHubAPIError(
            "GitHub rejected the installation token request"
        ) from error


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
