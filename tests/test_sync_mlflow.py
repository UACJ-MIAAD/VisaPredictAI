"""A2 — sync_mlflow idempotente/explícito + backfill de runs legados.

``mlflow`` no existe en el venv ``ante`` (exige pandas<3), así que el sync se prueba con un
mock in-memory registrado en ``sys.modules`` ANTES de cargar el módulo. Cubre: ingesta v1+v2,
idempotencia al re-correr, deduplicación v1 EXPLÍCITA (artefacto de reconciliación con los
rec_id colapsados), artifact_location relativa (sin ``/Users/``), telemetría/status FAILED y
procedencia como tags. El backfill se prueba contra un sqlite temporal con el subset del
esquema de MLflow que el script toca (experiments/runs/metrics/tags).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vp_data import tracking
from vp_model.tracking import track_run

ROOT = Path(__file__).resolve().parent.parent


# --- fake mlflow ---------------------------------------------------------------


class FakeStore:
    def __init__(self):
        self.experiments: dict[str, dict] = {}  # name -> {experiment_id, artifact_location}
        self.runs: dict[str, dict] = {}  # run_id -> {...}
        self._next = 1

    def create_experiment(self, name: str, artifact_location: str) -> str:
        exp_id = str(self._next)
        self._next += 1
        self.experiments[name] = {"experiment_id": exp_id, "artifact_location": artifact_location}
        return exp_id


class FakeClient:
    def __init__(self, store: FakeStore):
        self._s = store

    def search_experiments(self):
        return [SimpleNamespace(experiment_id=e["experiment_id"]) for e in self._s.experiments.values()]

    def search_runs(self, experiment_ids, max_results=50000):
        out = []
        for rid, run in self._s.runs.items():
            if run["experiment_id"] in experiment_ids:
                out.append(SimpleNamespace(info=SimpleNamespace(run_id=rid), data=SimpleNamespace(tags=run["tags"])))
        return out

    def create_run(self, experiment_id, start_time, tags, run_name):
        rid = f"run{len(self._s.runs):04d}"
        self._s.runs[rid] = {
            "experiment_id": experiment_id,
            "start_time": start_time,
            "tags": dict(tags),
            "run_name": run_name,
            "params": {},
            "metrics": {},
            "artifacts": [],
            "inputs": [],
            "status": None,
            "end_time": None,
        }
        return SimpleNamespace(info=SimpleNamespace(run_id=rid))

    def log_param(self, rid, k, v):
        self._s.runs[rid]["params"][k] = v

    def log_metric(self, rid, k, v, timestamp=None):
        self._s.runs[rid]["metrics"][k] = v

    def log_artifact(self, rid, path):
        self._s.runs[rid]["artifacts"].append(path)

    def set_tag(self, rid, k, v):
        self._s.runs[rid]["tags"][k] = v

    def log_inputs(self, rid, datasets):
        self._s.runs[rid]["inputs"].extend(datasets)

    def set_terminated(self, rid, status="FINISHED", end_time=None):
        self._s.runs[rid]["status"] = status
        self._s.runs[rid]["end_time"] = end_time


def _fake_mlflow(store: FakeStore) -> types.ModuleType:
    m: Any = types.ModuleType("mlflow")  # Any: los módulos fake reciben atributos ad-hoc
    m.set_tracking_uri = lambda uri: None
    m.get_experiment_by_name = lambda name: (
        SimpleNamespace(**store.experiments[name]) if name in store.experiments else None
    )
    m.create_experiment = store.create_experiment
    tracking_mod: Any = types.ModuleType("mlflow.tracking")
    tracking_mod.MlflowClient = lambda: FakeClient(store)
    m.tracking = tracking_mod
    entities_mod: Any = types.ModuleType("mlflow.entities")

    class Dataset:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class DatasetInput:
        def __init__(self, dataset, tags=()):
            self.dataset = dataset
            self.tags = list(tags)

    entities_mod.Dataset = Dataset
    entities_mod.DatasetInput = DatasetInput
    m.entities = entities_mod
    return m


@pytest.fixture
def sync_env(monkeypatch, tmp_path):
    """Carga experiments/sync_mlflow.py con mlflow mockeado y staging temporal."""
    store = FakeStore()
    fake = _fake_mlflow(store)
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    monkeypatch.setitem(sys.modules, "mlflow.tracking", fake.tracking)
    monkeypatch.setitem(sys.modules, "mlflow.entities", fake.entities)
    spec = importlib.util.spec_from_file_location("sync_mlflow_test", ROOT / "experiments" / "sync_mlflow.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(mod, "STAGING", staging)
    monkeypatch.setattr(mod, "RECONCILIATION", tmp_path / "recon.json")
    # jamás apuntar a la mlflow.db real: db temporal inexistente (portabilize la salta)
    monkeypatch.setattr(mod, "DB_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    return SimpleNamespace(mod=mod, store=store, staging=staging, recon=tmp_path / "recon.json", tmp=tmp_path)


def _v1_line(run_name: str, rec_id: str, ts: float, artifacts=()) -> str:
    return json.dumps(
        {
            "experiment": "expA",
            "run_name": run_name,
            "params": {"model": "ets"},
            "metrics": {"mase": 0.114},
            "tags": {"git_sha": "abc", "git_dirty": "False"},
            "artifacts": list(artifacts),
            "ts": ts,
            "rec_id": rec_id,
        }
    )


def _seed_staging(env, monkeypatch) -> None:
    # v1: 3 líneas, 2 colapsadas en el mismo rec_id (eventos distintos, mismo contenido)
    art = env.tmp / "artifact.csv"
    art.write_text("a,b\n1,2\n")
    (env.staging / "expA.jsonl").write_text(
        _v1_line("r1", "aaaa000000000001", 1.0, artifacts=[str(art)])
        + "\n"
        + _v1_line("r1", "aaaa000000000001", 2.0)
        + "\n"
        + _v1_line("r2", "bbbb000000000002", 3.0)
        + "\n"
    )
    # v2: records REALES de tracking (uno ok con telemetría, uno failed)
    monkeypatch.setattr(tracking, "STAGING", env.staging)
    with track_run("expB", "deep-ok", params={"model": "bitcn"}, seed=1) as run:
        run.log_metric("hold_mase", 0.109)
    with pytest.raises(RuntimeError):
        with track_run("expB", "deep-fail", params={"model": "deepar"}):
            raise RuntimeError("diverged")


def test_sync_ingests_v1_and_v2_with_explicit_dedup(sync_env, monkeypatch, capsys):
    _seed_staging(sync_env, monkeypatch)
    sync_env.mod.main()

    runs = sync_env.store.runs
    assert len(runs) == 4  # 2 v1 únicos + 2 v2 (el duplicado v1 NO se ingiere)

    # reconciliación EXPLÍCITA: el colapso v1 queda listado, no silenciado
    recon = json.loads(sync_env.recon.read_text())
    assert recon["staging_lines"] == 5
    assert recon["v1_lines"] == 3 and recon["v2_lines"] == 2
    assert recon["v1_collapsed"] == {"aaaa000000000001": 2}
    assert recon["v1_collapsed_extra_lines"] == 1
    assert recon["ingested_new"] == 4
    assert "rec_id" in recon["reason"]
    out = capsys.readouterr().out
    assert "dedup v1 EXPLÍCITA: 1 líneas colapsadas" in out

    # artifact_location portable: relativa, sin /Users/
    for exp in sync_env.store.experiments.values():
        assert exp["artifact_location"].startswith("mlartifacts/")
        assert "/Users/" not in exp["artifact_location"]

    # v1: artefacto físico logueado
    v1_runs = [r for r in runs.values() if r["tags"].get("rec_id") == "aaaa000000000001"]
    assert len(v1_runs) == 1 and len(v1_runs[0]["artifacts"]) == 1

    # v2: procedencia + dataset input + telemetría + status
    by_name = {r["run_name"]: r for r in runs.values()}
    ok = by_name["deep-ok"]
    assert ok["status"] == "FINISHED"
    assert ok["tags"]["schema_version"] == "2"
    assert ok["tags"]["vp.data_hash"].startswith("sha256:")
    assert ok["tags"]["content_hash"]
    assert ok["tags"]["pipeline_run_id"]
    assert len(ok["inputs"]) == 1 and ok["inputs"][0].dataset.name == "visa_panel_long"
    assert "telemetry_duration_s" in ok["metrics"]
    fail = by_name["deep-fail"]
    assert fail["status"] == "FAILED"
    assert fail["tags"]["telemetry_status"] == "failed"
    assert fail["tags"]["telemetry_exception"].startswith("RuntimeError: diverged")


def test_sync_is_idempotent_on_rerun(sync_env, monkeypatch):
    _seed_staging(sync_env, monkeypatch)
    sync_env.mod.main()
    assert len(sync_env.store.runs) == 4
    sync_env.mod.main()  # re-correr sobre el MISMO staging
    assert len(sync_env.store.runs) == 4  # no duplica
    recon = json.loads(sync_env.recon.read_text())
    assert recon["ingested_new"] == 0
    assert recon["already_synced"] == 5  # 4 únicos + el duplicado v1 colapsado


def test_sync_reports_corrupt_lines_without_aborting(sync_env, monkeypatch):
    _seed_staging(sync_env, monkeypatch)
    with (sync_env.staging / "expA.jsonl").open("a") as f:
        f.write("{corrupta\n")
    sync_env.mod.main()
    assert len(sync_env.store.runs) == 4  # lo sano se ingiere
    recon = json.loads(sync_env.recon.read_text())
    assert recon["corrupt_lines"] == ["expA.jsonl:4"]


# --- _portabilize: URIs nuevas relativas (mlflow canonicaliza a absoluta) --------


def test_portabilize_rewrites_only_new_rows(sync_env, tmp_path):
    root = tmp_path / "repo"
    db = tmp_path / "port.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE experiments (experiment_id TEXT, artifact_location TEXT)")
    con.execute("CREATE TABLE runs (run_uuid TEXT, artifact_uri TEXT)")
    con.execute("INSERT INTO experiments VALUES ('10', ?)", (f"{root}/mlartifacts/expN",))
    con.execute("INSERT INTO experiments VALUES ('11', ?)", (f"{root}/mlartifacts/legacy",))
    con.execute("INSERT INTO runs VALUES ('r_new', ?)", (f"file://{root}/mlartifacts/expN/r_new/artifacts",))
    con.execute("INSERT INTO runs VALUES ('r_old', ?)", (f"file://{root}/mlartifacts/legacy/r_old/artifacts",))
    con.commit()
    con.close()

    changed = sync_env.mod._portabilize(f"sqlite:///{db}", root, ["10"], ["r_new"])
    assert changed == 2
    con = sqlite3.connect(db)
    exps = dict(con.execute("SELECT experiment_id, artifact_location FROM experiments").fetchall())
    runs = dict(con.execute("SELECT run_uuid, artifact_uri FROM runs").fetchall())
    con.close()
    assert exps["10"] == "mlartifacts/expN"  # fila nueva: relativa, sin raíz absoluta
    assert exps["11"].startswith(str(root))  # fila legada: intacta (eso es del backfill)
    assert runs["r_new"] == "mlartifacts/expN/r_new/artifacts"
    assert runs["r_old"].startswith("file://")
    # idempotente: segunda pasada no cambia nada
    assert sync_env.mod._portabilize(f"sqlite:///{db}", root, ["10"], ["r_new"]) == 0


def test_portabilize_skips_missing_db_and_empty_ids(sync_env, tmp_path):
    assert sync_env.mod._portabilize(f"sqlite:///{tmp_path / 'nope.db'}", tmp_path, ["1"], []) == 0
    assert sync_env.mod._portabilize("postgresql://x", tmp_path, ["1"], ["r"]) == 0
    assert sync_env.mod._portabilize(f"sqlite:///{tmp_path / 'nope.db'}", tmp_path, [], []) == 0


# --- backfill de legados --------------------------------------------------------


MLFLOW_DDL = """
CREATE TABLE experiments (experiment_id INTEGER, name TEXT, artifact_location TEXT);
CREATE TABLE runs (run_uuid TEXT, artifact_uri TEXT);
CREATE TABLE metrics (key TEXT, value REAL, timestamp INTEGER, run_uuid TEXT, step INTEGER, is_nan INTEGER);
CREATE TABLE tags (key TEXT, value TEXT, run_uuid TEXT, PRIMARY KEY (key, run_uuid));
"""


def _load_backfill():
    spec = importlib.util.spec_from_file_location(
        "backfill_mlflow_legacy_test", ROOT / "experiments" / "backfill_mlflow_legacy.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def legacy_db(tmp_path):
    db = tmp_path / "mlflow.db"
    con = sqlite3.connect(db)
    con.executescript(MLFLOW_DDL)
    abs_prefix = "file:///Users/nadie/repo/mlartifacts"
    con.execute("INSERT INTO experiments VALUES (1, 'expA', ?)", (f"{abs_prefix}/expA",))
    con.execute("INSERT INTO runs VALUES ('r_complete', ?)", (f"{abs_prefix}/expA/r_complete/artifacts",))
    con.execute("INSERT INTO runs VALUES ('r_metrics', ?)", (f"{abs_prefix}/expA/r_metrics/artifacts",))
    con.execute("INSERT INTO runs VALUES ('r_invalid', ?)", (f"{abs_prefix}/expA/r_invalid/artifacts",))
    con.execute("INSERT INTO runs VALUES ('r_v2', 'mlartifacts/expA/r_v2/artifacts')")
    for rid in ("r_complete", "r_metrics", "r_v2"):
        con.execute("INSERT INTO metrics VALUES ('mase', 0.1, 0, ?, 0, 0)", (rid,))
    con.execute("INSERT INTO tags VALUES ('schema_version', '2', 'r_v2')")  # v2: no es legado
    con.commit()
    con.close()
    # artefactos físicos SOLO para r_complete
    adir = tmp_path / "mlartifacts" / "expA" / "r_complete" / "artifacts"
    adir.mkdir(parents=True)
    (adir / "model.pkl").write_bytes(b"x")
    return db


def _tags(db: Path) -> dict[str, str]:
    con = sqlite3.connect(db)
    try:
        return dict(con.execute("SELECT run_uuid, value FROM tags WHERE key='legacy_status'").fetchall())
    finally:
        con.close()


def test_backfill_dry_run_changes_nothing(legacy_db, tmp_path, capsys):
    mod = _load_backfill()
    assert mod.main(["--db", str(legacy_db), "--root", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["by_status"] == {"invalid": 1, "legacy_complete": 1, "legacy_metrics_only": 1}
    assert report["experiment_roots_to_fix"] == 1
    assert _tags(legacy_db) == {}  # dry-run NO escribe


def test_backfill_apply_tags_and_portable_roots(legacy_db, tmp_path, capsys):
    mod = _load_backfill()
    assert mod.main(["--db", str(legacy_db), "--root", str(tmp_path), "--apply"]) == 0
    tags = _tags(legacy_db)
    assert tags == {"r_complete": "legacy_complete", "r_metrics": "legacy_metrics_only", "r_invalid": "invalid"}
    assert "r_v2" not in tags  # los runs v2 no se tocan
    con = sqlite3.connect(legacy_db)
    loc = con.execute("SELECT artifact_location FROM experiments WHERE name='expA'").fetchone()[0]
    uris = [r[0] for r in con.execute("SELECT artifact_uri FROM runs").fetchall()]
    con.close()
    assert loc == "mlartifacts/expA"  # raíz reparada, relativa
    assert any("/Users/" in u for u in uris)  # los artifact_uri históricos NO se reescriben
    # idempotente: segunda pasada, mismo estado
    capsys.readouterr()
    assert mod.main(["--db", str(legacy_db), "--root", str(tmp_path), "--apply"]) == 0
    assert _tags(legacy_db) == tags


def test_backfill_missing_db_fails_closed(tmp_path, capsys):
    mod = _load_backfill()
    assert mod.main(["--db", str(tmp_path / "nope.db")]) == 1
