"""A2/A6 — contrato del record v2 de ``vp_data.tracking`` + telemetría de ``vp_model.tracking``.

Cubre: clave de EVENTO (dos eventos distintos no colisionan), ``content_hash`` compatible
con el ``rec_id`` v1, procedencia con fallbacks ``unknown`` (jamás fabricada),
``pipeline_run_id`` en el 100 % de los records, normalización de telemetría y el
context-manager ``track_run`` (ok / failed / artefactos), que loguea INCLUSO si el bloque
falla y re-lanza la excepción.

Sin dependencias del extra ``model``: corre en el job base de CI.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vp_data import tracking
from vp_model.tracking import TrackedRun, track_run

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def staging(monkeypatch, tmp_path):
    monkeypatch.setattr(tracking, "STAGING", tmp_path / "staging")
    return tmp_path / "staging"


def _read_records(staging_dir: Path, experiment: str) -> list[dict]:
    return [json.loads(x) for x in (staging_dir / f"{experiment}.jsonl").read_text().splitlines()]


# --- contrato v2 --------------------------------------------------------------


def test_v2_record_contract(staging):
    rec = tracking.log_run(
        "expA",
        "run1",
        params={"model": "ets", "table": "FAD"},
        metrics={"mase": 0.114},
        tags={"layer": "pool"},
        artifacts=["models/x.pkl"],
    )
    assert rec["schema_version"] == 2
    assert len(rec["rec_id"]) == 16 and len(rec["content_hash"]) == 16
    assert rec["tags"]["layer"] == "pool" and rec["tags"]["git_sha"]
    assert rec["tags"]["pipeline_run_id"]  # A6: 100 % de records con pipeline_run_id
    prov = rec["provenance"]
    assert set(prov) == {"pipeline_run_id", "data_hash", "code_sha", "recipe_version", "seed", "env_lock_hash", "seq"}
    assert prov["pipeline_run_id"] == rec["tags"]["pipeline_run_id"]
    # el repo real tiene panel + locks + git -> procedencia resuelta, no unknown
    assert prov["data_hash"].startswith("sha256:")
    assert prov["env_lock_hash"].startswith("sha256:")
    assert prov["code_sha"] not in ("", "unknown") and len(prov["code_sha"]) == 40
    # lo persistido == lo devuelto
    line = _read_records(staging, "expA")[0]
    assert line == rec


def test_content_hash_matches_v1_recid_semantics(staging):
    rec = tracking.log_run("expA", "run1", {"model": "ets"}, {"mase": 0.114})
    payload = {"experiment": "expA", "run_name": "run1", "params": {"model": "ets"}, "metrics": {"mase": 0.114}}
    v1_recid = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    assert rec["content_hash"] == v1_recid


def test_distinct_events_do_not_collide(staging, monkeypatch):
    """El bug diagnosticado: 1,777 eventos distintos colapsaban en el rec_id v1."""
    a = tracking.log_run("expA", "run1", {"model": "ets"}, {"mase": 0.114})
    b = tracking.log_run("expA", "run1", {"model": "ets"}, {"mase": 0.114})
    assert a["content_hash"] == b["content_hash"]  # mismo contenido...
    assert a["rec_id"] != b["rec_id"]  # ...pero eventos DISTINTOS

    monkeypatch.setenv("VP_PIPELINE_RUN_ID", "cron-12345")
    c = tracking.log_run("expA", "run1", {"model": "ets"}, {"mase": 0.114}, ts=a["ts"])
    assert c["provenance"]["pipeline_run_id"] == "cron-12345"
    assert c["rec_id"] not in {a["rec_id"], b["rec_id"]}


def test_nan_and_none_metrics_filtered(staging):
    rec = tracking.log_run("expA", "r", {}, {"ok": 1.0, "nan": float("nan"), "inf": float("inf"), "none": None})
    assert rec["metrics"] == {"ok": 1.0}


def test_provenance_fallbacks_are_unknown_never_fabricated(staging, monkeypatch, tmp_path):
    monkeypatch.setattr(tracking, "ROOT", tmp_path)  # sin panel ni git -> unknown
    monkeypatch.setattr(tracking, "LOCKS_DIR", tmp_path / "locks")  # sin locks -> unknown
    tracking.env_lock_hash.cache_clear()
    tracking.code_sha.cache_clear()
    try:
        rec = tracking.log_run("expA", "r", {}, {"m": 1.0})
    finally:
        # limpiar caches envenenadas por el ROOT falso (lru_cache sobrevive al monkeypatch)
        tracking.env_lock_hash.cache_clear()
        tracking.code_sha.cache_clear()
    prov = rec["provenance"]
    assert prov["data_hash"] == "unknown"
    assert prov["env_lock_hash"] == "unknown"
    assert prov["code_sha"] == "unknown"
    assert prov["recipe_version"] == "unknown"  # ni kwarg ni params
    assert prov["seed"] == "unknown"


def test_explicit_provenance_kwargs(staging):
    rec = tracking.log_run("expA", "r", {}, {"m": 1.0}, data_hash="sha256:abc", recipe_version="recipe-v3", seed=42)
    prov = rec["provenance"]
    assert prov["data_hash"] == "sha256:abc"
    assert prov["recipe_version"] == "recipe-v3"
    assert prov["seed"] == 42
    # y la clave de evento cambia con la procedencia
    other = tracking.log_run("expA", "r", {}, {"m": 1.0}, data_hash="sha256:def", recipe_version="recipe-v3", seed=42)
    assert other["rec_id"] != rec["rec_id"]


def test_seed_and_recipe_fallback_from_params(staging):
    rec = tracking.log_run("expA", "r", {"seed": 7, "recipe_version": "rv1"}, {"m": 1.0})
    assert rec["provenance"]["seed"] == 7
    assert rec["provenance"]["recipe_version"] == "rv1"


def test_backward_compat_v1_keys_still_present(staging):
    """Los consumidores v1 del staging (sync viejo, tests) leen estas claves."""
    rec = tracking.log_run("expA", "r", {"p": 1}, {"m": 1.0}, artifacts=["a.pkl"])
    for key in ("experiment", "run_name", "params", "metrics", "tags", "artifacts", "ts", "rec_id"):
        assert key in rec
    assert rec["tags"]["git_sha"] and rec["tags"]["git_dirty"] in ("True", "False")


def test_telemetry_normalization(staging):
    rec = tracking.log_run(
        "expA",
        "r",
        {},
        {"m": 1.0},
        telemetry={
            "status": "FAILED",
            "duration_s": float("inf"),  # no finito -> None
            "rss_peak_mb": 123.4,
            "warnings": ("w1", 2),
            "exception": {"type": "ValueError", "message": "x" * 900},
        },
    )
    tel = rec["telemetry"]
    assert tel["status"] == "failed"
    assert tel["duration_s"] is None and tel["rss_peak_mb"] == 123.4
    assert tel["gpu_mem_mb"] is None and tel["artifact_bytes"] is None
    assert tel["warnings"] == ["w1", "2"]
    assert tel["exception"]["type"] == "ValueError" and len(tel["exception"]["message"]) == 500


def test_no_telemetry_key_when_absent(staging):
    rec = tracking.log_run("expA", "r", {}, {"m": 1.0})
    assert "telemetry" not in rec


def test_pipeline_run_id_resolution(monkeypatch):
    monkeypatch.delenv("VP_PIPELINE_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert tracking.pipeline_run_id() == "local"
    monkeypatch.setenv("GITHUB_RUN_ID", "gh-1")
    assert tracking.pipeline_run_id() == "gh-1"
    monkeypatch.setenv("VP_PIPELINE_RUN_ID", "vp-1")  # VP_* gana
    assert tracking.pipeline_run_id() == "vp-1"


def test_git_state_public_and_alias_kept():
    sha, dirty = tracking.git_state()
    assert isinstance(sha, str) and sha
    assert isinstance(dirty, bool)
    assert tracking._git is tracking.git_state  # compat AP5


# --- A6: context-manager de telemetría (vp_model.tracking) --------------------


def test_track_run_ok_records_telemetry(staging, tmp_path):
    art = tmp_path / "model.bin"
    art.write_bytes(b"x" * 2048)
    with track_run("campA", "run-ok", params={"model": "ets"}, seed=11, recipe_version="rv2") as run:
        run.log_metric("hold_mase", 0.114)
        run.log_metrics({"sel_mase": 0.110})
        run.warn("convergence: restarted")
        run.add_artifact(art)
    recs = _read_records(staging, "campA")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["metrics"] == {"hold_mase": 0.114, "sel_mase": 0.110}
    assert rec["provenance"]["seed"] == 11 and rec["provenance"]["recipe_version"] == "rv2"
    assert rec["tags"]["pipeline_run_id"]
    tel = rec["telemetry"]
    assert tel["status"] == "ok" and tel["exception"] is None
    assert tel["duration_s"] >= 0
    assert tel["rss_peak_mb"] is None or tel["rss_peak_mb"] > 0
    assert tel["artifact_bytes"] == 2048
    assert tel["warnings"] == ["convergence: restarted"]


def test_track_run_failure_logs_typed_exception_and_reraises(staging):
    with pytest.raises(ValueError, match="boom"):
        with track_run("campA", "run-fail", params={"model": "lstm"}) as run:
            run.log_metric("partial", 1.0)
            raise ValueError("boom")
    rec = _read_records(staging, "campA")[0]
    tel = rec["telemetry"]
    assert tel["status"] == "failed"
    assert tel["exception"] == {"type": "ValueError", "message": "boom"}
    assert rec["metrics"] == {"partial": 1.0}  # lo logueado antes del fallo se conserva


def test_tracked_run_artifact_bytes_missing_file_is_none():
    run = TrackedRun()
    run.add_artifact("no/such/file.bin")
    assert run.artifact_bytes() is None
