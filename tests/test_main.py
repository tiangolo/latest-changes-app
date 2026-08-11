import base64
import json
import logging
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.automatic_labels import AutomaticLabelsConfig, AutomaticLabelsConfigError
from app.config import Settings, get_settings
from app.github import LABEL_STATUS_PERMISSIONS, GitHubAPIError
from app.main import (
    MAX_WEBHOOK_BODY_SIZE,
    app,
    get_github_client,
    verify_webhook_signature,
)
from app.models import PullRequest, Repository

EMPTY_AUTOMATIC_LABELS_CONFIG = AutomaticLabelsConfig.model_validate(
    {"auto-labels": {}}
)


@pytest.fixture
def api_client(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("GITHUB_CLIENT_ID", settings.github_client_id)
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY",
        settings.github_app_private_key.get_secret_value(),
    )
    monkeypatch.setenv(
        "GITHUB_WEBHOOK_SECRET",
        settings.github_webhook_secret.get_secret_value(),
    )
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()
        app.dependency_overrides.clear()


@pytest.fixture
def webhook_payload(
    pull_request: PullRequest,
    repository: Repository,
) -> dict[str, Any]:
    return {
        "action": "closed",
        "pull_request": pull_request.model_dump(),
        "repository": repository.model_dump(),
    }


@pytest.fixture
def client_factory(
    settings: Settings,
) -> Iterator[Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]]:
    clients: list[httpx.Client] = []

    def create(handle: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
        github_client = httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        )
        clients.append(github_client)
        app.dependency_overrides[get_github_client] = lambda: github_client
        return github_client

    yield create
    for client in clients:
        client.close()


def installation_token_response(
    request: httpx.Request,
    permissions: dict[str, str],
) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "token": "ghs_token",
            "expires_at": "2026-07-30T15:00:00Z",
            "permissions": {**permissions, "metadata": "read"},
            "repository_selection": "selected",
            "repositories": [{"id": 75369425}],
        },
        request=request,
    )


def post_label_webhook(
    webhook_payload: dict[str, Any],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> tuple[Any, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        if request.url.path.endswith("/access_tokens"):
            return installation_token_response(request, LABEL_STATUS_PERMISSIONS)
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(
                200,
                json=webhook_payload["pull_request"],
                request=request,
            )
        return httpx.Response(201, request=request)

    body = json.dumps(webhook_payload).encode()
    client_factory(handle)
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )
    return response, requests


def test_root(api_client: TestClient) -> None:
    response = api_client.get("/")

    assert response.status_code == 200
    assert response.json() == {"name": app.title, "version": app.version}


def test_health(api_client: TestClient) -> None:
    response = api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_version(api_client: TestClient) -> None:
    response = api_client.get("/openapi.json")

    with Path("pyproject.toml").open("rb") as pyproject_file:
        project_version = tomllib.load(pyproject_file)["project"]["version"]
    assert response.status_code == 200
    assert response.json()["info"]["version"] == project_version


def test_webhook_route_requires_github_headers(api_client: TestClient) -> None:
    response = api_client.post("/webhooks/github", content=b"{}")

    assert response.status_code == 422


def test_verify_webhook_signature_uses_github_test_vector() -> None:
    verify_webhook_signature(
        b"Hello, World!",
        "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17",
        "It's a Secret to Everybody",
    )


def test_webhook_updates_release_notes(
    webhook_payload: dict[str, Any],
    api_client: TestClient,
    caplog: pytest.LogCaptureFixture,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    caplog.set_level(logging.INFO, logger="app.main")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        if request.url.path.endswith("/access_tokens"):
            return installation_token_response(request, {"contents": "write"})
        if request.method == "GET":
            content = "## Latest Changes\n"
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "path": "release-notes.md",
                    "sha": "blob-sha",
                    "size": len(content),
                    "encoding": "base64",
                    "content": base64.b64encode(content.encode()).decode(),
                },
                request=request,
            )
        return httpx.Response(200, json={"content": {}}, request=request)

    body = json.dumps(webhook_payload).encode()
    client_factory(handle)
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "updated",
        "repository": "fastapi/fastapi",
        "path": "release-notes.md",
    }
    update = json.loads(requests[-1].read())
    assert update["sha"] == "blob-sha"
    assert update["branch"] == "master"
    assert "### Features" in base64.b64decode(update["content"]).decode()
    assert "delivery test-delivery event=pull_request" in caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"pull_request": {"merged": False}},
        {"pull_request": {"base": {"ref": "other", "sha": "base-sha"}}},
        {"pull_request": {"labels": [{"name": "release"}]}},
    ],
)
def test_webhook_skips_pull_request_without_release_notes_update(
    webhook_payload: dict[str, Any],
    changes: dict[str, Any],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    for key, value in changes.items():
        if isinstance(value, dict):
            webhook_payload[key].update(value)
        else:
            webhook_payload[key] = value

    body = json.dumps(webhook_payload).encode()
    client_factory(lambda request: pytest.fail("Unexpected GitHub call"))
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "skipped",
        "repository": "fastapi/fastapi",
        "path": None,
    }


@pytest.mark.parametrize(
    ("labels", "expected_status", "expected_description"),
    [
        ([], "pending", "Waiting for one Latest Changes label"),
        ([{"name": "feature"}], "success", "Latest Changes label: feature"),
        (
            [{"name": "bug"}, {"name": "feature"}],
            "failure",
            "Multiple Latest Changes labels: feature, bug",
        ),
        ([{"name": "release"}], "success", "Latest Changes label: release"),
    ],
)
def test_webhook_reports_latest_changes_label_status(
    webhook_payload: dict[str, Any],
    labels: list[dict[str, str]],
    expected_status: str,
    expected_description: str,
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    webhook_payload["action"] = "opened"
    webhook_payload["pull_request"]["labels"] = labels
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        if request.url.path.endswith("/access_tokens"):
            return installation_token_response(
                request,
                LABEL_STATUS_PERMISSIONS,
            )
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(
                200,
                json=webhook_payload["pull_request"],
                request=request,
            )
        if "/contents/.github/latest-changes.yml" in request.url.path:
            return httpx.Response(404, request=request)
        return httpx.Response(201, request=request)

    body = json.dumps(webhook_payload).encode()
    client_factory(handle)
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": expected_status,
        "repository": "fastapi/fastapi",
        "path": None,
    }
    token_request = json.loads(requests[1].read())
    assert token_request["permissions"] == LABEL_STATUS_PERMISSIONS
    pull_request_request = next(
        request for request in requests if request.url.path.endswith("/pulls/42")
    )
    assert pull_request_request.headers["Authorization"] == "Bearer ghs_token"
    status_request = requests[-1]
    assert status_request.url.path == "/repos/fastapi/fastapi/statuses/head-sha"
    assert json.loads(status_request.read()) == {
        "state": expected_status,
        "context": "latest-changes/label",
        "description": expected_description,
    }


def test_webhook_applies_automatic_label_on_open(
    webhook_payload: dict[str, Any],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook_payload["action"] = "opened"
    webhook_payload["pull_request"]["labels"] = []
    normalized: list[str | None] = []
    monkeypatch.setattr(
        main_module,
        "get_automatic_labels_config",
        lambda *args: EMPTY_AUTOMATIC_LABELS_CONFIG,
    )
    monkeypatch.setattr(
        main_module,
        "get_automatic_label_candidate",
        lambda *args: "docs",
    )
    monkeypatch.setattr(
        main_module,
        "normalize_pull_request_labels",
        lambda repository, pull_request, selected, token, client: normalized.append(
            selected
        ),
    )

    response, requests = post_label_webhook(
        webhook_payload,
        api_client,
        sign,
        client_factory,
    )

    assert response.json()["status"] == "success"
    assert normalized == ["docs"]
    assert json.loads(requests[-1].read())["description"] == (
        "Latest Changes label: docs"
    )


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ([{"name": "internal"}], "internal"),
        ([{"name": "feature"}, {"name": "internal"}], "feature"),
    ],
)
def test_webhook_respects_manual_labels_without_reclassifying(
    webhook_payload: dict[str, Any],
    labels: list[dict[str, str]],
    expected: str,
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook_payload["action"] = "labeled"
    webhook_payload["label"] = {"name": "internal"}
    webhook_payload["pull_request"]["labels"] = labels
    normalized: list[str | None] = []
    monkeypatch.setattr(
        main_module,
        "get_automatic_labels_config",
        lambda *args: EMPTY_AUTOMATIC_LABELS_CONFIG,
    )
    monkeypatch.setattr(
        main_module,
        "get_automatic_label_candidate",
        lambda *args: pytest.fail("Label events must not reclassify files"),
    )
    monkeypatch.setattr(
        main_module,
        "normalize_pull_request_labels",
        lambda repository, pull_request, selected, token, client: normalized.append(
            selected
        ),
    )

    response, _requests = post_label_webhook(
        webhook_payload,
        api_client,
        sign,
        client_factory,
    )

    assert response.json()["status"] == "success"
    assert normalized == [expected]


def test_webhook_reports_pending_after_label_removal(
    webhook_payload: dict[str, Any],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook_payload["action"] = "unlabeled"
    webhook_payload["pull_request"]["labels"] = []
    monkeypatch.setattr(
        main_module,
        "get_automatic_labels_config",
        lambda *args: EMPTY_AUTOMATIC_LABELS_CONFIG,
    )

    response, requests = post_label_webhook(
        webhook_payload,
        api_client,
        sign,
        client_factory,
    )

    assert response.json()["status"] == "pending"
    assert json.loads(requests[-1].read())["state"] == "pending"


@pytest.mark.parametrize(
    ("labels", "expected_status"),
    [([], "failure"), ([{"name": "feature"}], "success")],
)
def test_webhook_handles_automatic_classification_error(
    webhook_payload: dict[str, Any],
    labels: list[dict[str, str]],
    expected_status: str,
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook_payload["action"] = "synchronize"
    webhook_payload["pull_request"]["labels"] = labels
    monkeypatch.setattr(
        main_module,
        "get_automatic_labels_config",
        lambda *args: EMPTY_AUTOMATIC_LABELS_CONFIG,
    )

    def fail(*args: object) -> None:
        raise GitHubAPIError("files unavailable")

    monkeypatch.setattr(main_module, "get_automatic_label_candidate", fail)

    response, _requests = post_label_webhook(
        webhook_payload,
        api_client,
        sign,
        client_factory,
    )

    assert response.json()["status"] == expected_status


def test_webhook_reports_invalid_automatic_configuration(
    webhook_payload: dict[str, Any],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook_payload["action"] = "opened"

    def fail(*args: object) -> None:
        raise AutomaticLabelsConfigError("invalid")

    monkeypatch.setattr(main_module, "get_automatic_labels_config", fail)

    response, requests = post_label_webhook(
        webhook_payload,
        api_client,
        sign,
        client_factory,
    )

    assert response.json()["status"] == "failure"
    assert json.loads(requests[-1].read())["description"] == (
        "Invalid .github/latest-changes.yml configuration"
    )


def test_webhook_reports_label_mutation_error(
    webhook_payload: dict[str, Any],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook_payload["action"] = "reopened"
    monkeypatch.setattr(
        main_module,
        "get_automatic_labels_config",
        lambda *args: EMPTY_AUTOMATIC_LABELS_CONFIG,
    )

    def fail(*args: object) -> None:
        raise GitHubAPIError("mutation failed")

    monkeypatch.setattr(main_module, "normalize_pull_request_labels", fail)

    response, requests = post_label_webhook(
        webhook_payload,
        api_client,
        sign,
        client_factory,
    )

    assert response.json()["status"] == "failure"
    assert json.loads(requests[-1].read())["description"] == (
        "Could not update Latest Changes labels"
    )


def test_webhook_skips_status_if_current_base_is_not_default(
    webhook_payload: dict[str, Any],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    webhook_payload["action"] = "edited"
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        if request.url.path.endswith("/access_tokens"):
            return installation_token_response(
                request,
                LABEL_STATUS_PERMISSIONS,
            )
        current_pull_request = {
            **webhook_payload["pull_request"],
            "base": {"ref": "other", "sha": "base-sha"},
        }
        return httpx.Response(200, json=current_pull_request, request=request)

    body = json.dumps(webhook_payload).encode()
    client_factory(handle)
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"
    assert requests[-1].url.path.endswith("/pulls/42")


@pytest.mark.parametrize(
    "labels",
    [[], [{"name": "feature"}, {"name": "bug"}]],
)
def test_webhook_rejects_merged_pull_request_with_invalid_labels(
    webhook_payload: dict[str, Any],
    labels: list[dict[str, str]],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    webhook_payload["pull_request"]["labels"] = labels
    body = json.dumps(webhook_payload).encode()
    client_factory(lambda request: pytest.fail("Unexpected GitHub call"))

    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == 422


def test_webhook_skips_other_event(
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    body = b"{}"
    client_factory(lambda request: pytest.fail("Unexpected GitHub call"))
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "skipped",
        "repository": None,
        "path": None,
    }


def test_webhook_rejects_invalid_signature(
    api_client: TestClient,
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    client_factory(lambda request: pytest.fail("Unexpected GitHub call"))
    response = api_client.post(
        "/webhooks/github",
        content=b"{}",
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    )

    assert response.status_code == 403


def test_webhook_rejects_invalid_payload(
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    body = b"{}"
    client_factory(lambda request: pytest.fail("Unexpected GitHub call"))
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == 400


def test_webhook_rejects_large_payload(
    api_client: TestClient,
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    client_factory(lambda request: pytest.fail("Unexpected GitHub call"))
    response = api_client.post(
        "/webhooks/github",
        content=b"x" * (MAX_WEBHOOK_BODY_SIZE + 1),
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "not-checked-before-size-limit",
        },
    )

    assert response.status_code == 413


@pytest.mark.parametrize(
    ("github_status", "expected_status"),
    [(500, 502), (200, 422)],
)
def test_webhook_reports_processing_error(
    webhook_payload: dict[str, Any],
    api_client: TestClient,
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
    github_status: int,
    expected_status: int,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if github_status == 500:
            return httpx.Response(500, request=request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        if request.url.path.endswith("/access_tokens"):
            return installation_token_response(request, {"contents": "write"})
        content = "# Release Notes\n"
        return httpx.Response(
            200,
            json={
                "type": "file",
                "path": "release-notes.md",
                "sha": "blob-sha",
                "size": len(content),
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
            },
            request=request,
        )

    body = json.dumps(webhook_payload).encode()
    client_factory(handle)
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "test-delivery",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == expected_status
