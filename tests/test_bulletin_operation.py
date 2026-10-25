"""Manual PDF orchestration keeps publication explicit and transports fail closed."""

from __future__ import annotations

import base64
import concurrent.futures
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest


def module():
    from pipeline import bulletin

    return bulletin


def test_prepare_never_publishes(tmp_path, monkeypatch):
    b = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    called = []
    monkeypatch.setattr(b, "prepare", lambda month, file: receipt)
    monkeypatch.setattr(b, "validate_receipt", lambda path, month: {"month": month})
    monkeypatch.setattr(b, "publish", lambda *args, **kwargs: called.append(args))
    assert b.main(["--month", "2026-11"]) == 0
    assert called == []


def test_publish_needs_explicit_existing_receipt(tmp_path):
    b = module()
    with pytest.raises(ValueError, match="receipt"):
        b.main(["--month", "2026-11", "--publish"])


def test_failed_receipt_is_rejected_before_transport(tmp_path, monkeypatch):
    b = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"schema": "visapredict.pdf-intake/1", "status": "failed"}))
    monkeypatch.setattr(b, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        b.validate_receipt(receipt, "2026-11")


def test_s3_existing_identical_is_noop_and_conflict_never_puts(tmp_path):
    b = module()
    source = tmp_path / "bulletin.html"
    source.write_bytes(b"validated snapshot")

    class Fake:
        puts = 0

        def s3_get(self, key):
            return b"validated snapshot"

        def s3_put(self, key, path):
            self.puts += 1

    fake = Fake()
    assert b.archive_snapshot(fake, "key", source) == "identical"
    assert fake.puts == 0
    source.write_bytes(b"conflict")
    with pytest.raises(ValueError, match="S3"):
        b.archive_snapshot(fake, "key", source)
    assert fake.puts == 0


def test_s3_create_race_rechecks_bytes(tmp_path):
    b = module()
    source = tmp_path / "bulletin.html"
    source.write_bytes(b"snapshot")

    class Fake:
        reads = 0

        def s3_get(self, key):
            self.reads += 1
            return None if self.reads == 1 else b"different concurrent object"

        def s3_put(self, key, path):
            return None

    with pytest.raises(ValueError, match="S3"):
        b.archive_snapshot(Fake(), "key", source)


def test_terminal_failure_never_becomes_green():
    b = module()
    with pytest.raises(ValueError, match="failure"):
        b.require_success({"status": "completed", "conclusion": "failure"})
    assert b.require_success({"status": "completed", "conclusion": "success"})
    assert not b.require_success({"status": "in_progress", "conclusion": None})


def test_run_selection_rejects_another_dispatch():
    b = module()
    runs = [{"id": 3, "display_title": "unrelated", "head_sha": "a" * 40, "head_branch": "main"}]
    assert b.match_runs(runs, "request", "a" * 40) == []
    runs[0]["display_title"] = "Assisted bulletin request"
    assert b.match_runs(runs, "request", "b" * 40) == []
    assert b.match_runs(runs, "request", "a" * 40) == [runs[0]]


def test_dispatch_wait_timeout_is_failure(monkeypatch):
    b = module()
    clock = iter([0, 1, 100])
    monkeypatch.setattr(b.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(b.time, "sleep", lambda seconds: None)

    class Fake:
        def api(self, path):
            return {"workflow_runs": []}

    with pytest.raises(TimeoutError):
        b.find_dispatch(Fake(), "request", "a" * 40, seconds=10)


def telemetry(folder, run_id):
    b = module()
    folder.mkdir(parents=True, exist_ok=True)
    contents = {
        "tracking_monthly.txt": b"ok original summary\n",
        "web_forecast_scoring.jsonl": json.dumps(
            {"experiment": "web_forecast_scoring", "provenance": {"pipeline_run_id": str(run_id)}}
        ).encode()
        + b"\n",
    }
    for name, content in contents.items():
        (folder / name).write_bytes(content)
    receipt = {
        "schema": "scoring-telemetry-archive/1",
        "complete": True,
        "run_id": str(run_id),
        "missing": [],
        "records": 1,
        "files": {n: {"sha256": b.sha(c), "bytes": len(c)} for n, c in contents.items()},
    }
    (folder / "receipt.json").write_text(json.dumps(receipt))


class MonthlyFake:
    def __init__(self, vintage="2026-10", conclusion="success"):
        self.vintage, self.conclusion = vintage, conclusion
        self.commands = []
        self.request = ""
        self.completed = False

    def api(self, path):
        if path.startswith("contents/"):
            body = self.manifest()
            return {"content": base64.b64encode(body).decode()}
        if path == "commits/main":
            return {"sha": "a" * 40}
        run = {
            "id": 42,
            "display_title": f"Assisted bulletin {self.request}",
            "head_sha": "a" * 40,
            "head_branch": "main",
            "status": "completed",
            "conclusion": self.conclusion,
        }
        return {"workflow_runs": [run]} if "workflows/" in path else run

    def manifest(self):
        b = module()
        entries = [
            {"path": path, "sha256": b.sha(b"content"), "size": 7}
            for path in (
                "data/processed/visa_panel_long.csv",
                "reports/prospective/web_forecasts.csv",
                "reports/governance/ingestion_state.json",
            )
        ]
        return json.dumps(
            {
                "panel_vintage": "2026-11" if self.completed else self.vintage,
                "pipeline_run_id": "42" if self.completed else "previous",
                "release_id": "release-fixture",
                "artifacts": entries,
            }
        ).encode()

    def public_get(self, url):
        if url.endswith("release-state.json"):
            return b'{"status":"fresh","release_id":"release-fixture"}'
        return self.manifest() if url.endswith("release_manifest.json") else b"content"

    def command(self, args, timeout=60):
        self.commands.append(args)
        if args[1:3] == ["workflow", "run"]:
            self.request = next(arg.split("=", 1)[1] for arg in args if arg.startswith("operator_request_id="))
            self.completed = True
        else:
            telemetry(Path(args[-1]), 42)
        return b""

    def s3_get(self, key):
        return b"valid"


def evidence(tmp_path):
    b = module()
    snapshot = tmp_path / "bulletin.html"
    snapshot.write_bytes(b"valid")
    return {
        "archive": tmp_path,
        "html": snapshot,
        "html_sha": b.sha(b"valid"),
        "receipt_sha": "1" * 64,
        "source_sha": "a" * 40,
        "month": "2026-11",
    }


def test_existing_sealed_month_never_dispatches(tmp_path):
    b = module()
    fake = MonthlyFake(vintage="2026-11")
    result = b.publish(evidence(tmp_path), fake)
    assert json.loads(result.read_text())["status"] == "already_sealed_no_dispatch"
    assert fake.commands == []


def test_explicit_publication_pins_run_and_preserves_original_telemetry(tmp_path):
    b = module()
    fake = MonthlyFake()
    result = b.publish(evidence(tmp_path), fake)
    saved = json.loads(result.read_text())
    assert saved["status"] == "monthly_success_consumers_verified"
    assert saved["run_id"] == 42 and len(saved["telemetry"]) == 3
    assert len([c for c in fake.commands if c[1:3] == ["workflow", "run"]]) == 1


def test_failed_delivery_keeps_artifact_and_never_passes(tmp_path):
    b = module()
    with pytest.raises(ValueError, match="failure"):
        b.publish(evidence(tmp_path), MonthlyFake(conclusion="failure"))
    saved = json.loads((tmp_path / "publication/operation.json").read_text())
    assert saved["status"] == "blocked_inspect_recorded_run"
    assert saved["conclusion"] == "failure" and len(saved["telemetry"]) == 3


def test_transport_error_propagates_without_dispatch(tmp_path):
    b = module()

    class Failed(MonthlyFake):
        def s3_get(self, key):
            raise RuntimeError("authentication failure")

    fake = Failed()
    with pytest.raises(RuntimeError):
        b.publish(evidence(tmp_path), fake)
    assert fake.commands == []


def test_paths_outside_archive_and_symlinks_are_rejected(tmp_path):
    b = module()
    archive = tmp_path / "archive"
    archive.mkdir()
    outside = tmp_path / "secret"
    outside.write_bytes(b"not part of receipt")
    with pytest.raises(ValueError):
        b.checked_file(archive / ".." / "secret", archive)
    (archive / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        b.checked_file(archive / "link", archive)


def test_original_telemetry_hash_and_run_id_are_required(tmp_path):
    b = module()
    telemetry(tmp_path, 42)
    assert len(b.verify_telemetry(tmp_path, 42)) == 3
    with pytest.raises(ValueError):
        b.verify_telemetry(tmp_path, 43)
    (tmp_path / "tracking_monthly.txt").write_bytes(b"altered")
    with pytest.raises(ValueError):
        b.verify_telemetry(tmp_path, 42)


@pytest.mark.parametrize("target", ["bulletin", "ingest-pdf"])
def test_make_passes_filenames_as_literal_environment_values(tmp_path, target):
    b = module()
    output = tmp_path / "observed.json"
    logger = tmp_path / "logger"
    logger.write_text(
        f"#!{sys.executable}\nimport os,json\nopen({str(output)!r},'w').write(json.dumps(dict(os.environ)))\n"
    )
    logger.chmod(0o700)
    marker = tmp_path / "must-not-exist"
    name = f'/tmp/literal $(touch {marker}) `touch {marker}` "quotes".pdf'
    subprocess.run(
        ["make", target, f"PY={logger}", "MONTH=2026-11", f"FILE={name}", f"RECEIPT={name}"],
        cwd=b.ROOT,
        check=True,
        capture_output=True,
    )
    env = json.loads(output.read_text())
    assert env["BULLETIN_FILE"] == name and env["BULLETIN_RECEIPT"] == name
    assert not marker.exists()


def test_consumer_fresh_identity_and_payload_are_required():
    b = module()

    class Altered(MonthlyFake):
        def public_get(self, url):
            if url.endswith("forecasts.csv"):
                return b"wrong bytes"
            return super().public_get(url)

    with pytest.raises(ValueError, match="artefacto servido"):
        b.verify_consumers(Altered(), Altered().manifest())


def test_missing_consumer_mapping_never_claims_complete():
    b = module()
    manifest = json.loads(MonthlyFake().manifest())
    manifest["artifacts"] = manifest["artifacts"][:1]

    class Partial(MonthlyFake):
        def manifest(self):
            return json.dumps(manifest).encode()

    with pytest.raises(ValueError, match="críticos"):
        b.verify_consumers(Partial(), Partial().manifest())


def test_duplicate_receipt_keys_are_not_an_authorization():
    b = module()
    with pytest.raises(ValueError, match="duplicadas"):
        b.load_json('{"status":"failed","status":"local_candidate_green"}')
    assert b.load_json('{"status":"failed"}') == {"status": "failed"}


def test_changed_html_and_unintegrated_code_block_before_s3(tmp_path):
    b = module()
    supplied = evidence(tmp_path)
    fake = MonthlyFake()
    supplied["source_sha"] = "b" * 40
    with pytest.raises(ValueError, match="main"):
        b.publish(supplied, fake)
    assert fake.commands == []
    supplied["source_sha"] = "a" * 40
    supplied["html"].write_bytes(b"substituted HTML")
    with pytest.raises(ValueError, match="HTML cambió"):
        b.publish(supplied, fake)
    assert fake.commands == []


def test_s3_403_is_not_treated_as_404(monkeypatch):
    b = module()
    from types import SimpleNamespace

    monkeypatch.setattr(b.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr=b"(403) Forbidden"))
    with pytest.raises(RuntimeError, match="HEAD"):
        b.Transport().s3_get("key")
    monkeypatch.setattr(b.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr=b"(404) Not Found"))
    assert b.Transport().s3_get("key") is None


def test_changed_request_id_and_other_run_are_rejected(tmp_path):
    b = module()
    supplied = evidence(tmp_path)
    destination = tmp_path / "publication"
    destination.mkdir()
    state = {
        "schema": "visapredict.assisted-publication/1",
        "month": "2026-11",
        "receipt_sha256": "1" * 64,
        "request": "forged",
        "run_id": 10,
    }
    (destination / "operation.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="otro receipt"):
        b.publish(supplied, MonthlyFake())


@pytest.fixture
def receipt_world(tmp_path, monkeypatch):
    b = module()
    source = b.ROOT / "tests/fixtures/pdf_bulletins/Visa Bulletin For November 2026.pdf"
    producers = ["pipeline/ingest_pdf.py", "pipeline/pdf_bulletin.py", "Makefile"]
    for name in producers:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture producer")
    (tmp_path / ".gitignore").write_text("data/\n")
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "Javier Rebull",
        "GIT_COMMITTER_NAME": "Javier Rebull",
        "GIT_AUTHOR_EMAIL": "168046724+jrebull@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": "168046724+jrebull@users.noreply.github.com",
    }
    for command in (["init", "-q"], ["add", "."], ["commit", "-qm", "Fixture producer"]):
        subprocess.run(["git", *command], cwd=tmp_path, env=env, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path).decode().strip()
    archive = tmp_path / "data/pdf_intake/2026-11/fixture/run"
    archive.mkdir(parents=True)
    (archive / "source.pdf").write_bytes(source.read_bytes())
    text, _ = b.extract_text(archive / "source.pdf")
    cells = b.parse_layout(text, "2026-11")
    html = b.as_html(cells, "2026-11", b.sha(source.read_bytes()))
    for name, content in {
        "bulletin.html": html,
        "cells.json": json.dumps(cells).encode(),
        "extracted.txt": text.encode(),
    }.items():
        (archive / name).write_bytes(content)
    candidate = archive / "candidate"
    for name, content in {
        "data/processed/visa_panel_long.csv": b"panel fixture",
        "data/processed/bulletins.json": b"[]",
        "data/processed/visapredict.duckdb": b"DB fixture",
        "data/snapshots/visa-bulletin-for-november-2026.html": html,
    }.items():
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    receipt = {
        "schema": "visapredict.pdf-intake/1",
        "month": "2026-11",
        "status": "local_candidate_green",
        "publication": "not_requested",
        "candidate_promoted": False,
        "scoring_monthly": "not_executed",
        "source_git_dirty": False,
        "source_git_sha": head,
        "candidate": str(candidate.relative_to(tmp_path)),
        "pdf_sha256": b.sha(source.read_bytes()),
        "producer_files": {n: b.sha(b"fixture producer") for n in producers},
        "files": {str(p.relative_to(archive)): b.sha(p.read_bytes()) for p in archive.rglob("*") if p.is_file()},
    }
    path = archive / "receipt.json"
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(b, "ROOT", tmp_path)
    return b, path, receipt


def test_full_receipt_benign_and_source_reparse(receipt_world):
    b, path, _ = receipt_world
    result = b.validate_receipt(path, "2026-11")
    assert result["html"].name == "visa-bulletin-for-november-2026.html"
    assert result["source_sha"] and result["html_sha"]


@pytest.mark.parametrize("attack", ["pdf", "html", "month", "producer", "head", "omitted", "duplicate", "symlink"])
def test_full_receipt_attacks_never_authorize_transport(receipt_world, attack):
    b, path, receipt = receipt_world
    if attack in {"pdf", "html"}:
        target = path.parent / ("source.pdf" if attack == "pdf" else "bulletin.html")
        target.write_bytes(b"substituted source")
    elif attack == "month":
        receipt["month"] = "2026-10"
    elif attack == "producer":
        receipt["producer_files"]["Makefile"] = "0" * 64
    elif attack == "head":
        (b.ROOT / "Makefile").write_bytes(b"changed producer")
    elif attack == "omitted":
        receipt["files"].pop("candidate/data/processed/visapredict.duckdb")
    elif attack == "symlink":
        snapshot = path.parent / "bulletin.html"
        content = snapshot.read_bytes()
        snapshot.unlink()
        outside = b.ROOT / "outside"
        outside.write_bytes(content)
        snapshot.symlink_to(outside)
    path.write_text(json.dumps(receipt))
    if attack == "duplicate":
        path.write_text(path.read_text()[:-1] + ',"status":"local_candidate_green"}')
    with pytest.raises((ValueError, subprocess.SubprocessError)):
        b.validate_receipt(path, "2026-11")


def test_old_cut_cannot_satisfy_a_new_bulletin_request(tmp_path):
    b = module()

    class OldCut(MonthlyFake):
        def manifest(self):
            result = json.loads(super().manifest())
            result["panel_vintage"] = "2026-10"
            return json.dumps(result).encode()

    with pytest.raises(ValueError, match="release|corte|mes"):
        b.publish(evidence(tmp_path), OldCut())


def test_other_run_cannot_satisfy_new_release(tmp_path):
    b = module()

    class WrongRun(MonthlyFake):
        def manifest(self):
            result = json.loads(super().manifest())
            result["pipeline_run_id"] = "another-run"
            return json.dumps(result).encode()

    with pytest.raises(ValueError, match="release"):
        b.publish(evidence(tmp_path), WrongRun())


def test_changed_main_cannot_be_adopted_between_check_and_dispatch(tmp_path):
    b = module()

    class Moving(MonthlyFake):
        checks = 0

        def api(self, path):
            if path == "commits/main":
                self.checks += 1
                return {"sha": ("a" if self.checks == 1 else "b") * 40}
            result = super().api(path)
            if "workflows/" in path:
                result["workflow_runs"][0]["head_sha"] = "b" * 40
            elif path.startswith("actions/runs/"):
                result["head_sha"] = "b" * 40
            return result

    with pytest.raises(ValueError, match="fuente|HEAD|corrida"):
        b.publish(evidence(tmp_path), Moving())


def test_concurrent_same_receipt_has_one_publisher(tmp_path):
    b = module()
    entered, release = threading.Event(), threading.Event()

    class Paused(MonthlyFake):
        def api(self, path):
            if path == "commits/main":
                entered.set()
                assert release.wait(5)
            return super().api(path)

    fake = Paused(vintage="2026-11")
    supplied = evidence(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(b.publish, supplied, fake)
        assert entered.wait(5)
        second = executor.submit(b.publish, supplied, fake)
        try:
            with pytest.raises(ValueError, match="operación|proceso|publicación"):
                second.result(timeout=1)
        finally:
            release.set()
        assert first.result(timeout=5)


@pytest.mark.parametrize(
    "actual,expected,ok", [("a" * 40, "a" * 40, True), ("b" * 40, "a" * 40, False), ("a" * 40, "malformed", False)]
)
def test_workflow_rejects_changed_source_before_proceed(tmp_path, actual, expected, ok):
    import os

    import yaml

    b = module()
    workflow = yaml.safe_load((b.ROOT / ".github/workflows/freeze_and_rebuild.yml").read_text())
    script = workflow["jobs"]["gate"]["steps"][0]["run"].replace("${{ github.event_name }}", "workflow_dispatch")
    output = tmp_path / "output"
    env = {
        **os.environ,
        "OPERATOR_REQUEST_ID": "fixture-request",
        "EXPECTED_SOURCE_SHA": expected,
        "ACTUAL_SOURCE_SHA": actual,
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True)
    assert (result.returncode == 0) is ok
    assert (output.exists() and "proceed=true" in output.read_text()) is ok


def test_same_receipt_resumes_without_another_dispatch(tmp_path):
    b = module()
    fake = MonthlyFake()
    supplied = evidence(tmp_path)
    b.publish(supplied, fake)
    b.publish(supplied, fake)
    assert len([c for c in fake.commands if c[1:3] == ["workflow", "run"]]) == 1


def test_ingest_alias_forces_prepare_even_with_publish_environment(tmp_path, monkeypatch):
    b = module()
    monkeypatch.setenv("BULLETIN_MONTH", "2026-11")
    monkeypatch.setenv("BULLETIN_PUBLISH", "1")
    monkeypatch.setenv("BULLETIN_RECEIPT", "must-not-be-used")
    receipt = tmp_path / "receipt"
    monkeypatch.setattr(b, "prepare", lambda month, file: receipt)
    monkeypatch.setattr(b, "validate_receipt", lambda path, month: {})
    monkeypatch.setattr(b, "publish", lambda *a, **k: pytest.fail("prepare-only alias tried to publish"))
    assert b.main(["--environment-prepare-only"]) == 0


def test_bound_run_resumes_after_its_cron_advanced_main(tmp_path):
    b = module()

    class Advances(MonthlyFake):
        def api(self, path):
            if path == "commits/main" and self.completed:
                return {"sha": "b" * 40}
            return super().api(path)

        def s3_get(self, key):
            if self.completed:
                pytest.fail("observation-only resume attempted another S3 operation")
            return super().s3_get(key)

    fake = Advances()
    supplied = evidence(tmp_path)
    b.publish(supplied, fake)
    result = b.publish(supplied, fake)
    assert json.loads(result.read_text())["status"] == "monthly_success_consumers_verified"
    assert len([c for c in fake.commands if c[1:3] == ["workflow", "run"]]) == 1
