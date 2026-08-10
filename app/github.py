import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from app.config import Settings
from app.models import (
    CommitStatusState,
    Installation,
    InstallationToken,
    PullRequest,
    Repository,
)

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
LABEL_STATUS_CONTEXT = "latest-changes/label"
CONTENTS_PERMISSIONS = {"contents": "write"}
LABEL_STATUS_PERMISSIONS = {
    "pull_requests": "read",
    "statuses": "write",
}

logger = logging.getLogger(__name__)


class GitHubAPIError(RuntimeError):
    pass


def raise_for_github_status(
    response: httpx.Response,
    operation: str,
    repository: Repository,
    error_message: str,
) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        logger.error(
            "GitHub API operation %s failed for %s with status %s",
            operation,
            repository.full_name,
            response.status_code,
        )
        raise GitHubAPIError(error_message) from error


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
    permissions: Mapping[str, str] = CONTENTS_PERMISSIONS,
) -> str:
    app_headers = github_headers(create_app_jwt(settings))
    installation_response = client.get(
        f"/repos/{repository.full_name}/installation",
        headers=app_headers,
    )
    raise_for_github_status(
        installation_response,
        "get_repository_installation",
        repository,
        "GitHub rejected the installation token request",
    )
    try:
        installation = Installation.model_validate_json(installation_response.content)
    except ValueError as error:
        logger.error(
            "GitHub returned an invalid repository installation response for %s",
            repository.full_name,
        )
        raise GitHubAPIError(
            "GitHub rejected the installation token request"
        ) from error

    token_response = client.post(
        f"/app/installations/{installation.id}/access_tokens",
        headers=app_headers,
        json={
            "repository_ids": [repository.id],
            "permissions": dict(permissions),
        },
    )
    raise_for_github_status(
        token_response,
        "create_installation_token",
        repository,
        "GitHub rejected the installation token request",
    )
    try:
        token = InstallationToken.model_validate_json(token_response.content)
    except ValueError as error:
        logger.error(
            "GitHub returned an invalid installation token response for %s",
            repository.full_name,
        )
        raise GitHubAPIError(
            "GitHub rejected the installation token request"
        ) from error

    repository_ids = [item.id for item in token.repositories]
    expected_permissions = {**permissions, "metadata": "read"}
    if (
        token.permissions != expected_permissions
        or token.repository_selection != "selected"
        or repository_ids != [repository.id]
    ):
        logger.error(
            "GitHub returned an unexpected installation token scope: "
            "repository=%s permissions=%s repository_selection=%s repository_ids=%s",
            repository.full_name,
            token.permissions,
            token.repository_selection,
            repository_ids,
        )
        raise GitHubAPIError("GitHub rejected the installation token request")
    return token.token


def get_pull_request(
    repository: Repository,
    number: int,
    token: str,
    client: httpx.Client,
) -> PullRequest:
    response = client.get(
        f"/repos/{repository.full_name}/pulls/{number}",
        headers=github_headers(token),
    )
    raise_for_github_status(
        response,
        "get_pull_request",
        repository,
        "GitHub rejected the pull-request request",
    )
    try:
        return PullRequest.model_validate_json(response.content)
    except ValueError as error:
        logger.error(
            "GitHub returned an invalid pull-request response for %s",
            repository.full_name,
        )
        raise GitHubAPIError("GitHub rejected the pull-request request") from error


def create_commit_status(
    repository: Repository,
    sha: str,
    state: CommitStatusState,
    description: str,
    token: str,
    client: httpx.Client,
) -> None:
    response = client.post(
        f"/repos/{repository.full_name}/statuses/{sha}",
        headers=github_headers(token),
        json={
            "state": state,
            "context": LABEL_STATUS_CONTEXT,
            "description": description,
        },
    )
    raise_for_github_status(
        response,
        "create_commit_status",
        repository,
        "GitHub rejected the commit-status request",
    )


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
