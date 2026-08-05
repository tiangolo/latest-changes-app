import base64
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import (
    MAX_WEBHOOK_BODY_SIZE,
    app,
    get_github_client,
    verify_webhook_signature,
)
from app.models import PullRequest, Repository


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


def test_health(api_client: TestClient) -> None:
    response = api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    sign: Callable[[bytes], str],
    client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client],
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "ghs_token",
                    "expires_at": "2026-07-30T15:00:00Z",
                    "permissions": {"contents": "write"},
                    "repository_selection": "selected",
                    "repositories": [{"id": 75369425}],
                },
                request=request,
            )
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


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "opened"},
        {"pull_request": {"merged": False}},
        {"pull_request": {"base": {"ref": "other"}}},
        {"pull_request": {"labels": [{"name": "release"}]}},
    ],
)
def test_webhook_skips_unapproved_pull_request(
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
            return httpx.Response(
                201,
                json={
                    "token": "ghs_token",
                    "expires_at": "2026-07-30T15:00:00Z",
                    "permissions": {"contents": "write"},
                    "repository_selection": "selected",
                    "repositories": [{"id": 75369425}],
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    body = json.dumps(webhook_payload).encode()
    client_factory(handle)
    response = api_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body),
        },
    )

    assert response.status_code == expected_status
