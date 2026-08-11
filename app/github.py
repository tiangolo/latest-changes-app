import base64
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import jwt
from pydantic import TypeAdapter

from app.config import Settings
from app.models import (
    CommitStatusState,
    Installation,
    InstallationToken,
    PullRequest,
    PullRequestFile,
    Repository,
    RepositoryFile,
)

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
LABEL_STATUS_CONTEXT = "latest-changes/label"
CONTENTS_PERMISSIONS = {"contents": "write"}
LABEL_STATUS_PERMISSIONS = {
    "contents": "read",
    "pull_requests": "write",
    "statuses": "write",
}
PULL_REQUEST_FILES_PER_PAGE = 100

logger = logging.getLogger(__name__)
pull_request_files_adapter = TypeAdapter(list[PullRequestFile])


class GitHubAPIError(RuntimeError):
    pass


class RepositoryFileContentError(RuntimeError):
    pass


def decode_repository_file(
    repository_file: RepositoryFile,
    *,
    max_size: int,
    description: str,
) -> str:
    if repository_file.size > max_size:
        raise RepositoryFileContentError(f"The {description} file is too large")
    if repository_file.encoding != "base64":
        raise RepositoryFileContentError(
            f"GitHub did not return the {description} contents"
        )
    try:
        encoded_content = "".join(repository_file.content.split())
        return base64.b64decode(encoded_content, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise RepositoryFileContentError(
            f"The {description} file is not valid UTF-8"
        ) from error


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


def get_pull_request_files(
    repository: Repository,
    pull_request: PullRequest,
    token: str,
    client: httpx.Client,
) -> list[PullRequestFile]:
    files: list[PullRequestFile] = []
    page = 1
    while True:
        response = client.get(
            f"/repos/{repository.full_name}/pulls/{pull_request.number}/files",
            headers=github_headers(token),
            params={"per_page": PULL_REQUEST_FILES_PER_PAGE, "page": page},
        )
        raise_for_github_status(
            response,
            "get_pull_request_files",
            repository,
            "GitHub rejected the pull-request files request",
        )
        try:
            page_files = pull_request_files_adapter.validate_json(response.content)
        except ValueError as error:
            logger.error(
                "GitHub returned an invalid pull-request files response for %s",
                repository.full_name,
            )
            raise GitHubAPIError(
                "GitHub rejected the pull-request files request"
            ) from error
        files.extend(page_files)
        if len(page_files) < PULL_REQUEST_FILES_PER_PAGE:
            break
        page += 1

    if (
        pull_request.changed_files is not None
        and len(files) != pull_request.changed_files
    ):
        logger.error(
            "GitHub returned an incomplete pull-request files response for %s: "
            "expected=%s actual=%s",
            repository.full_name,
            pull_request.changed_files,
            len(files),
        )
        raise GitHubAPIError("GitHub rejected the pull-request files request")
    return files


def get_repository_file(
    repository: Repository,
    path: str,
    ref: str,
    token: str,
    client: httpx.Client,
) -> RepositoryFile | None:
    response = client.get(
        f"/repos/{repository.full_name}/contents/{path}",
        headers=github_headers(token),
        params={"ref": ref},
    )
    if response.status_code == 404:
        return None
    raise_for_github_status(
        response,
        "get_repository_file",
        repository,
        "GitHub rejected the repository file request",
    )
    try:
        return RepositoryFile.model_validate_json(response.content)
    except ValueError as error:
        logger.error(
            "GitHub returned an invalid repository file response for %s",
            repository.full_name,
        )
        raise GitHubAPIError("GitHub rejected the repository file request") from error


def add_pull_request_label(
    repository: Repository,
    pull_request: PullRequest,
    label: str,
    token: str,
    client: httpx.Client,
) -> None:
    response = client.post(
        f"/repos/{repository.full_name}/issues/{pull_request.number}/labels",
        headers=github_headers(token),
        json={"labels": [label]},
    )
    raise_for_github_status(
        response,
        "add_pull_request_label",
        repository,
        "GitHub rejected the pull-request label update",
    )


def remove_pull_request_label(
    repository: Repository,
    pull_request: PullRequest,
    label: str,
    token: str,
    client: httpx.Client,
) -> None:
    response = client.delete(
        f"/repos/{repository.full_name}/issues/{pull_request.number}/labels/"
        f"{quote(label, safe='')}",
        headers=github_headers(token),
    )
    if response.status_code == 404:
        return
    raise_for_github_status(
        response,
        "remove_pull_request_label",
        repository,
        "GitHub rejected the pull-request label update",
    )


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
