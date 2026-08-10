import base64
import json

import httpx
import jwt
import pytest

from app.config import Settings
from app.github import (
    GitHubAPIError,
    create_commit_status,
    get_pull_request,
    issue_installation_token,
)
from app.latest_changes import (
    LatestChangesError,
    classify_latest_changes_labels,
    decode_file,
    generate_latest_changes,
    get_latest_changes_file,
    update_latest_changes,
)
from app.models import Label, PullRequest, Repository, RepositoryFile


def repository_file(content: str, **updates: object) -> RepositoryFile:
    data: dict[str, object] = {
        "type": "file",
        "path": "release-notes.md",
        "sha": "blob-sha",
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content.encode()).decode(),
    }
    data.update(updates)
    return RepositoryFile.model_validate(data)


def test_issue_installation_token_is_repository_scoped(
    settings: Settings,
    repository: Repository,
    public_key: str,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        return httpx.Response(
            201,
            json={
                "token": "ghs_secret",
                "expires_at": "2026-07-30T15:00:00Z",
                "permissions": {"contents": "write", "metadata": "read"},
                "repository_selection": "selected",
                "repositories": [{"id": 75369425}],
            },
            request=request,
        )

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        token = issue_installation_token(repository, settings, client)

    assert token == "ghs_secret"
    assert requests[0].url.path == "/repos/fastapi/fastapi/installation"
    assert requests[0].headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert requests[1].read() == (
        b'{"repository_ids":[75369425],"permissions":{"contents":"write"}}'
    )
    encoded_jwt = requests[0].headers["Authorization"].removeprefix("Bearer ")
    payload = jwt.decode(
        encoded_jwt,
        public_key,
        algorithms=["RS256"],
        options={"verify_aud": False},
    )
    assert payload["iss"] == "Iv23exampleClientId"


@pytest.mark.parametrize("response_type", ["error", "invalid"])
def test_get_pull_request_reports_error(
    repository: Repository,
    caplog: pytest.LogCaptureFixture,
    response_type: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if response_type == "error":
            return httpx.Response(500, request=request)
        return httpx.Response(200, json={}, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="pull-request request"),
    ):
        get_pull_request(repository, 42, "token", client)

    if response_type == "invalid":
        assert "invalid pull-request response" in caplog.text


def test_create_commit_status_reports_error(repository: Repository) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="commit-status request"),
    ):
        create_commit_status(
            repository,
            "head-sha",
            "success",
            "Latest Changes label: feature",
            "token",
            client,
        )


@pytest.mark.parametrize(
    ("token_data", "repositories"),
    [
        (
            {
                "permissions": {"contents": "read", "metadata": "read"},
                "repository_selection": "selected",
            },
            [{"id": 75369425}],
        ),
        (
            {
                "permissions": {"contents": "write", "metadata": "read"},
                "repository_selection": "all",
            },
            [{"id": 75369425}],
        ),
        (
            {
                "permissions": {"contents": "write", "metadata": "read"},
                "repository_selection": "selected",
            },
            [{"id": 1}],
        ),
    ],
)
def test_issue_installation_token_rejects_unexpected_scope(
    settings: Settings,
    repository: Repository,
    caplog: pytest.LogCaptureFixture,
    token_data: dict[str, object],
    repositories: list[dict[str, int]],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        return httpx.Response(
            201,
            json={
                "token": "ghs_secret",
                "expires_at": "2026-07-30T15:00:00Z",
                **token_data,
                "repositories": repositories,
            },
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="installation token"),
    ):
        issue_installation_token(repository, settings, client)

    assert "unexpected installation token scope" in caplog.text


def test_issue_installation_token_reports_installation_lookup_error(
    settings: Settings,
    repository: Repository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="installation token"),
    ):
        issue_installation_token(repository, settings, client)

    assert (
        "get_repository_installation failed for fastapi/fastapi with status 404"
        in caplog.text
    )


def test_issue_installation_token_reports_token_request_error(
    settings: Settings,
    repository: Repository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 987}, request=request)
        return httpx.Response(403, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="installation token"),
    ):
        issue_installation_token(repository, settings, client)

    assert (
        "create_installation_token failed for fastapi/fastapi with status 403"
        in caplog.text
    )


@pytest.mark.parametrize(
    ("invalid_response", "expected_log"),
    [
        ("installation", "invalid repository installation response"),
        ("token", "invalid installation token response"),
    ],
)
def test_issue_installation_token_reports_invalid_response(
    settings: Settings,
    repository: Repository,
    caplog: pytest.LogCaptureFixture,
    invalid_response: str,
    expected_log: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            data = {} if invalid_response == "installation" else {"id": 987}
            return httpx.Response(200, json=data, request=request)
        return httpx.Response(201, json={}, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="installation token"),
    ):
        issue_installation_token(repository, settings, client)

    assert expected_log in caplog.text


@pytest.mark.parametrize(
    "existing_path",
    [
        "release-notes.md",
        "docs/release-notes.md",
        "docs/en/docs/release-notes.md",
        "CHANGELOG.md",
    ],
)
def test_get_latest_changes_file_uses_first_existing_path(
    repository: Repository,
    existing_path: str,
) -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/repos/fastapi/fastapi/contents/")
        paths.append(path)
        if path != existing_path:
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            json=repository_file(
                "## Latest Changes\n", path=existing_path
            ).model_dump(),
            request=request,
        )

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        result = get_latest_changes_file(repository, "token", client)

    supported_paths = [
        "release-notes.md",
        "docs/release-notes.md",
        "docs/en/docs/release-notes.md",
        "CHANGELOG.md",
    ]
    assert result is not None
    assert result.path == existing_path
    assert paths == supported_paths[: supported_paths.index(existing_path) + 1]


def test_get_latest_changes_file_returns_none_when_missing(
    repository: Repository,
) -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(missing),
    ) as client:
        result = get_latest_changes_file(repository, "token", client)

    assert result is None


def test_get_latest_changes_file_rejects_large_file(
    repository: Repository,
) -> None:

    def large_without_content(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "path": "release-notes.md",
                "sha": "blob-sha",
                "size": 1_000_001,
                "encoding": "none",
                "content": "",
            },
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(large_without_content),
        ) as client,
        pytest.raises(LatestChangesError, match="too large"),
    ):
        get_latest_changes_file(repository, "token", client)


def test_get_latest_changes_file_rejects_missing_content(
    repository: Repository,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=repository_file("content")
            .model_copy(update={"encoding": "none", "content": ""})
            .model_dump(),
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="did not return"),
    ):
        get_latest_changes_file(repository, "token", client)

    def large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=repository_file("content", size=1_000_001).model_dump(),
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(large),
        ) as client,
        pytest.raises(LatestChangesError, match="too large"),
    ):
        get_latest_changes_file(repository, "token", client)


def test_get_latest_changes_file_hides_invalid_response(
    repository: Repository,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="release-notes request"),
    ):
        get_latest_changes_file(repository, "token", client)


def test_decode_file_rejects_invalid_content() -> None:
    invalid = repository_file("content").model_copy(update={"content": "not-base64"})

    with pytest.raises(LatestChangesError, match="valid UTF-8"):
        decode_file(invalid)


def test_decode_file_accepts_wrapped_base64() -> None:
    wrapped = repository_file("content").model_copy(
        update={"content": "Y29u\ndGVudA=="}
    )

    assert decode_file(wrapped) == "content"


def test_generate_latest_changes_uses_predefined_label_order(
    pull_request: PullRequest,
) -> None:
    content = """# Release Notes

## Latest Changes

### Fixes

* Existing fix.

### Internal

* Existing internal change.

## 1.0.0
"""

    result = generate_latest_changes(content, pull_request)

    assert (
        result
        == """# Release Notes

## Latest Changes

### Features

* Add the feature. PR [#42](https://github.com/fastapi/fastapi/pull/42) by [@contributor](https://github.com/contributor).

### Fixes

* Existing fix.

### Internal

* Existing internal change.

## 1.0.0
"""
    )
    assert generate_latest_changes(result, pull_request) == result


def test_generate_latest_changes_rejects_missing_known_label(
    pull_request: PullRequest,
) -> None:
    unlabeled = pull_request.model_copy(update={"labels": []})

    with pytest.raises(LatestChangesError, match="no recognized"):
        generate_latest_changes("## Latest Changes\n", unlabeled)


def test_generate_latest_changes_in_empty_release_before_history(
    pull_request: PullRequest,
) -> None:
    content = """# Release Notes

## Latest Changes

## 1.0.0

### Features

* Historical feature.
"""

    result = generate_latest_changes(content, pull_request)

    assert (
        result
        == """# Release Notes

## Latest Changes

### Features

* Add the feature. PR [#42](https://github.com/fastapi/fastapi/pull/42) by [@contributor](https://github.com/contributor).

## 1.0.0

### Features

* Historical feature.
"""
    )


def test_generate_latest_changes_preserves_uncategorized_entries(
    pull_request: PullRequest,
) -> None:
    content = """## Latest Changes

* Existing uncategorized change.

## 1.0.0
"""

    result = generate_latest_changes(content, pull_request)

    assert (
        result
        == """## Latest Changes

* Existing uncategorized change.

### Features

* Add the feature. PR [#42](https://github.com/fastapi/fastapi/pull/42) by [@contributor](https://github.com/contributor).

## 1.0.0
"""
    )


def test_generate_latest_changes_preserves_mixed_uncategorized_and_sections(
    pull_request: PullRequest,
) -> None:
    content = """## Latest Changes

* Existing uncategorized change.

### Features

* Existing feature.

### Docs

* Existing documentation change.

## 1.0.0
"""

    result = generate_latest_changes(content, pull_request)

    assert (
        result
        == """## Latest Changes

* Existing uncategorized change.

### Features

* Add the feature. PR [#42](https://github.com/fastapi/fastapi/pull/42) by [@contributor](https://github.com/contributor).
* Existing feature.

### Docs

* Existing documentation change.

## 1.0.0
"""
    )


def test_generate_latest_changes_rejects_multiple_matching_labels(
    pull_request: PullRequest,
) -> None:
    multiple_labels = pull_request.model_copy(
        update={"labels": [Label(name="bug"), Label(name="feature")]}
    )

    with pytest.raises(LatestChangesError, match="multiple Latest Changes labels"):
        generate_latest_changes("## Latest Changes\n", multiple_labels)


def test_generate_latest_changes_rejects_release_label(
    pull_request: PullRequest,
) -> None:
    release = pull_request.model_copy(update={"labels": [Label(name="release")]})

    with pytest.raises(LatestChangesError, match="must not update"):
        generate_latest_changes("## Latest Changes\n", release)


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ([], ("pending", ())),
        (["unrelated"], ("pending", ())),
        (["feature", "unrelated"], ("success", ("feature",))),
        (["bug", "feature"], ("failure", ("feature", "bug"))),
        (["release"], ("success", ("release",))),
    ],
)
def test_classify_latest_changes_labels(
    pull_request: PullRequest,
    labels: list[str],
    expected: tuple[str, tuple[str, ...]],
) -> None:
    updated = pull_request.model_copy(
        update={"labels": [Label(name=label) for label in labels]}
    )

    assert classify_latest_changes_labels(updated) == expected


def test_generate_latest_changes_reorders_existing_sections(
    pull_request: PullRequest,
) -> None:
    docs_pull_request = pull_request.model_copy(update={"labels": [Label(name="docs")]})
    content = """## Latest Changes

### Internal

* Existing internal change.

### Fixes

* Existing fix.
"""

    result = generate_latest_changes(content, docs_pull_request)

    assert result.index("### Fixes") < result.index("### Docs")
    assert result.index("### Docs") < result.index("### Internal")
    assert "* Existing fix." in result
    assert "* Existing internal change." in result


def test_generate_latest_changes_preserves_surrounding_document(
    pull_request: PullRequest,
) -> None:
    content = """# Release Notes

Introductory content.

## Documentation

### Features

This section is not part of the release notes.

## Latest Changes

## License

Released under the MIT License.
"""

    result = generate_latest_changes(content, pull_request)

    assert (
        result
        == """# Release Notes

Introductory content.

## Documentation

### Features

This section is not part of the release notes.

## Latest Changes

### Features

* Add the feature. PR [#42](https://github.com/fastapi/fastapi/pull/42) by [@contributor](https://github.com/contributor).

## License

Released under the MIT License.
"""
    )


def test_generate_latest_changes_preserves_multiple_historical_releases(
    pull_request: PullRequest,
) -> None:
    content = """## Latest Changes

### Fixes

* Current fix.

## 2.0.0

### Features

* Version 2 feature.

## 1.0.0

### Internal

* Version 1 internal change.
"""
    history = content[content.index("## 2.0.0") :]

    result = generate_latest_changes(content, pull_request)

    assert result[result.index("## 2.0.0") :] == history


def test_generate_latest_changes_is_idempotent_across_document(
    pull_request: PullRequest,
) -> None:
    entry = "* Add the feature. PR [#42](https://github.com/fastapi/fastapi/pull/42) by [@contributor](https://github.com/contributor)."
    content = f"""## Latest Changes

## 1.0.0

### Features

{entry}
"""

    assert generate_latest_changes(content, pull_request) == content


def test_generate_latest_changes_normalizes_line_endings_and_blank_space(
    pull_request: PullRequest,
) -> None:
    content = "# Release Notes\r\n\r\n## Latest Changes\r\n   \r\n## 1.0.0"

    result = generate_latest_changes(content, pull_request)

    assert "\r" not in result
    assert result.endswith("## 1.0.0\n")
    assert "### Features" in result


def test_generate_latest_changes_preserves_markdown_content(
    pull_request: PullRequest,
) -> None:
    markdown_pull_request = pull_request.model_copy(
        update={"title": "Fix `[link](https://example.com)` and `code`"}
    )
    content = """## Latest Changes

### Features

* Existing change by [@dependabot[bot]](https://github.com/apps/dependabot).
"""

    result = generate_latest_changes(content, markdown_pull_request)

    assert "* Fix `[link](https://example.com)` and `code`. PR [#42]" in result
    assert "[@dependabot[bot]]" in result


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("# Release Notes\n", "no '## Latest Changes'"),
        (
            "## Latest Changes\n\n### Other\n\nText\n",
            "Unsupported latest-changes section",
        ),
        (
            "## Latest Changes\n\n### Docs\n\nFirst.\n\n### Docs\n\nSecond.\n",
            "Duplicate latest-changes section",
        ),
    ],
)
def test_generate_latest_changes_rejects_unsupported_content(
    pull_request: PullRequest,
    content: str,
    message: str,
) -> None:
    with pytest.raises(LatestChangesError, match=message):
        generate_latest_changes(content, pull_request)


def test_update_latest_changes_retries_stale_sha(
    repository: Repository,
    pull_request: PullRequest,
) -> None:
    put_count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal put_count
        if request.method == "GET":
            return httpx.Response(
                200,
                json=repository_file("## Latest Changes\n").model_dump(),
                request=request,
            )
        put_count += 1
        return httpx.Response(409 if put_count == 1 else 200, request=request)

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        result = update_latest_changes(repository, pull_request, "token", client)

    assert result == ("updated", "release-notes.md")
    assert put_count == 2


@pytest.mark.parametrize("conflict_status", [409, 422])
def test_update_latest_changes_creates_missing_file_and_retries_conflict(
    repository: Repository,
    pull_request: PullRequest,
    conflict_status: int,
) -> None:
    requests: list[httpx.Request] = []
    file_exists = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal file_exists
        requests.append(request)
        if request.method == "GET":
            if file_exists and request.url.path.endswith("/release-notes.md"):
                return httpx.Response(
                    200,
                    json=repository_file("## Latest Changes\n").model_dump(),
                    request=request,
                )
            return httpx.Response(404, request=request)
        file_exists = True
        if sum(item.method == "PUT" for item in requests) == 1:
            return httpx.Response(conflict_status, request=request)
        return httpx.Response(200, request=request)

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        result = update_latest_changes(repository, pull_request, "token", client)

    updates = [
        json.loads(request.read()) for request in requests if request.method == "PUT"
    ]
    assert result == ("updated", "release-notes.md")
    assert "sha" not in updates[0]
    assert updates[1]["sha"] == "blob-sha"
    created_content = base64.b64decode(updates[0]["content"]).decode()
    assert created_content.startswith("# Release Notes\n\n## Latest Changes\n")
    assert "### Features" in created_content


def test_update_latest_changes_returns_unchanged(
    repository: Repository,
    pull_request: PullRequest,
) -> None:
    content = generate_latest_changes("## Latest Changes\n", pull_request)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=repository_file(content).model_dump(),
            request=request,
        )

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        result = update_latest_changes(repository, pull_request, "token", client)

    assert result == ("unchanged", "release-notes.md")


def test_update_latest_changes_hides_update_error(
    repository: Repository,
    pull_request: PullRequest,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=repository_file("## Latest Changes\n").model_dump(),
                request=request,
            )
        return httpx.Response(500, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="release-notes update"),
    ):
        update_latest_changes(repository, pull_request, "token", client)


def test_update_latest_changes_does_not_retry_validation_error(
    repository: Repository,
    pull_request: PullRequest,
) -> None:
    put_count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal put_count
        if request.method == "GET":
            return httpx.Response(
                200,
                json=repository_file("## Latest Changes\n").model_dump(),
                request=request,
            )
        put_count += 1
        return httpx.Response(422, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(GitHubAPIError, match="release-notes update"),
    ):
        update_latest_changes(repository, pull_request, "token", client)

    assert put_count == 1
