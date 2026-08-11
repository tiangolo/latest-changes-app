import base64

import httpx
import pytest

from app import automatic_labels
from app.automatic_labels import (
    AutomaticLabelsConfig,
    AutomaticLabelsConfigError,
    ExcludeRule,
    classify_pyproject_changes,
    get_automatic_label_candidate,
    get_automatic_labels_config,
    get_pyproject_classifications,
    match_path_rules,
    normalize_pull_request_labels,
    parse_automatic_labels_config,
    select_latest_changes_label,
)
from app.models import Label, PullRequest, PullRequestFile, Repository, RepositoryFile


def repository_file(
    text: str,
    *,
    path: str = ".github/latest-changes.yml",
    **updates: object,
) -> RepositoryFile:
    data: dict[str, object] = {
        "type": "file",
        "path": path,
        "sha": "blob-sha",
        "size": len(text),
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
    }
    data.update(updates)
    return RepositoryFile.model_validate(data)


def test_parse_automatic_labels_config() -> None:
    result = parse_automatic_labels_config(
        """
auto-labels:
  docs:
    - docs/**
  internal:
    - exclude: scripts/generated/**
    - scripts/**
"""
    )

    assert result.automatic_labels["docs"] == ["docs/**"]
    assert result.automatic_labels["internal"] == [
        ExcludeRule(exclude="scripts/generated/**"),
        "scripts/**",
    ]


@pytest.mark.parametrize(
    "content",
    [
        "[",
        "auto-labels: []",
        "auto-labels:\n  release:\n    - release/**",
        "auto-labels:\n  unknown:\n    - unknown/**",
        "auto-labels:\n  docs: []",
        "auto-labels:\n  docs:\n    - ''",
        "auto-labels:\n  docs:\n    - exclude: ''",
        "auto-labels:\n  docs:\n    - exclude: docs/**\n      other: true",
        "auto-labels: {}\nother: true",
    ],
)
def test_parse_automatic_labels_config_rejects_invalid_content(
    content: str,
) -> None:
    with pytest.raises(AutomaticLabelsConfigError, match="configuration is invalid"):
        parse_automatic_labels_config(content)


def test_get_automatic_labels_config() -> None:
    content = "auto-labels: {}\n"

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ref"] == "master"
        return httpx.Response(
            200,
            json=repository_file(content).model_dump(),
            request=request,
        )

    repository = Repository(
        id=1,
        full_name="fastapi/fastapi",
        default_branch="master",
    )
    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        result = get_automatic_labels_config(repository, "token", client)

    assert result == AutomaticLabelsConfig.model_validate({"auto-labels": {}})


def test_get_automatic_labels_config_rejects_invalid_file(
    repository: Repository,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=repository_file("", encoding="utf-8").model_dump(),
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(AutomaticLabelsConfigError, match="did not return"),
    ):
        get_automatic_labels_config(repository, "token", client)


def test_get_automatic_labels_config_returns_none_when_missing(
    repository: Repository,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        assert get_automatic_labels_config(repository, "token", client) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/generated/schema.json", False),
        ("docs/en/index.md", True),
        ("src/app.py", None),
    ],
)
def test_match_path_rules(path: str, expected: bool | None) -> None:
    rules = [ExcludeRule(exclude="docs/generated/**"), "docs/**"]

    assert match_path_rules(path, rules) is expected


@pytest.mark.parametrize(
    ("base", "head", "expected"),
    [
        (
            '[project]\nname="example"\ndependencies=["a", "b"]\n',
            '[project]\nname="example"\ndependencies=["b", "c"]\n',
            {"upgrade"},
        ),
        (
            '[project]\nname="example"\noptional-dependencies={docs=["a"]}\n',
            '[project]\nname="example"\noptional-dependencies={docs=["b"]}\n',
            {"upgrade"},
        ),
        (
            '[project]\nname="example"\n[dependency-groups]\ndev=["a"]\n',
            '[project]\nname="example"\n[dependency-groups]\ndev=["b"]\n',
            {"internal"},
        ),
        (
            '[project]\nname="example"\nversion="1"\n',
            '[project]\nname="example"\nversion="2"\n',
            {None},
        ),
        (
            '[project]\nname="example"\ndependencies=["a", "b"]\n',
            '[project]\nname="example"\ndependencies=["b", "a"]\n',
            {None},
        ),
        (
            '[project]\nname="example"\nclassifiers=["a", "b"]\n',
            '[project]\nname="example"\nclassifiers=["b", "a"]\n',
            {None},
        ),
        (
            "[project]\ndependencies=[]\n",
            '[project]\ndependencies=["a"]\n',
            {"upgrade"},
        ),
        ("project = 'invalid-shape'\n", "project = 'changed'\n", {None}),
        ("invalid = [\n", "invalid = []\n", {None}),
    ],
)
def test_classify_pyproject_changes(
    base: str,
    head: str,
    expected: set[str | None],
) -> None:
    assert classify_pyproject_changes(base, head) == expected


def test_classify_pyproject_changes_keeps_mixed_changes_unclassified() -> None:
    base = '[project]\nname="example"\nversion="1"\ndependencies=["a"]\n'
    head = '[project]\nname="example"\nversion="2"\ndependencies=["b"]\n'

    assert classify_pyproject_changes(base, head) == {"upgrade", None}


def test_get_pyproject_classifications(
    repository: Repository,
    pull_request: PullRequest,
) -> None:
    contents = {
        "base-sha": '[project]\nname="example"\ndependencies=["a"]\n',
        "head-sha": '[project]\nname="example"\ndependencies=["b"]\n',
    }

    def handle(request: httpx.Request) -> httpx.Response:
        content = contents[request.url.params["ref"]]
        return httpx.Response(
            200,
            json=repository_file(content, path="pyproject.toml").model_dump(),
            request=request,
        )

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        result = get_pyproject_classifications(
            repository,
            pull_request,
            "token",
            client,
        )

    assert result == {"upgrade"}


@pytest.mark.parametrize("response_type", ["missing", "invalid"])
def test_get_pyproject_classifications_returns_unclassified_when_unavailable(
    repository: Repository,
    pull_request: PullRequest,
    response_type: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if response_type == "missing":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            json=repository_file(
                "",
                path="pyproject.toml",
                encoding="utf-8",
            ).model_dump(),
            request=request,
        )

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        result = get_pyproject_classifications(
            repository,
            pull_request,
            "token",
            client,
        )

    assert result == {None}


@pytest.mark.parametrize(
    ("files", "pyproject", "expected"),
    [
        ([PullRequestFile(filename="docs/index.md", status="modified")], None, "docs"),
        ([PullRequestFile(filename="src/app.py", status="modified")], None, None),
        (
            [PullRequestFile(filename="pyproject.toml", status="modified")],
            {"upgrade"},
            "upgrade",
        ),
        (
            [
                PullRequestFile(filename="pyproject.toml", status="modified"),
                PullRequestFile(filename="uv.lock", status="modified"),
            ],
            {"internal"},
            "internal",
        ),
        (
            [PullRequestFile(filename="pyproject.toml", status="modified")],
            {"upgrade", None},
            None,
        ),
        (
            [
                PullRequestFile(
                    filename="docs/new.md",
                    previous_filename="docs/old.md",
                    status="renamed",
                )
            ],
            None,
            "docs",
        ),
        ([], None, None),
    ],
)
def test_get_automatic_label_candidate(
    repository: Repository,
    pull_request: PullRequest,
    monkeypatch: pytest.MonkeyPatch,
    files: list[PullRequestFile],
    pyproject: set[str | None] | None,
    expected: str | None,
) -> None:
    automatic_config = parse_automatic_labels_config(
        """
auto-labels:
  docs:
    - docs/**
  internal:
    - .github/**
    - uv.lock
"""
    )
    monkeypatch.setattr(automatic_labels, "get_pull_request_files", lambda *args: files)
    if pyproject is not None:
        monkeypatch.setattr(
            automatic_labels,
            "get_pyproject_classifications",
            lambda *args: pyproject,
        )

    with httpx.Client() as client:
        result = get_automatic_label_candidate(
            repository,
            pull_request,
            automatic_config,
            "token",
            client,
        )

    assert result == expected


def test_automatic_label_exclusion_suppresses_built_in(
    repository: Repository,
    pull_request: PullRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    automatic_config = parse_automatic_labels_config(
        """
auto-labels:
  upgrade:
    - exclude: pyproject.toml
"""
    )
    files = [PullRequestFile(filename="pyproject.toml", status="modified")]
    monkeypatch.setattr(automatic_labels, "get_pull_request_files", lambda *args: files)
    monkeypatch.setattr(
        automatic_labels,
        "get_pyproject_classifications",
        lambda *args: {"upgrade"},
    )

    with httpx.Client() as client:
        result = get_automatic_label_candidate(
            repository,
            pull_request,
            automatic_config,
            "token",
            client,
        )

    assert result is None


@pytest.mark.parametrize(
    ("labels", "explicit", "automatic", "expected"),
    [
        (["feature", "internal"], "internal", "upgrade", "feature"),
        (["internal"], "internal", "upgrade", "internal"),
        (["feature"], "internal", None, "feature"),
        (["release", "feature"], None, "breaking", "release"),
        (["bug", "internal"], None, None, "bug"),
        (["internal"], None, "docs", "docs"),
        (["docs"], None, "internal", "docs"),
        ([], None, None, None),
    ],
)
def test_select_latest_changes_label(
    labels: list[str],
    explicit: str | None,
    automatic: str | None,
    expected: str | None,
) -> None:
    assert (
        select_latest_changes_label(
            [Label(name=label) for label in labels],
            explicit_label=explicit,
            automatic_candidate=automatic,
        )
        == expected
    )


def test_normalize_pull_request_labels(
    repository: Repository,
    pull_request: PullRequest,
) -> None:
    pull_request = pull_request.model_copy(
        update={
            "labels": [
                Label(name="feature"),
                Label(name="internal"),
                Label(name="other"),
            ]
        }
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        normalize_pull_request_labels(
            repository,
            pull_request,
            "docs",
            "token",
            client,
        )

    assert requests[0].method == "POST"
    assert requests[0].read() == b'{"labels":["docs"]}'
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests[1:]] == [
        "feature",
        "internal",
    ]


def test_normalize_pull_request_labels_does_nothing_without_labels(
    repository: Repository,
    pull_request: PullRequest,
) -> None:
    pull_request = pull_request.model_copy(update={"labels": []})

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Unexpected GitHub request")
        )
    ) as client:
        normalize_pull_request_labels(
            repository,
            pull_request,
            None,
            "token",
            client,
        )
