"""Monthly integrity checks cannot rewrite a campaign benchmark or trust replace refs."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools import check_published_benchmark as gate

EXPECTED = (
    "reports/governance/champion_challenger.json",
    "reports/governance/champion_manifest.json",
    "reports/governance/e5_facts.json",
    "reports/eval/series_cohorts.json",
    "reports/prospective/pi_scale_by_h.json",
)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=Javier Rebull", "-c", "user.email=168046724+jrebull@users.noreply.github.com", *args],
        cwd=root,
        text=True,
    ).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    artifacts = []
    for relative in EXPECTED:
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"FAD":{},"DFF":{}}\n')
        artifacts.append({"path": relative, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    p = tmp_path / gate.MANIFEST
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"artifacts": artifacts}))
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def test_committed_and_worktree_benchmark_match(repo: Path) -> None:
    assert gate.check(repo) == []


def test_scientific_archive_inventory_is_complete() -> None:
    assert set(gate.ARTIFACTS) == set(EXPECTED)


@pytest.mark.parametrize("relative", EXPECTED)
def test_altered_scientific_archive_is_rejected(repo: Path, relative: str) -> None:
    (repo / relative).write_text('{"tampered":true}\n')
    assert gate.check(repo), relative


def test_assume_unchanged_cannot_hide_different_worktree_bytes(repo: Path) -> None:
    relative = gate.ARTIFACTS[0]
    git(repo, "update-index", "--assume-unchanged", relative)
    (repo / relative).write_text('{"fake":true}\n')
    assert gate.check(repo)


def test_symlink_to_identical_bytes_is_rejected(repo: Path) -> None:
    p = repo / gate.ARTIFACTS[0]
    target = repo / "copy.json"
    target.write_bytes(p.read_bytes())
    p.unlink()
    p.symlink_to(target)
    assert gate.check(repo)


def test_changed_committed_benchmark_cannot_reuse_the_previous_manifest(repo: Path) -> None:
    (repo / gate.ARTIFACTS[0]).write_text('{"changed":true}\n')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "changed")
    assert gate.check(repo)


def test_replace_ref_does_not_rebind_the_committed_manifest(repo: Path) -> None:
    original = git(repo, "rev-parse", "HEAD")
    (repo / gate.MANIFEST).write_text('{"artifacts":[]}\n')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "poison")
    replacement = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", original)
    git(repo, "replace", original, replacement)
    assert gate.check(repo) == []


def test_duplicate_manifest_entry_fails_closed(repo: Path) -> None:
    manifest = json.loads((repo / gate.MANIFEST).read_text())
    manifest["artifacts"].append(manifest["artifacts"][0])
    (repo / gate.MANIFEST).write_text(json.dumps(manifest))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "duplicate")
    assert gate.check(repo)


@pytest.mark.parametrize("relative", EXPECTED)
def test_missing_worktree_benchmark_fails_closed(repo: Path, relative: str) -> None:
    (repo / relative).unlink()
    assert gate.check(repo)


def test_executable_git_blob_is_not_a_regular_published_artifact(repo: Path) -> None:
    git(repo, "update-index", "--chmod=+x", gate.ARTIFACTS[0])
    git(repo, "commit", "-qm", "mode")
    assert gate.check(repo)


def test_duplicate_json_key_cannot_rebind_the_manifest(repo: Path) -> None:
    path = repo / gate.MANIFEST
    artifacts = json.dumps(json.loads(path.read_text())["artifacts"])
    path.write_text('{"artifacts":' + artifacts + ',"artifacts":' + artifacts + "}")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "duplicate key")
    assert gate.check(repo)
