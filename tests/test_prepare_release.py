from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scripts.prepare_release import (
    BumpType,
    app,
    bump_version,
    get_current_version,
    get_release_notes_body,
    parse_version,
    update_release_notes,
    update_version_file,
)

runner = CliRunner()


@pytest.mark.parametrize(
    ("current_version", "bump", "new_version"),
    [
        ("0.1.2", "major", "1.0.0"),
        ("0.1.2", "minor", "0.2.0"),
        ("0.1.2", "patch", "0.1.3"),
    ],
)
def test_bump_version(current_version: str, bump: BumpType, new_version: str) -> None:
    assert bump_version(current_version, bump) == new_version


def test_parse_version_rejects_invalid_version() -> None:
    with pytest.raises(ValueError, match="Invalid version"):
        parse_version("0.1")


def test_get_current_version_requires_exactly_one_assignment() -> None:
    content = 'version = "0.1.2"\nversion = "0.1.3"\n'

    with pytest.raises(RuntimeError, match="Expected exactly one"):
        get_current_version(content, Path("pyproject.toml"))


def test_update_version_file() -> None:
    content = '[project]\nversion = "0.1.2"\n'

    assert update_version_file(content, "0.1.3", Path("pyproject.toml")) == (
        '[project]\nversion = "0.1.3"\n'
    )


def test_update_version_file_requires_newer_version() -> None:
    with pytest.raises(RuntimeError, match="must be greater"):
        update_version_file('version = "0.1.2"\n', "0.1.2", Path("pyproject.toml"))


def test_update_release_notes() -> None:
    content = """# Release Notes

## Latest Changes

### Fixes

* Fix something.

## 0.1.2 (2026-08-04)

* Previous change.
"""

    assert (
        update_release_notes(
            content, "0.1.3", date(2026, 8, 5), Path("release-notes.md")
        )
        == """# Release Notes

## Latest Changes

## 0.1.3 (2026-08-05)

### Fixes

* Fix something.

## 0.1.2 (2026-08-04)

* Previous change.
"""
    )


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("## Latest Changes\n", "must start"),
        ("# Release Notes\n\n### Fixes\n", "must start"),
        (
            "# Release Notes\n\n## Latest Changes\n\n## 0.1.3\n",
            "already contain",
        ),
    ],
)
def test_update_release_notes_rejects_invalid_content(content: str, error: str) -> None:
    with pytest.raises(RuntimeError, match=error):
        update_release_notes(
            content, "0.1.3", date(2026, 8, 5), Path("release-notes.md")
        )


def test_get_release_notes_body() -> None:
    content = """# Release Notes

## Latest Changes

## 0.1.3 (2026-08-05)

### Fixes

* Fix something.

## 0.1.2 (2026-08-04)

* Previous change.
"""

    assert (
        get_release_notes_body(content, "0.1.3", Path("release-notes.md"))
        == "### Fixes\n\n* Fix something.\n"
    )


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("# Release Notes\n\n## Latest Changes\n", "Could not find"),
        (
            "# Release Notes\n\n## Latest Changes\n\n## 0.1.3\n\n## 0.1.2\n",
            "is empty",
        ),
    ],
)
def test_get_release_notes_body_rejects_invalid_content(
    content: str, error: str
) -> None:
    with pytest.raises(RuntimeError, match=error):
        get_release_notes_body(content, "0.1.3", Path("release-notes.md"))


def test_cli_commands(tmp_path: Path) -> None:
    version_file = tmp_path / "pyproject.toml"
    version_file.write_text('[project]\nversion = "0.1.2"\n')
    release_notes_file = tmp_path / "release-notes.md"
    release_notes_file.write_text(
        "# Release Notes\n\n## Latest Changes\n\n* Fix something.\n"
    )

    result = runner.invoke(
        app,
        [
            "prepare",
            "patch",
            "--version-file",
            str(version_file),
            "--release-notes-file",
            str(release_notes_file),
            "--date",
            "2026-08-05",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Prepared release 0.1.3 (2026-08-05)" in result.output

    result = runner.invoke(
        app, ["current-version", "--version-file", str(version_file)]
    )
    assert result.exit_code == 0, result.output
    assert result.output == "0.1.3\n"

    result = runner.invoke(
        app,
        [
            "release-notes",
            "--version-file",
            str(version_file),
            "--release-notes-file",
            str(release_notes_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output == "* Fix something.\n"
