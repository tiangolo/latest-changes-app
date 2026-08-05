import base64

import httpx
import jwt
import pytest

from app.config import Settings
from app.github import GitHubAPIError, issue_installation_token
from app.latest_changes import (
    LatestChangesError,
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
                "permissions": {"contents": "write"},
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
    assert payload["iss"] == "123"


@pytest.mark.parametrize(
    ("token_data", "repositories"),
    [
        (
            {"permissions": {"contents": "read"}, "repository_selection": "selected"},
            [{"id": 75369425}],
        ),
        (
            {"permissions": {"contents": "write"}, "repository_selection": "all"},
            [{"id": 75369425}],
        ),
        (
            {"permissions": {"contents": "write"}, "repository_selection": "selected"},
            [{"id": 1}],
        ),
    ],
)
def test_issue_installation_token_rejects_unexpected_scope(
    settings: Settings,
    repository: Repository,
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


def test_issue_installation_token_hides_github_error(
    settings: Settings,
    repository: Repository,
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
    assert result.path == existing_path
    assert paths == supported_paths[: supported_paths.index(existing_path) + 1]


def test_get_latest_changes_file_rejects_missing_or_large_file(
    repository: Repository,
) -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(missing),
        ) as client,
        pytest.raises(LatestChangesError, match="No supported"),
    ):
        get_latest_changes_file(repository, "token", client)

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


def test_generate_latest_changes_without_known_label(
    pull_request: PullRequest,
) -> None:
    unlabeled = pull_request.model_copy(update={"labels": []})

    result = generate_latest_changes("## Latest Changes\n", unlabeled)

    assert result.startswith("## Latest Changes\n\n* Add the feature.")


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


def test_generate_latest_changes_prepends_to_uncategorized_entries(
    pull_request: PullRequest,
) -> None:
    unlabeled = pull_request.model_copy(update={"labels": []})
    content = """## Latest Changes

* Existing uncategorized change.

## 1.0.0
"""

    result = generate_latest_changes(content, unlabeled)

    assert (
        result
        == """## Latest Changes

* Add the feature. PR [#42](https://github.com/fastapi/fastapi/pull/42) by [@contributor](https://github.com/contributor).
* Existing uncategorized change.

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


def test_generate_latest_changes_uses_first_matching_label(
    pull_request: PullRequest,
) -> None:
    multiple_labels = pull_request.model_copy(
        update={"labels": [Label(name="bug"), Label(name="feature")]}
    )

    result = generate_latest_changes("## Latest Changes\n", multiple_labels)

    assert "### Features\n\n* Add the feature." in result
    assert "### Fixes" not in result


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
