"""Historical reproducibility does not silently read today's moving panel."""

import subprocess
from pathlib import Path

import pytest

from tests.published_inputs import archived_inputs


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=Javier Rebull", "-c", "user.email=168046724+jrebull@users.noreply.github.com", *args],
        cwd=root,
        text=True,
    ).strip()


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    (tmp_path / "artifact.json").write_text('{"scientific":"original"}\n')
    (tmp_path / "panel.csv").write_text("the original panel\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "original fixture")
    original = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "panel.csv").write_text("a later monthly panel\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "later fixture")
    return tmp_path, original


def test_reads_the_artifact_epoch_even_after_the_panel_advances(history: tuple[Path, str]) -> None:
    root, original = history
    revision, inputs = archived_inputs(root, "artifact.json", ("panel.csv",))
    assert revision == original
    assert inputs == {"panel.csv": b"the original panel\n"}


def test_changed_worktree_artifact_is_not_an_archive(history: tuple[Path, str]) -> None:
    root, _ = history
    (root / "artifact.json").write_text('{"changed":true}\n')
    with pytest.raises(AssertionError, match="uncommitted"):
        archived_inputs(root, "artifact.json", ("panel.csv",))


def test_missing_historical_input_rejects(history: tuple[Path, str]) -> None:
    root, _ = history
    with pytest.raises(AssertionError, match="missing"):
        archived_inputs(root, "artifact.json", ("not-present.csv",))


def test_replace_refs_cannot_swap_the_historical_input(history: tuple[Path, str]) -> None:
    root, original = history
    replacement = _git(root, "rev-parse", "HEAD")
    _git(root, "replace", original, replacement)
    _, inputs = archived_inputs(root, "artifact.json", ("panel.csv",))
    assert inputs["panel.csv"] == b"the original panel\n"


def test_reference_panel_rejects_a_wrong_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import scientific_reference as reference

    monkeypatch.setattr(reference, "_blob", lambda *_: b"other panel\n")
    with pytest.raises(AssertionError, match="reference input changed"):
        reference.reference_panel()


def test_archived_input_must_be_a_regular_git_blob(history: tuple[Path, str]) -> None:
    root, _ = history
    (root / "link.csv").symlink_to("panel.csv")
    _git(root, "add", ".")
    (root / "artifact.json").write_text('{"scientific":"next"}\n')
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "linked fixture")
    with pytest.raises(AssertionError, match="link.csv"):
        archived_inputs(root, "artifact.json", ("link.csv",))
