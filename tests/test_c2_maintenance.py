"""C2: expediente sobre objetos Git reales, sin crear autorización humana ni publicar."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from tools import check_maintenance_release as maintenance

BASE = ""
CANDIDATE = ""


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture C2",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture C2",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        },
    ).strip()


def seal(repo: Path, paths: list[str], source: str, card: str) -> None:
    cc = maintenance.cc
    normalized = f"release_id: {cc.RELEASE_ID_SENTINEL}\n{card}\n"
    normalized_sha = hashlib.sha256(normalized.encode()).hexdigest()
    pairs = [
        (p, normalized_sha if p == maintenance.CARD else hashlib.sha256((repo / p).read_bytes()).hexdigest())
        for p in sorted(paths)
    ]
    digest = hashlib.sha256(json.dumps(pairs, sort_keys=True).encode()).hexdigest()
    release = f"2026-09-{digest[:12]}"
    (repo / maintenance.CARD).write_text(normalized.replace(cc.RELEASE_ID_SENTINEL, release))
    artifacts = [
        {
            "path": p,
            "sha256": hashlib.sha256((repo / p).read_bytes()).hexdigest(),
            "size": len((repo / p).read_bytes()),
            "criticality": "required",
            "mime": "text/plain",
        }
        for p in sorted(paths)
    ]
    (repo / maintenance.MANIFEST).write_text(
        json.dumps(
            {
                "release_id": release,
                "git_sha": source[:12],
                "panel_vintage": "2026-09",
                "artifacts": artifacts,
                "identity": {
                    "schema": 2,
                    "card_path": maintenance.CARD,
                    "sentinel": cc.RELEASE_ID_SENTINEL,
                    "digest": digest,
                    "card_sha256_normalized": normalized_sha,
                },
            }
        )
    )


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch) -> Path:
    # Historial sintético pequeño: no exige SHAs de ramas que un futuro squash puede retirar.
    repo = tmp_path / "candidate"
    repo.mkdir()
    git(repo, "init", "-q")
    paths = [
        maintenance.CARD,
        maintenance.cc.INGESTION_FEED_PATH,
        *maintenance.cc.E5_PATHS,
        "data/processed/visa_panel_long.csv",
        "reports/governance/key_facts.json",
    ]
    for path in (*paths, maintenance.MANIFEST):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n")
    source = commit(repo)
    seal(repo, paths, source, "base publicada sintética")
    # La base histórica incorporó este documento después de su commit de origen.
    # No se reescribe; el candidato nuevo sí debe fijarlo en su commit de origen.
    (repo / "docs").mkdir()
    (repo / "docs/MLOPS_ARCHITECTURE.md").write_text("historical generated metadata\n")
    base = commit(repo)
    contract = repo / maintenance.CONTRACT
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(json.dumps(maintenance.CONTRACT_V1))
    source = commit(repo)
    seal(repo, [*paths, maintenance.CONTRACT], source, "mantenimiento sintético")
    head = commit(repo)
    monkeypatch.setitem(globals(), "BASE", base)
    monkeypatch.setitem(globals(), "CANDIDATE", head)
    return repo


def commit(repo: Path) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "synthetic maintenance test")
    return git(repo, "rev-parse", "HEAD")


def test_metadata_only_is_eligible_but_does_not_authorize(candidate: Path) -> None:
    result = maintenance.inspect(candidate, BASE, CANDIDATE)
    assert result["eligible"] is True and result["authorizes_publication"] is False
    assert result["base_release_id"] != result["release_id"]
    assert result["release_id"] == json.loads((candidate / maintenance.MANIFEST).read_bytes())["release_id"]
    assert result["source_commit_full"].startswith(result["source_commit"])
    assert len(result["required_next"]) == 3
    assert git(candidate, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "path",
    [
        "reports/governance/key_facts.json",
        "data/processed/visa_panel_long.csv",
        "dvc.lock",
        "docs/cohort_deck.json",
        "models.dvc",
    ],
)
def test_scientific_changes_are_not_maintenance(candidate: Path, path: str) -> None:
    target = candidate / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(target.read_bytes() + b"\n" if target.exists() else b"new scientific input\n")
    source = commit(candidate)
    paths = [a["path"] for a in json.loads((candidate / maintenance.MANIFEST).read_bytes())["artifacts"]]
    seal(candidate, paths, source, "adversarial resealed scientific change")
    head = commit(candidate)
    with pytest.raises(maintenance.MaintenanceError, match="científico"):
        maintenance.inspect(candidate, BASE, head)


@pytest.mark.parametrize("path", [maintenance.CARD, maintenance.MANIFEST, maintenance.CONTRACT])
def test_eligible_paths_are_still_hash_checked(candidate: Path, path: str) -> None:
    target = candidate / path
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_bytes(target.read_bytes() + b"\n")
    head = commit(candidate)
    if path == maintenance.MANIFEST:
        # Whitespace in the manifest does not change its parsed identity: expected, and reported by raw hash.
        result = maintenance.inspect(candidate, BASE, head)
        assert result["manifest_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
        assert result["manifest_sha256"] != before
    else:
        with pytest.raises(maintenance.MaintenanceError, match="hash"):
            maintenance.inspect(candidate, BASE, head)


@pytest.mark.parametrize("path", [maintenance.CARD, maintenance.MANIFEST, maintenance.CONTRACT])
def test_symlink_cannot_publish_its_target_bytes(candidate: Path, path: str, tmp_path: Path) -> None:
    target = candidate / path
    external = tmp_path / "external"
    external.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(external)
    head = commit(candidate)
    with pytest.raises(maintenance.MaintenanceError, match="regular"):
        maintenance.inspect(candidate, BASE, head)


def test_worktree_does_not_replace_reviewed_blobs(candidate: Path) -> None:
    target = candidate / maintenance.CARD
    expected = maintenance.inspect(candidate, BASE, CANDIDATE)
    git(candidate, "update-index", "--assume-unchanged", maintenance.CARD)
    target.write_text("malicious disk card\n")
    assert maintenance.inspect(candidate, BASE, CANDIDATE) == expected


def test_dirty_and_wrong_head_are_rejected(candidate: Path) -> None:
    (candidate / "extra.txt").write_text("unreviewed\n")
    with pytest.raises(maintenance.MaintenanceError, match="limpio"):
        maintenance.inspect(candidate, BASE, CANDIDATE)
    with pytest.raises(maintenance.MaintenanceError, match="HEAD"):
        maintenance.inspect(candidate, BASE, BASE)


def test_old_source_pin_cannot_hide_new_code_or_rag_document(candidate: Path) -> None:
    (candidate / "README.md").write_text("changed RAG source\n")
    head = commit(candidate)
    with pytest.raises(maintenance.MaintenanceError, match="commit de origen"):
        maintenance.inspect(candidate, BASE, head)


def test_duplicate_manifest_keys_fail(candidate: Path) -> None:
    manifest = candidate / maintenance.MANIFEST
    raw = manifest.read_text()
    manifest.write_text('{"release_id": "duplicate",' + raw[1:])
    head = commit(candidate)
    with pytest.raises(maintenance.MaintenanceError, match="JSON inválido"):
        maintenance.inspect(candidate, BASE, head)


def test_git_replacement_does_not_change_the_result(candidate: Path) -> None:
    before = maintenance.inspect(candidate, BASE, CANDIDATE)
    old_blob = git(candidate, "rev-parse", f"{CANDIDATE}:{maintenance.CARD}")
    other_blob = git(candidate, "hash-object", "-w", "reports/governance/key_facts.json")
    git(candidate, "replace", old_blob, other_blob)
    assert maintenance.inspect(candidate, BASE, CANDIDATE) == before


def test_cli_has_no_publication_operation(candidate: Path, capsys: pytest.CaptureFixture, monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv", ["check_maintenance_release", "--repo", str(candidate), "--base", BASE, "--candidate", CANDIDATE]
    )
    assert maintenance.main() == 0
    assert json.loads(capsys.readouterr().out)["authorizes_publication"] is False
    assert not hasattr(maintenance, "publish")


@pytest.mark.parametrize("raw", [b"[]", b"{", b"\xff", b'{"a": 1, "a": 2}'])
def test_invalid_json_is_rejected(raw: bytes) -> None:
    with pytest.raises(maintenance.MaintenanceError):
        maintenance._json(raw)


def test_removed_artifact_and_extra_report_fail(candidate: Path) -> None:
    (candidate / "reports/governance/ingestion_state.json").unlink()
    head = commit(candidate)
    with pytest.raises(maintenance.MaintenanceError, match="científico"):
        maintenance.inspect(candidate, BASE, head)


def test_executable_manifest_is_not_eligible(candidate: Path) -> None:
    (candidate / maintenance.MANIFEST).chmod(0o755)
    head = commit(candidate)
    with pytest.raises(maintenance.MaintenanceError, match="regular"):
        maintenance.inspect(candidate, BASE, head)


@pytest.mark.parametrize("change", ["digest", "size", "bool-size"])
def test_forged_identity_or_size_is_rejected(candidate: Path, change: str) -> None:
    path = candidate / maintenance.MANIFEST
    body = json.loads(path.read_bytes())
    if change == "digest":
        body["identity"]["digest"] = "0" * 64
    else:
        # La tarjeta está exceptuada del cotejo con la base. Sólo la guarda de
        # tamaño puede rechazar este caso: otro artefacto lo rechazaría también
        # por cambio de metadatos, ocultando la pérdida de esa guarda.
        next(a for a in body["artifacts"] if a["path"] == maintenance.CARD)["size"] = (
            True if change == "bool-size" else 999999
        )
    path.write_text(json.dumps(body))
    head = commit(candidate)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.inspect(candidate, BASE, head)


def test_missing_blob_is_blocked(candidate: Path, monkeypatch) -> None:
    real = maintenance._git

    def unreadable(repo, *args):
        if args[:2] == ("cat-file", "blob"):
            raise maintenance.MaintenanceError("blob ilegible")
        return real(repo, *args)

    monkeypatch.setattr(maintenance, "_git", unreadable)
    with pytest.raises(maintenance.MaintenanceError, match="blob ilegible"):
        maintenance.inspect(candidate, BASE, CANDIDATE)


@pytest.mark.parametrize("change", ["missing-contract-key", "extra-artifact", "removed-artifact", "metadata", "source"])
def test_even_a_resealed_inventory_cannot_expand_maintenance(candidate: Path, change: str) -> None:
    manifest = json.loads((candidate / maintenance.MANIFEST).read_bytes())
    paths = [entry["path"] for entry in manifest["artifacts"]]
    if change == "missing-contract-key":
        contract = candidate / maintenance.CONTRACT
        body = json.loads(contract.read_bytes())
        del body["required_keys"]["reason"]
        contract.write_text(json.dumps(body))
    elif change == "extra-artifact":
        new = "vp_data/contracts/extra.json"
        (candidate / new).write_text("{}")
        paths.append(new)
    elif change == "removed-artifact":
        paths.remove(maintenance.cc.INGESTION_FEED_PATH)
    source = commit(candidate) if change in ("missing-contract-key", "extra-artifact") else CANDIDATE
    seal(candidate, paths, source, "re-sealed adversarial metadata")
    if change == "metadata":
        altered = json.loads((candidate / maintenance.MANIFEST).read_bytes())
        next(a for a in altered["artifacts"] if a["path"] == maintenance.cc.INGESTION_FEED_PATH)["criticality"] = (
            "optional"
        )
        (candidate / maintenance.MANIFEST).write_text(json.dumps(altered))
    elif change == "source":
        altered = json.loads((candidate / maintenance.MANIFEST).read_bytes())
        altered["git_sha"] = BASE[:12]  # anterior al contrato añadido, aunque resoluble
        (candidate / maintenance.MANIFEST).write_text(json.dumps(altered))
    head = commit(candidate)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.inspect(candidate, BASE, head)


@pytest.fixture
def review_package(candidate: Path) -> tuple[Path, dict, dict, dict]:
    inspection = maintenance.inspect(candidate, BASE, CANDIDATE)
    ci = {
        "schema": "metadata-maintenance-ci-capture/1",
        "repository": "UACJ-MIAAD/VisaPredictAI",
        "workflow": ".github/workflows/ci.yml",
        "head_sha": CANDIDATE,
        "tested_tree_sha": inspection["candidate_tree"],
        "run_id": 123,
        "completed_at": "2026-10-20T10:00:00Z",
        "jobs": dict.fromkeys(maintenance.CI_JOBS, "success"),
    }
    review = {
        **{k: inspection[k] for k in maintenance.REVIEW_BINDINGS},
        "schema": "metadata-maintenance-review/1",
        "decision": "aprobada",
        "reviewed_by": "Fixture Persona",
        "reviewed_at": "2026-10-20T10:01:00Z",
        "ci_run_id": 123,
    }
    return candidate, inspection, review, ci


def test_review_and_ci_hashes_describe_the_same_single_read(review_package) -> None:
    _, inspection, review, ci = review_package
    raw_review, raw_ci = json.dumps(review).encode(), json.dumps(ci).encode()
    result = maintenance.check_review_bindings(inspection, raw_review, raw_ci)
    assert result == {
        "review_sha256": hashlib.sha256(raw_review).hexdigest(),
        "ci_capture_sha256": hashlib.sha256(raw_ci).hexdigest(),
    }
    assert inspection["authorizes_publication"] is False


def test_ci_must_identify_the_tree_actually_tested_not_only_event_head(review_package) -> None:
    _, inspection, review, ci = review_package
    ci.pop("tested_tree_sha", None)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.check_review_bindings(inspection, json.dumps(review).encode(), json.dumps(ci).encode())


@pytest.mark.parametrize("key", maintenance.REVIEW_BINDINGS)
def test_another_reviewed_identity_is_rejected(review_package, key: str) -> None:
    _, inspection, review, ci = review_package
    review[key] = "another identity"
    with pytest.raises(maintenance.MaintenanceError, match="otro candidato"):
        maintenance.check_review_bindings(inspection, json.dumps(review).encode(), json.dumps(ci).encode())


@pytest.mark.parametrize(
    "change",
    [
        "sha",
        "tree",
        "run",
        "bool-run",
        "failed-job",
        "missing-job",
        "extra-job",
        "repo",
        "workflow",
        "schema",
        "extra-key",
        "review-bool-run",
        "review-bot",
        "review-github",
        "review-noreply",
        "review-short",
        "rejected",
        "review-schema",
        "review-extra",
        "before-ci",
        "naive",
        "future",
    ],
)
def test_ci_or_review_mismatch_is_never_a_compatible_package(review_package, change: str) -> None:
    _, inspection, review, ci = review_package
    if change == "sha":
        ci["head_sha"] = "a" * 40
    elif change == "tree":
        ci["tested_tree_sha"] = "0" * 40
    elif change == "run":
        ci["run_id"] = 321
    elif change == "bool-run":
        ci["run_id"] = True
    elif change == "failed-job":
        ci["jobs"]["model-tests"] = "failure"
    elif change == "missing-job":
        del ci["jobs"]["model-tests"]
    elif change == "extra-job":
        ci["jobs"]["extra"] = "success"
    elif change == "repo":
        ci["repository"] = "other/repository"
    elif change == "workflow":
        ci["workflow"] = "other.yml"
    elif change == "schema":
        ci["schema"] = "another-schema"
    elif change == "extra-key":
        ci["extra"] = True
    elif change == "review-bool-run":
        review["ci_run_id"] = True
        ci["run_id"] = 1
    elif change == "review-bot":
        review["reviewed_by"] = "release bot"
    elif change == "review-github":
        review["reviewed_by"] = "GitHub Actions"
    elif change == "review-noreply":
        review["reviewed_by"] = "noreply reviewer"
    elif change == "review-short":
        review["reviewed_by"] = "A B"
    elif change == "rejected":
        review["decision"] = "rechazada"
    elif change == "review-schema":
        review["schema"] = "campaign-validation-receipt/3"
    elif change == "review-extra":
        review["extra"] = True
    elif change == "before-ci":
        review["reviewed_at"] = "2026-10-20T09:59:00Z"
    elif change == "naive":
        review["reviewed_at"] = "2026-10-20T10:01:00"
    elif change == "future":
        review["reviewed_at"] = "2099-01-01T10:01:00Z"
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.check_review_bindings(inspection, json.dumps(review).encode(), json.dumps(ci).encode())


def test_even_matching_review_and_ci_do_not_enable_a_publisher(
    review_package, tmp_path: Path, monkeypatch, capsys
) -> None:
    repo, _, review, ci = review_package
    review_path, ci_path = tmp_path / "review.json", tmp_path / "ci.json"
    review_path.write_text(json.dumps(review))
    ci_path.write_text(json.dumps(ci))
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_maintenance_release",
            "--repo",
            str(repo),
            "--base",
            BASE,
            "--candidate",
            CANDIDATE,
            "--review",
            str(review_path),
            "--ci",
            str(ci_path),
        ],
    )
    assert maintenance.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["authorizes_publication"] is False and "evidence_bindings" in output
    assert git(repo, "status", "--porcelain") == ""
