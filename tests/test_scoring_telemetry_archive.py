"""D7: the monthly scoring record and its one-line summary must outlive the CI runner.

The record lives in the gitignored ``mlruns_staging/`` and the summary in ``/tmp``; October's
run lost both. The cron archives them byte for byte with a receipt (run, commit, hashes,
record status) and uploads that directory as a CI artifact even when modeling fails.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CRON = ROOT / ".github/workflows/freeze_and_rebuild.yml"


def _archive(tmp_path: Path, *, record: bytes | None, summary: bytes | None) -> dict:
    staging, out = tmp_path / "staging", tmp_path / "out"
    staging.mkdir()
    if record is not None:
        (staging / "web_forecast_scoring.jsonl").write_bytes(record)
    summary_path = tmp_path / "tracking_monthly.txt"
    if summary is not None:
        summary_path.write_bytes(summary)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "vp_model.tracking",
            "archive",
            "--staging",
            str(staging),
            "--summary",
            str(summary_path),
            "--out",
            str(out),
            "--run-id",
            "123",
            "--git-sha",
            "a" * 40,
        ],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return json.loads((out / "receipt.json").read_text())


def test_archive_copies_the_record_and_summary_byte_for_byte_with_their_hashes(tmp_path):
    record = b'{"experiment":"web_forecast_scoring","telemetry":{"status":"ok"}}\n'
    summary = "ok · status=ok · n_scored=3518\n".encode()
    receipt = _archive(tmp_path, record=record, summary=summary)
    out = tmp_path / "out"
    assert (out / "web_forecast_scoring.jsonl").read_bytes() == record
    assert (out / "tracking_monthly.txt").read_bytes() == summary
    files = receipt["files"]
    assert files["web_forecast_scoring.jsonl"]["sha256"] == hashlib.sha256(record).hexdigest()
    assert files["tracking_monthly.txt"]["sha256"] == hashlib.sha256(summary).hexdigest()
    assert (receipt["run_id"], receipt["git_sha"], receipt["records"]) == ("123", "a" * 40, 1)
    assert receipt["last_status"] == "ok" and receipt["complete"] is True and receipt["missing"] == []


def test_a_failed_or_missing_record_is_declared_not_hidden(tmp_path):
    receipt = _archive(tmp_path, record=None, summary=b"failed\n")
    assert receipt["complete"] is False and receipt["missing"] == ["web_forecast_scoring.jsonl"]
    assert receipt["records"] == 0 and receipt["last_status"] is None


def test_a_failed_scoring_run_is_archived_with_its_status(tmp_path):
    receipt = _archive(tmp_path, record=b'{"telemetry":{"status":"failed"}}\n', summary=None)
    assert receipt["last_status"] == "failed" and receipt["missing"] == ["tracking_monthly.txt"]


def test_the_cron_archives_and_uploads_the_telemetry_even_when_modeling_fails():
    steps = yaml.safe_load(CRON.read_text())["jobs"]["update"]["steps"]
    ids = [s.get("id") for s in steps]
    archive = next(s for s in steps if "vp_model.tracking archive" in s.get("run", ""))
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert ids.index("model") < steps.index(archive) < steps.index(upload)
    for step in (archive, upload):
        assert "always()" in step["if"] and "steps.run.outputs.rebuilt == 'true'" in step["if"]
    assert "--summary /tmp/tracking_monthly.txt" in archive["run"]
    assert upload["with"]["path"] in archive["run"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert len(upload["uses"].split("@")[1].split()[0]) == 40  # pinned by SHA
