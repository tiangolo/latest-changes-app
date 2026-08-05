import logging
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from app.config import Settings
from app.models import Installation, InstallationToken, Repository

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"

logger = logging.getLogger(__name__)


class GitHubAPIError(RuntimeError):
    pass


def raise_for_github_status(response: httpx.Response, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        logger.error(
            "GitHub API operation %s failed with status %s",
            operation,
            response.status_code,
        )
        raise GitHubAPIError(
            "GitHub rejected the installation token request"
        ) from error


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
    app_headers = github_headers(create_app_jwt(settings))
    installation_response = client.get(
        f"/repos/{repository.full_name}/installation",
        headers=app_headers,
    )
    raise_for_github_status(installation_response, "get_repository_installation")
    try:
        installation = Installation.model_validate_json(installation_response.content)
    except ValueError as error:
        logger.error("GitHub returned an invalid repository installation response")
        raise GitHubAPIError(
            "GitHub rejected the installation token request"
        ) from error

    token_response = client.post(
        f"/app/installations/{installation.id}/access_tokens",
        headers=app_headers,
        json={
            "repository_ids": [repository.id],
            "permissions": {"contents": "write"},
        },
    )
    raise_for_github_status(token_response, "create_installation_token")
    try:
        token = InstallationToken.model_validate_json(token_response.content)
    except ValueError as error:
        logger.error("GitHub returned an invalid installation token response")
        raise GitHubAPIError(
            "GitHub rejected the installation token request"
        ) from error

    repository_ids = [item.id for item in token.repositories]
    if (
        token.permissions != {"contents": "write", "metadata": "read"}
        or token.repository_selection != "selected"
        or repository_ids != [repository.id]
    ):
        logger.error(
            "GitHub returned an unexpected installation token scope: "
            "permissions=%s repository_selection=%s repository_ids=%s",
            token.permissions,
            token.repository_selection,
            repository_ids,
        )
        raise GitHubAPIError("GitHub rejected the installation token request")
    return token.token


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
