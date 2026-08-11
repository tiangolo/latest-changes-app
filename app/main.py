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

from app.automatic_labels import (
    AutomaticLabelsConfigError,
    get_automatic_label_candidate,
    get_automatic_labels_config,
    normalize_pull_request_labels,
    select_latest_changes_label,
)
from app.config import Settings, get_settings
from app.github import (
    GITHUB_API_URL,
    LABEL_STATUS_PERMISSIONS,
    GitHubAPIError,
    create_commit_status,
    get_pull_request,
    issue_installation_token,
)
from app.latest_changes import (
    LatestChangesError,
    classify_latest_changes_labels,
    get_latest_changes_label,
    update_latest_changes,
)
from app.models import (
    CommitStatusState,
    PullRequest,
    PullRequestWebhook,
    WebhookResponse,
)

MAX_WEBHOOK_BODY_SIZE = 1_000_000
LABEL_STATUS_ACTIONS = {
    "edited",
    "labeled",
    "opened",
    "reopened",
    "synchronize",
    "unlabeled",
}

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


def update_pull_request_label_status(
    webhook: PullRequestWebhook,
    pull_request: PullRequest,
    token: str,
    github_client: httpx.Client,
) -> tuple[CommitStatusState, str]:
    repository = webhook.repository
    try:
        automatic_config = get_automatic_labels_config(
            repository,
            token,
            github_client,
        )
    except AutomaticLabelsConfigError:
        return "failure", "Invalid .github/latest-changes.yml configuration"

    if automatic_config is None:
        label_status, matching_labels = classify_latest_changes_labels(pull_request)
        if label_status == "pending":
            return label_status, "Waiting for one Latest Changes label"
        if label_status == "success":
            return label_status, f"Latest Changes label: {matching_labels[0]}"
        return label_status, "Multiple Latest Changes labels: " + ", ".join(
            matching_labels
        )

    automatic_candidate = None
    automatic_error = False
    if webhook.action in {"opened", "synchronize"}:
        try:
            automatic_candidate = get_automatic_label_candidate(
                repository,
                pull_request,
                automatic_config,
                token,
                github_client,
            )
        except GitHubAPIError:
            automatic_error = True
            logger.exception(
                "Could not classify automatic labels for %s#%s",
                repository.full_name,
                pull_request.number,
            )
    explicit_label = (
        webhook.label.name
        if webhook.action == "labeled" and webhook.label is not None
        else None
    )
    selected_label = select_latest_changes_label(
        pull_request.labels,
        explicit_label=explicit_label,
        automatic_candidate=automatic_candidate,
    )
    if automatic_error and selected_label is None:
        return "failure", "Could not determine an automatic label"

    try:
        normalize_pull_request_labels(
            repository,
            pull_request,
            selected_label,
            token,
            github_client,
        )
    except GitHubAPIError:
        return "failure", "Could not update Latest Changes labels"
    if selected_label is None:
        return "pending", "Waiting for one Latest Changes label"
    return "success", f"Latest Changes label: {selected_label}"


def process_pull_request_webhook(
    webhook: PullRequestWebhook,
    settings: Settings,
    github_client: httpx.Client,
) -> WebhookResponse:
    pull_request = webhook.pull_request
    repository = webhook.repository
    if pull_request.base.ref != repository.default_branch:
        return WebhookResponse(status="skipped", repository=repository.full_name)

    if webhook.action in LABEL_STATUS_ACTIONS:
        token = issue_installation_token(
            repository,
            settings,
            github_client,
            permissions=LABEL_STATUS_PERMISSIONS,
        )
        current_pull_request = get_pull_request(
            repository,
            pull_request.number,
            token,
            github_client,
        )
        if current_pull_request.base.ref != repository.default_branch:
            return WebhookResponse(status="skipped", repository=repository.full_name)

        label_status, description = update_pull_request_label_status(
            webhook,
            current_pull_request,
            token,
            github_client,
        )
        create_commit_status(
            repository,
            current_pull_request.head.sha,
            label_status,
            description,
            token,
            github_client,
        )
        return WebhookResponse(
            status=label_status,
            repository=repository.full_name,
        )

    if webhook.action != "closed" or not pull_request.merged:
        return WebhookResponse(status="skipped", repository=repository.full_name)

    selected_label = get_latest_changes_label(pull_request)
    if selected_label == "release":
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
