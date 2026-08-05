import hashlib
import hmac
import logging
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.github import GITHUB_API_URL, GitHubAPIError, issue_installation_token
from app.latest_changes import LatestChangesError, update_latest_changes
from app.models import PullRequestWebhook, WebhookResponse

MAX_WEBHOOK_BODY_SIZE = 1_000_000

logger = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]

with (Path(__file__).parent.parent / "pyproject.toml").open("rb") as pyproject_file:
    app_version = tomllib.load(pyproject_file)["project"]["version"]

app = FastAPI(
    title="Latest Changes",
    version=app_version,
)


def get_github_client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=GITHUB_API_URL, timeout=10) as client:
        yield client


GitHubClientDep = Annotated[
    httpx.Client,
    Depends(get_github_client, scope="function"),
]


@app.get("/", tags=["system"])
def root() -> dict[str, str]:
    return {"name": app.title, "version": app.version}


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> None:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The GitHub webhook signature is invalid",
        )


def process_pull_request_webhook(
    webhook: PullRequestWebhook,
    settings: Settings,
    github_client: httpx.Client,
) -> WebhookResponse:
    pull_request = webhook.pull_request
    repository = webhook.repository
    if (
        webhook.action != "closed"
        or not pull_request.merged
        or pull_request.base.ref != repository.default_branch
    ):
        return WebhookResponse(status="skipped", repository=repository.full_name)

    labels = {label.name for label in pull_request.labels}
    if "release" in labels:
        return WebhookResponse(status="skipped", repository=repository.full_name)

    token = issue_installation_token(repository, settings, github_client)
    update_status, path = update_latest_changes(
        repository,
        pull_request,
        token,
        github_client,
    )
    return WebhookResponse(
        status=update_status,
        repository=repository.full_name,
        path=path,
    )


async def get_webhook(
    request: Request,
    settings: SettingsDep,
    github_delivery: Annotated[str, Header(alias="X-GitHub-Delivery")],
    github_event: Annotated[str, Header(alias="X-GitHub-Event")],
    github_signature: Annotated[str, Header(alias="X-Hub-Signature-256")],
) -> PullRequestWebhook | None:
    body_parts: list[bytes] = []
    body_size = 0
    async for part in request.stream():
        body_size += len(part)
        if body_size > MAX_WEBHOOK_BODY_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="The GitHub webhook payload is too large",
            )
        body_parts.append(part)
    body = b"".join(body_parts)
    verify_webhook_signature(
        body,
        github_signature,
        settings.github_webhook_secret.get_secret_value(),
    )
    logger.info(
        "Received GitHub webhook delivery %s event=%s",
        github_delivery,
        github_event,
    )
    if github_event != "pull_request":
        return None
    try:
        return PullRequestWebhook.model_validate_json(body)
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The GitHub webhook payload is invalid",
        ) from error


GitHubWebhookDep = Annotated[PullRequestWebhook | None, Depends(get_webhook)]


@app.post("/webhooks/github")
def github_webhook(
    webhook: GitHubWebhookDep,
    settings: SettingsDep,
    github_client: GitHubClientDep,
) -> WebhookResponse:
    if webhook is None:
        return WebhookResponse(status="skipped")
    try:
        return process_pull_request_webhook(
            webhook,
            settings,
            github_client,
        )
    except GitHubAPIError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error
    except LatestChangesError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
