import copy
import tomllib
from collections.abc import Collection, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Annotated, Any

import httpx
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from app.github import (
    RepositoryFileContentError,
    add_pull_request_label,
    decode_repository_file,
    get_pull_request_files,
    get_repository_file,
    remove_pull_request_label,
)
from app.latest_changes import LABEL_SECTIONS, LATEST_CHANGES_LABELS
from app.models import Label, PullRequest, Repository

AUTOMATIC_LABELS_CONFIG_PATH = ".github/latest-changes.yml"
PYPROJECT_PATH = "pyproject.toml"
MAX_CONFIG_SIZE = 100_000
MAX_PYPROJECT_SIZE = 1_000_000
SECTION_LABELS = tuple(label for label, _header in LABEL_SECTIONS)
MISSING = object()

type NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class ExcludeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude: NonEmptyString


type PathRule = NonEmptyString | ExcludeRule
type PathRules = Annotated[list[PathRule], Field(min_length=1)]


class AutomaticLabelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    automatic_labels: dict[str, PathRules] = Field(alias="auto-labels")

    @field_validator("automatic_labels")
    @classmethod
    def validate_labels(
        cls,
        automatic_labels: dict[str, PathRules],
    ) -> dict[str, PathRules]:
        unsupported = set(automatic_labels) - set(SECTION_LABELS)
        if unsupported:
            labels = ", ".join(sorted(unsupported))
            raise ValueError(f"unsupported automatic labels: {labels}")
        return automatic_labels


class AutomaticLabelsConfigError(RuntimeError):
    pass


def parse_automatic_labels_config(content: str) -> AutomaticLabelsConfig:
    try:
        data = yaml.safe_load(content)
        return AutomaticLabelsConfig.model_validate(data)
    except (yaml.YAMLError, ValidationError) as error:
        raise AutomaticLabelsConfigError(
            f"The {AUTOMATIC_LABELS_CONFIG_PATH} configuration is invalid"
        ) from error


def get_automatic_labels_config(
    repository: Repository,
    token: str,
    client: httpx.Client,
) -> AutomaticLabelsConfig | None:
    repository_file = get_repository_file(
        repository,
        AUTOMATIC_LABELS_CONFIG_PATH,
        repository.default_branch,
        token,
        client,
    )
    if repository_file is None:
        return None
    try:
        content = decode_repository_file(
            repository_file,
            max_size=MAX_CONFIG_SIZE,
            description="automatic-label configuration",
        )
    except RepositoryFileContentError as error:
        raise AutomaticLabelsConfigError(str(error)) from error
    return parse_automatic_labels_config(content)


def match_path_rules(path: str, rules: Sequence[PathRule]) -> bool | None:
    pure_path = PurePosixPath(path)
    for rule in rules:
        if isinstance(rule, str):
            if pure_path.full_match(rule, case_sensitive=True):
                return True
        elif pure_path.full_match(rule.exclude, case_sensitive=True):
            return False
    return None


def get_highest_priority_label(labels: Collection[str]) -> str | None:
    return next((label for label in SECTION_LABELS if label in labels), None)


def labels_for_path(
    path: str,
    config: AutomaticLabelsConfig,
    built_in_label: str | None = None,
) -> set[str]:
    labels: set[str] = set()
    for label, rules in config.automatic_labels.items():
        result = match_path_rules(path, rules)
        if result is True or label == built_in_label and result is not False:
            labels.add(label)
    if built_in_label is not None and built_in_label not in config.automatic_labels:
        labels.add(built_in_label)
    return labels


def normalize_toml_value(value: Any) -> Any:
    if isinstance(value, list):
        normalized = (normalize_toml_value(item) for item in value)
        return tuple(sorted(normalized, key=repr))
    if isinstance(value, dict):
        return tuple(
            (key, normalize_toml_value(item)) for key, item in sorted(value.items())
        )
    return value


def get_nested_value(data: Mapping[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            return MISSING
        value = value[key]
    return normalize_toml_value(value)


def pyproject_remainder(data: dict[str, Any]) -> dict[str, Any]:
    remainder = copy.deepcopy(data)
    project = remainder.get("project")
    if isinstance(project, dict):
        project.pop("dependencies", None)
        project.pop("optional-dependencies", None)
        if not project:
            remainder.pop("project")
    remainder.pop("dependency-groups", None)
    return remainder


def classify_pyproject_changes(
    base_content: str,
    head_content: str,
) -> set[str | None]:
    try:
        base = tomllib.loads(base_content)
        head = tomllib.loads(head_content)
    except TypeError, tomllib.TOMLDecodeError:
        return {None}

    classifications: set[str | None] = set()
    if get_nested_value(base, "project", "dependencies") != get_nested_value(
        head, "project", "dependencies"
    ):
        classifications.add("upgrade")
    if get_nested_value(base, "project", "optional-dependencies") != get_nested_value(
        head, "project", "optional-dependencies"
    ):
        classifications.add("upgrade")
    if get_nested_value(base, "dependency-groups") != get_nested_value(
        head, "dependency-groups"
    ):
        classifications.add("internal")
    if pyproject_remainder(base) != pyproject_remainder(head):
        classifications.add(None)
    return classifications or {None}


def get_pyproject_classifications(
    repository: Repository,
    pull_request: PullRequest,
    token: str,
    client: httpx.Client,
) -> set[str | None]:
    base_file = get_repository_file(
        repository,
        PYPROJECT_PATH,
        pull_request.base.sha,
        token,
        client,
    )
    head_file = get_repository_file(
        repository,
        PYPROJECT_PATH,
        pull_request.head.sha,
        token,
        client,
    )
    if base_file is None or head_file is None:
        return {None}
    try:
        base_content = decode_repository_file(
            base_file,
            max_size=MAX_PYPROJECT_SIZE,
            description=PYPROJECT_PATH,
        )
        head_content = decode_repository_file(
            head_file,
            max_size=MAX_PYPROJECT_SIZE,
            description=PYPROJECT_PATH,
        )
    except RepositoryFileContentError:
        return {None}
    return classify_pyproject_changes(base_content, head_content)


def get_automatic_label_candidate(
    repository: Repository,
    pull_request: PullRequest,
    config: AutomaticLabelsConfig,
    token: str,
    client: httpx.Client,
) -> str | None:
    files = get_pull_request_files(repository, pull_request, token, client)
    candidates: set[str] = set()
    pyproject_classifications: set[str | None] | None = None

    for changed_file in files:
        paths = [changed_file.filename]
        if (
            changed_file.previous_filename is not None
            and changed_file.previous_filename != changed_file.filename
        ):
            paths.append(changed_file.previous_filename)

        for path in paths:
            if path == PYPROJECT_PATH:
                if pyproject_classifications is None:
                    pyproject_classifications = get_pyproject_classifications(
                        repository,
                        pull_request,
                        token,
                        client,
                    )
                for built_in_label in pyproject_classifications:
                    labels = labels_for_path(path, config, built_in_label)
                    if not labels:
                        return None
                    candidates.update(labels)
            else:
                labels = labels_for_path(path, config)
                if not labels:
                    return None
                candidates.update(labels)

    return get_highest_priority_label(candidates)


def select_latest_changes_label(
    labels: Sequence[Label],
    *,
    explicit_label: str | None = None,
    automatic_candidate: str | None = None,
) -> str | None:
    existing = {label.name for label in labels}
    matching = existing.intersection(LATEST_CHANGES_LABELS)
    if explicit_label in matching and len(matching) == 1:
        return explicit_label
    if "release" in matching:
        return "release"

    if automatic_candidate is not None:
        matching.add(automatic_candidate)
    return get_highest_priority_label(matching)


def normalize_pull_request_labels(
    repository: Repository,
    pull_request: PullRequest,
    selected_label: str | None,
    token: str,
    client: httpx.Client,
) -> None:
    existing = {label.name for label in pull_request.labels}
    if selected_label is not None and selected_label not in existing:
        add_pull_request_label(
            repository,
            pull_request,
            selected_label,
            token,
            client,
        )
    for label in LATEST_CHANGES_LABELS:
        if label in existing and label != selected_label:
            remove_pull_request_label(
                repository,
                pull_request,
                label,
                token,
                client,
            )
