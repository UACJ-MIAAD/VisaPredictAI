"""Operación nominal: autoridades sintéticas, Git real, ningún permiso de producción."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests import test_c2_maintenance as legacy
from tests.test_c2_maintenance import candidate, commit, git  # noqa: F401 — fixture

ROOT = Path(__file__).resolve().parents[1]


def test_workflow_records_the_actual_checkout_in_every_required_job() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    for name in legacy.maintenance.CI_JOBS:
        steps = workflow["jobs"][name]["steps"]
        record = [s for s in steps if s.get("name") == "Record maintenance checkout identity"]
        assert len(record) == 1, f"{name}: falta identidad real del checkout"
        assert "git --no-replace-objects rev-parse HEAD" in record[0]["run"]
        assert "VP_MAINTENANCE_CHECKOUT" in record[0]["run"]


def test_contract_is_adopted_but_this_order_does_not_approve_a_release() -> None:
    text = (ROOT / "docs/adr/0005-metadata-maintenance.md").read_text()
    assert "Estado: adoptado" in text
    assert "no autoriza la publicación de un candidato" in text


@pytest.fixture
def world(candidate: Path, tmp_path: Path, monkeypatch, request) -> dict:  # noqa: F811
    op = importlib.import_module("tools.maintenance_release")
    gh = importlib.import_module("tools.maintenance_github")
    base, head = legacy.BASE, legacy.CANDIDATE
    if getattr(request, "param", None) == "dvc":
        from tests.conftest import requiere

        requiere(modulos=("dvc",))
        dvc = Path(sys.executable).parent / "dvc"
        remote = tmp_path / "dvc-remote"
        remote.mkdir()

        def run_dvc(*args):
            subprocess.run([str(dvc), *args], cwd=candidate, capture_output=True, check=True)

        run_dvc("init", "-q")
        run_dvc("remote", "add", "-d", "fixture", str(remote))
        (candidate / "models").mkdir()
        (candidate / "models/model.bin").write_bytes(b"published unchanged weights\n")
        run_dvc("add", "models")
        run_dvc("push")
        paths = [
            a["path"]
            for a in json.loads((candidate / op.check.MANIFEST).read_bytes())["artifacts"]
            if a["path"] != op.check.CONTRACT
        ]
        (candidate / op.check.CONTRACT).unlink()
        source = commit(candidate)
        legacy.seal(candidate, paths, source, "synthetic published base with DVC")
        base = commit(candidate)
        (candidate / op.check.CONTRACT).write_text(json.dumps(op.check.CONTRACT_V1))
        source = commit(candidate)
        legacy.seal(candidate, [*paths, op.check.CONTRACT], source, "synthetic metadata-only successor")
        head = commit(candidate)
        monkeypatch.setattr(legacy, "BASE", base)
        monkeypatch.setattr(legacy, "CANDIDATE", head)
    manifest = git(candidate, "show", base + ":" + op.check.MANIFEST)
    monkeypatch.setattr(op, "BASE", base)
    monkeypatch.setattr(op, "BASE_RELEASE", json.loads(manifest)["release_id"])
    # git() strips whitespace; use the original blob for the exact policy pin.
    monkeypatch.setattr(
        op,
        "BASE_MANIFEST_SHA256",
        hashlib.sha256(op.check._git(candidate, "show", base + ":" + op.check.MANIFEST)).hexdigest(),
    )
    registry = tmp_path / "registry"
    identity = op.initialize(registry)
    inspection = op.inspect_candidate(candidate, head)
    review = {key: inspection[key] for key in op.check.REVIEW_BINDINGS}
    review.update(
        schema="metadata-maintenance-review/1",
        decision="aprobada",
        reviewed_by="Javier Rebull",
        reviewed_at="2026-10-02T00:00:00Z",
        ci_run_id=1,
    )
    raw = json.dumps(review).encode()

    GitHubBase: Any = gh.GitHub

    class Fake(GitHubBase):
        def __init__(self):
            self.responses: dict[str, bytes] = {}
            self.calls: list[str] = []

        def read(self, path: str) -> bytes:
            self.calls.append(path)
            if path not in self.responses:
                raise op.check.MaintenanceError("fixture: GitHub no disponible")
            return self.responses[path]

        def put(self, path: str, value: dict) -> None:
            self.responses[path] = json.dumps(value).encode()

        def run(self, run_id: int, sha: str) -> None:
            tree = git(candidate, "rev-parse", sha + "^{tree}")
            self.put(
                f"actions/runs/{run_id}",
                {
                    "id": run_id,
                    "repository": {"full_name": gh.REPOSITORY},
                    "path": ".github/workflows/ci.yml",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": sha,
                    "run_attempt": 1,
                    "updated_at": "2026-10-01T00:00:00Z",
                },
            )
            jobs = []
            for index, name in enumerate(sorted(op.check.CI_JOBS)):
                job_id = run_id * 100 + index
                jobs.append(
                    {
                        "id": job_id,
                        "run_id": run_id,
                        "name": name,
                        "status": "completed",
                        "conclusion": "success",
                        "steps": [{"name": gh.STEP, "conclusion": "success"}],
                    }
                )
                self.responses[f"actions/jobs/{job_id}/logs"] = (
                    f"timestamp VP_MAINTENANCE_CHECKOUT {sha} {tree}\n".encode()
                )
            self.put(f"actions/runs/{run_id}/attempts/1/jobs?per_page=100", {"total_count": 4, "jobs": jobs})
            self.put(f"git/commits/{sha}", {"sha": sha, "tree": {"sha": tree}})

    fake = Fake()
    fake.run(1, head)
    fake.put(
        "pulls/7",
        {
            "head": {"sha": head},
            "base": {"ref": "main", "repo": {"full_name": gh.REPOSITORY}},
            "merged": False,
            "merge_commit_sha": None,
        },
    )
    fake.put(
        "issues/comments/9",
        {
            "id": 9,
            "user": {"id": 168046724, "type": "User", "login": "jrebull"},
            "body": json.dumps(op.authorization_request(identity["publisher_id"], raw)),
            "created_at": "2026-10-03T00:00:00Z",
            "updated_at": "2026-10-03T00:00:00Z",
            "issue_url": f"https://api.github.com/repos/{gh.REPOSITORY}/issues/7",
        },
    )
    monkeypatch.setattr(op, "GitHub", lambda: fake)
    return dict(
        op=op,
        gh=gh,
        fake=fake,
        repo=candidate,
        registry=registry,
        identity=identity,
        inspection=inspection,
        review=raw,
        head=head,
        tmp=tmp_path,
    )


def register(w: dict) -> dict:
    return w["op"].register(w["repo"], w["registry"], w["head"], w["review"], 1, 9)


def integrate(w: dict, monkeypatch) -> str:

    origin = w["tmp"] / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git(w["repo"], "remote", "add", "origin", str(origin))
    tree = git(w["repo"], "rev-parse", w["head"] + "^{tree}")
    merged = git(w["repo"], "commit-tree", tree, "-p", legacy.BASE, "-m", "synthetic squash")
    git(w["repo"], "checkout", "--detach", "-q", merged)
    git(w["repo"], "push", "-q", "origin", merged + ":refs/heads/main")
    release = w["inspection"]["release_id"]
    git(w["repo"], "tag", "-a", "release/" + release, w["inspection"]["source_commit_full"], "-m", "synthetic release")
    git(w["repo"], "push", "-q", "origin", "refs/tags/release/" + release)
    monkeypatch.setattr(w["op"], "ORIGINS", (str(origin),))
    w["fake"].run(2, merged)
    pull = w["gh"].loads(w["fake"].responses["pulls/7"])
    pull.update(merged=True, merge_commit_sha=merged)
    w["fake"].put("pulls/7", pull)
    return merged


def test_full_register_squash_tag_and_single_consumption(world: dict, monkeypatch) -> None:
    w = world
    record = register(w)
    assert record["status"] == "approved"
    merged = integrate(w, monkeypatch)
    result = w["op"].consume(w["repo"], w["registry"], w["inspection"]["release_id"], 2)
    assert result["status"] == "consumed" and result["revision"] == 1
    assert result["events"][-1]["payload"]["release_commit"] == merged
    before = list(w["fake"].calls)
    with pytest.raises(w["op"].Blocked, match="consumid"):
        w["op"].consume(w["repo"], w["registry"], w["inspection"]["release_id"], 2)
    assert w["fake"].calls == before
    assert git(w["repo"], "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "change",
    [
        "ci-red",
        "ci-head",
        "ci-tree",
        "ci-no-log",
        "ci-no-step",
        "ci-repo",
        "comment-bot",
        "comment-id",
        "comment-body",
        "comment-other-pr",
        "comment-pr-head",
        "comment-early",
        "unavailable",
    ],
)
def test_bad_authority_cannot_register(world: dict, change: str) -> None:
    w = world
    f = w["fake"]
    gh = w["gh"]
    if change.startswith("ci-"):
        if change in {"ci-red", "ci-head", "ci-repo"}:
            v = gh.loads(f.responses["actions/runs/1"])
            if change == "ci-red":
                v["conclusion"] = "failure"
            if change == "ci-head":
                v["head_sha"] = "f" * 40
            if change == "ci-repo":
                v["repository"]["full_name"] = "other/repo"
            f.put("actions/runs/1", v)
        elif change == "ci-tree":
            f.responses["actions/jobs/100/logs"] = f"VP_MAINTENANCE_CHECKOUT {w['head']} {'f' * 40}\n".encode()
        elif change == "ci-no-log":
            del f.responses["actions/jobs/100/logs"]
        else:
            v = gh.loads(f.responses["actions/runs/1/attempts/1/jobs?per_page=100"])
            v["jobs"][0]["steps"] = []
            f.put("actions/runs/1/attempts/1/jobs?per_page=100", v)
    elif change == "unavailable":
        f.responses.clear()
    else:
        v = gh.loads(f.responses["issues/comments/9"])
        if change == "comment-bot":
            v["user"]["type"] = "Bot"
        if change == "comment-id":
            v["user"]["id"] = 1
        if change == "comment-body":
            v["body"] = "aprobada"
        if change == "comment-other-pr":
            v["issue_url"] = "https://api.github.com/repos/other/repo/issues/7"
        if change == "comment-early":
            v["created_at"] = "2026-09-01T00:00:00Z"
        if change == "comment-pr-head":
            pull = gh.loads(f.responses["pulls/7"])
            pull["head"]["sha"] = "f" * 40
            f.put("pulls/7", pull)
        f.put("issues/comments/9", v)
    with pytest.raises(w["op"].Blocked):
        register(w)
    assert not (w["registry"] / (w["inspection"]["release_id"] + ".json")).exists()


@pytest.mark.parametrize(
    "change", ["dirty", "head", "tag", "main", "origin", "integration-ci", "approval-edited", "unmerged"]
)
def test_consumption_checks_again_before_writing(world: dict, monkeypatch, change: str) -> None:
    w = world
    register(w)
    integrate(w, monkeypatch)
    if change == "dirty":
        (w["repo"] / "unreviewed").write_text("new")
    if change == "head":
        (w["repo"] / "new").write_text("new")
        commit(w["repo"])
    if change == "tag":
        git(w["repo"], "push", "-q", "origin", ":refs/tags/release/" + w["inspection"]["release_id"])
    if change == "main":
        advanced = git(
            w["repo"],
            "commit-tree",
            git(w["repo"], "rev-parse", "HEAD^{tree}"),
            "-p",
            git(w["repo"], "rev-parse", "HEAD"),
            "-m",
            "synthetic concurrent main advance",
        )
        git(w["repo"], "push", "-q", "origin", advanced + ":refs/heads/main")
    if change == "origin":
        other = w["tmp"] / "untrusted-origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(w["tmp"] / "origin.git"), str(other)], check=True)
        git(w["repo"], "remote", "set-url", "origin", str(other))
    if change == "integration-ci":
        v = w["gh"].loads(w["fake"].responses["actions/runs/2"])
        v["conclusion"] = "failure"
        w["fake"].put("actions/runs/2", v)
    if change in {"approval-edited", "unmerged"}:
        key = "issues/comments/9" if change == "approval-edited" else "pulls/7"
        v = w["gh"].loads(w["fake"].responses[key])
        v["body" if change == "approval-edited" else "merged"] = "revoked" if change == "approval-edited" else False
        w["fake"].put(key, v)
    with pytest.raises(w["op"].Blocked):
        w["op"].consume(w["repo"], w["registry"], w["inspection"]["release_id"], 2)
    assert w["op"].status(w["registry"], w["inspection"]["release_id"])["status"] == "approved"


def test_two_consumers_share_one_lock(world: dict, monkeypatch) -> None:
    w = world
    register(w)
    integrate(w, monkeypatch)

    def consume():
        try:
            return w["op"].consume(w["repo"], w["registry"], w["inspection"]["release_id"], 2)["status"]
        except w["op"].Blocked:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: consume(), range(2))) == ["blocked", "consumed"]


@pytest.mark.parametrize("change", ["copy", "symlink", "hardlink", "mode", "tamper"])
def test_registry_cannot_be_relocated_or_silently_corrupted(world: dict, change: str) -> None:
    w = world
    register(w)
    registry = w["registry"]
    release = w["inspection"]["release_id"]
    path = registry / (release + ".json")
    if change == "copy":
        other = w["tmp"] / "other-registry"
        shutil.copytree(registry, other)
        registry = other
    elif change == "symlink":
        raw = path.read_bytes()
        path.unlink()
        other = w["tmp"] / "other"
        other.write_bytes(raw)
        other.chmod(0o600)
        path.symlink_to(other)
    elif change == "hardlink":
        (registry / "hardlink").hardlink_to(path)
    elif change == "mode":
        path.chmod(0o644)
    else:
        v = json.loads(path.read_bytes())
        v["events"][0]["payload"]["comment_id"] = 10
        path.write_text(json.dumps(v))
    with pytest.raises(w["op"].Blocked):
        w["op"].status(registry, release)


def test_register_is_idempotent_for_the_same_proof(world: dict) -> None:
    first = register(world)
    second = register(world)
    assert first == second and second["revision"] == 0


@pytest.mark.parametrize("world", ["dvc"], indirect=True)
def test_nominal_operation_preserves_already_published_dvc_objects(world: dict, monkeypatch) -> None:
    w = world
    remote = w["tmp"] / "dvc-remote"

    def objects():
        return {
            p.relative_to(remote).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in remote.rglob("*")
            if p.is_file()
        }

    before = objects()
    assert before
    register(w)
    integrate(w, monkeypatch)
    result = w["op"].consume(w["repo"], w["registry"], w["inspection"]["release_id"], 2)
    assert result["status"] == "consumed" and objects() == before
    assert git(w["repo"], "status", "--porcelain") == ""


@pytest.mark.parametrize("at", [1, 2])
def test_fsync_failure_preserves_or_reports_committed_consumption(world: dict, monkeypatch, at: int) -> None:
    import os

    w = world
    register(w)
    integrate(w, monkeypatch)
    original = os.fsync
    calls = 0

    def fail_once(fd):
        nonlocal calls
        calls += 1
        if calls == at:
            raise OSError("synthetic fsync failure")
        return original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_once)
        with pytest.raises(w["op"].Blocked):
            w["op"].consume(w["repo"], w["registry"], w["inspection"]["release_id"], 2)
    state = w["op"].status(w["registry"], w["inspection"]["release_id"])
    assert state["status"] == ("approved" if at == 1 else "consumed")
    assert not list(w["registry"].glob(".pending-*"))


def test_partial_writes_and_stale_compare_are_not_silently_accepted(world: dict, monkeypatch) -> None:
    import os

    w = world
    original = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: original(fd, data[:17]))
    record = register(w)
    assert record["status"] == "approved"
    storage = importlib.import_module("tools.maintenance_registry")
    name = w["inspection"]["release_id"] + ".json"
    with storage.Registry(w["registry"]) as registry:
        raw = registry.read(name)
        (w["registry"] / name).write_bytes(raw + b" ")
        with pytest.raises(w["op"].Blocked, match="CAS"):
            registry.write(name, record, raw)


@pytest.mark.parametrize("change", ["boolean-run", "bad-jobs", "bad-user"])
def test_malformed_remote_values_fail_closed_with_diagnostic(world: dict, change: str) -> None:
    w = world
    if change == "boolean-run":
        key = "actions/runs/1"
        value = w["gh"].loads(w["fake"].responses[key])
        value["id"] = True
    elif change == "bad-jobs":
        key = "actions/runs/1/attempts/1/jobs?per_page=100"
        value = {"total_count": 4, "jobs": [None] * 4}
    else:
        key = "issues/comments/9"
        value = w["gh"].loads(w["fake"].responses[key])
        value["user"] = []
    w["fake"].put(key, value)
    with pytest.raises(w["op"].Blocked):
        register(w)


def test_registry_reads_detect_same_inode_mutation(world: dict, monkeypatch) -> None:
    import os

    w = world
    register(w)
    target = w["registry"] / (w["inspection"]["release_id"] + ".json")
    original = os.read
    changed = False

    def mutate(fd, size):
        nonlocal changed
        raw = original(fd, size)
        if raw and not changed:
            changed = True
            info = target.stat()
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        return raw

    with monkeypatch.context() as patch:
        # status reads identity first, then the state; only mutate during state read.
        storage = importlib.import_module("tools.maintenance_registry")
        with storage.Registry(w["registry"]) as registry:
            patch.setattr(os, "read", mutate)
            with pytest.raises(w["op"].Blocked, match="durante lectura"):
                registry.read(target.name)


def test_registry_cannot_be_initialized_inside_the_candidate(world: dict) -> None:
    with pytest.raises(world["op"].Blocked, match="fuera"):
        world["op"].initialize(world["repo"] / "registry")
    assert not (world["repo"] / "registry").exists()


def test_writer_never_creates_a_record_larger_than_its_reader_limit(world: dict) -> None:
    storage = importlib.import_module("tools.maintenance_registry")
    with storage.Registry(world["registry"]) as registry:
        with pytest.raises(world["op"].Blocked, match="demasiado grande"):
            registry.write("oversized.json", {"value": "a" * (1024 * 1024)}, None)
        assert registry.read("oversized.json") is None


def test_ci_evidence_is_stable_between_different_cli_processes(world: dict) -> None:
    w = world
    fixtures = w["tmp"] / "github-fixtures.json"
    fixtures.write_text(json.dumps({key: value.hex() for key, value in w["fake"].responses.items()}))
    code = (
        "import json,sys; from pathlib import Path; from tools.maintenance_github import GitHub; "
        "responses=json.loads(Path(sys.argv[1]).read_text()); api=GitHub(); "
        "api.read=lambda key:bytes.fromhex(responses[key]); "
        "capture=api.capture(1,sys.argv[2],sys.argv[3]); "
        "print(json.dumps({'capture':capture.decode(),'proofs':api.last_proofs},sort_keys=True))"
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code, str(fixtures), w["head"], w["inspection"]["candidate_tree"]],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONHASHSEED": str(seed)},
        )
        for seed in (3, 9)
    ]
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("change", ["release", "manifest"])
def test_adopted_base_is_pinned_independently_of_the_caller(world: dict, monkeypatch, change: str) -> None:
    key = "BASE_RELEASE" if change == "release" else "BASE_MANIFEST_SHA256"
    monkeypatch.setattr(world["op"], key, "f" * 64)
    with pytest.raises(world["op"].Blocked, match="base|manifiesto"):
        register(world)
