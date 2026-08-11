import base64

import httpx

from app.github import (
    GitHubAPIError,
    RepositoryFileContentError,
    decode_repository_file,
    get_repository_file,
    github_headers,
)
from app.models import (
    CommitStatusState,
    PullRequest,
    Repository,
    RepositoryFile,
    UpdateStatus,
)

LATEST_CHANGES_FILES = (
    "release-notes.md",
    "docs/release-notes.md",
    "docs/en/docs/release-notes.md",
    "CHANGELOG.md",
)
DEFAULT_LATEST_CHANGES = "# Release Notes\n\n## Latest Changes\n"
MAX_FILE_SIZE = 1_000_000
COMMIT_MESSAGE = "📝 Update release notes\n\n[skip ci]"
MAX_UPDATE_ATTEMPTS = 3
LABEL_SECTIONS = (
    ("breaking", "Breaking Changes"),
    ("security", "Security Fixes"),
    ("feature", "Features"),
    ("bug", "Fixes"),
    ("refactor", "Refactors"),
    ("upgrade", "Upgrades"),
    ("docs", "Docs"),
    ("lang-all", "Translations"),
    ("infra", "Infrastructure"),
    ("internal", "Internal"),
)
LATEST_CHANGES_LABELS = (*tuple(label for label, _header in LABEL_SECTIONS), "release")


class LatestChangesError(RuntimeError):
    pass


def classify_latest_changes_labels(
    pull_request: PullRequest,
) -> tuple[CommitStatusState, tuple[str, ...]]:
    labels = {label.name for label in pull_request.labels}
    matching_labels = tuple(label for label in LATEST_CHANGES_LABELS if label in labels)
    if not matching_labels:
        return "pending", matching_labels
    if len(matching_labels) == 1:
        return "success", matching_labels
    return "failure", matching_labels


def get_latest_changes_label(pull_request: PullRequest) -> str:
    status, matching_labels = classify_latest_changes_labels(pull_request)
    if status == "pending":
        raise LatestChangesError(
            "The pull request has no recognized Latest Changes label"
        )
    if status == "failure":
        raise LatestChangesError(
            "The pull request has multiple Latest Changes labels: "
            + ", ".join(matching_labels)
        )
    return matching_labels[0]


def get_latest_changes_file(
    repository: Repository,
    token: str,
    client: httpx.Client,
) -> RepositoryFile | None:
    for path in LATEST_CHANGES_FILES:
        repository_file = get_repository_file(
            repository,
            path,
            repository.default_branch,
            token,
            client,
        )
        if repository_file is None:
            continue
        return repository_file
    return None


def update_latest_changes(
    repository: Repository,
    pull_request: PullRequest,
    token: str,
    client: httpx.Client,
) -> tuple[UpdateStatus, str]:
    headers = github_headers(token)
    attempt = 1
    while True:
        repository_file = get_latest_changes_file(repository, token, client)
        if repository_file is None:
            current_content = DEFAULT_LATEST_CHANGES
        else:
            try:
                current_content = decode_repository_file(
                    repository_file,
                    max_size=MAX_FILE_SIZE,
                    description="release-notes",
                )
            except RepositoryFileContentError as error:
                raise LatestChangesError(str(error)) from error
        updated_content = generate_latest_changes(current_content, pull_request)
        if repository_file is not None and updated_content == current_content:
            return "unchanged", repository_file.path

        path = (
            repository_file.path
            if repository_file is not None
            else LATEST_CHANGES_FILES[0]
        )
        update: dict[str, str] = {
            "message": COMMIT_MESSAGE,
            "content": base64.b64encode(updated_content.encode()).decode(),
            "branch": repository.default_branch,
        }
        if repository_file is not None:
            update["sha"] = repository_file.sha
        response = client.put(
            f"/repos/{repository.full_name}/contents/{path}",
            headers=headers,
            json=update,
        )
        if (
            response.status_code == 409
            or repository_file is None
            and response.status_code == 422
        ) and attempt < MAX_UPDATE_ATTEMPTS:
            attempt += 1
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise GitHubAPIError("GitHub rejected the release-notes update") from error
        return "updated", path


def generate_latest_changes(content: str, pull_request: PullRequest) -> str:
    message = (
        f"* {pull_request.title}. PR [#{pull_request.number}]"
        f"({pull_request.html_url}) by [@{pull_request.user.login}]"
        f"({pull_request.user.html_url})."
    )
    if message in content:
        return content

    lines = content.splitlines()
    try:
        header_index = lines.index("## Latest Changes")
    except ValueError as error:
        raise LatestChangesError(
            "The release-notes file has no '## Latest Changes' heading"
        ) from error

    release_end = len(lines)
    for index in range(header_index + 1, len(lines)):
        if lines[index].startswith("## "):
            release_end = index
            break

    release_lines = lines[header_index + 1 : release_end]
    while release_lines and not release_lines[0].strip():
        release_lines.pop(0)
    while release_lines and not release_lines[-1].strip():
        release_lines.pop()

    headings = {f"### {header}": label for label, header in LABEL_SECTIONS}
    section_indexes = [
        index for index, line in enumerate(release_lines) if line.startswith("### ")
    ]
    for index in section_indexes:
        if release_lines[index] not in headings:
            raise LatestChangesError(
                f"Unsupported latest-changes section: {release_lines[index]}"
            )

    first_section = section_indexes[0] if section_indexes else len(release_lines)
    sectionless_lines = release_lines[:first_section]
    while sectionless_lines and not sectionless_lines[-1].strip():
        sectionless_lines.pop()
    sections: dict[str, list[str]] = {}
    for position, start in enumerate(section_indexes):
        end = (
            section_indexes[position + 1]
            if position + 1 < len(section_indexes)
            else len(release_lines)
        )
        label = headings[release_lines[start]]
        if label in sections:
            raise LatestChangesError(
                f"Duplicate latest-changes section: {release_lines[start]}"
            )
        section_content = release_lines[start + 1 : end]
        while section_content and not section_content[0].strip():
            section_content.pop(0)
        while section_content and not section_content[-1].strip():
            section_content.pop()
        sections[label] = section_content

    selected_label = get_latest_changes_label(pull_request)
    if selected_label == "release":
        raise LatestChangesError(
            "A pull request with the release label must not update Latest Changes"
        )
    sections[selected_label] = [message, *sections.get(selected_label, [])]

    rebuilt_release: list[str] = list(sectionless_lines)
    for label, header in LABEL_SECTIONS:
        section_content = sections.get(label)
        if not section_content:
            continue
        if rebuilt_release:
            rebuilt_release.append("")
        rebuilt_release.extend([f"### {header}", "", *section_content])

    updated_lines = [
        *lines[: header_index + 1],
        "",
        *rebuilt_release,
        "",
        *lines[release_end:],
    ]
    while updated_lines and not updated_lines[-1].strip():
        updated_lines.pop()
    return "\n".join(updated_lines) + "\n"
