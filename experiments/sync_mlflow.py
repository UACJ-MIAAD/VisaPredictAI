"""Ingesta los records JSONL de ``mlruns_staging/`` a MLflow (backend SQLite). Corre en ``ante_nf``.

MLflow vive solo en ``ante_nf`` (pandas<3). Este script lee lo que cualquier env escribió vía
``tracking.log_run`` y lo materializa como runs de MLflow. MLflow 3.x deprecó el file-store →
backend SQLite (``mlflow.db``) con artefactos en ``mlartifacts/``.

A2 (plan auditoría 2026-07-12) — garantías:

- **Idempotente**: la clave de sync es el ``rec_id`` del record (tag en cada run de MLflow);
  re-correr sobre el mismo staging no duplica.
- **Concurrent-safe**: toda la ingesta corre bajo un lock exclusivo de archivo
  (``mlruns_staging/.sync.lock``): dos syncs paralelos se serializan.
- **Backward-compat**: líneas sin ``schema_version`` se tratan como **v1** (su ``rec_id``
  es hash de contenido y COLAPSA eventos idénticos en métricas pero distintos en
  pipeline_run_id/tags/ts). Esa deduplicación deja de ser silenciosa: cada corrida escribe
  ``reports/governance/mlflow_sync_reconciliation.json`` con los rec_id colapsados y el motivo.
- **v2 completo**: los records con ``schema_version>=2`` ingieren además procedencia
  (``vp.data_hash``/``vp.code_sha``/``vp.recipe_version``/``vp.seed``/``vp.env_lock_hash``),
  input dataset (panel + digest, best-effort vía ``client.log_inputs``), telemetría
  (duración/RSS/GPU/artefactos como métricas; warnings/excepción como tags) y estado
  FINISHED/FAILED según ``telemetry.status``.
- **Artefactos portables**: los experiments nuevos se crean con ``artifact_location``
  RELATIVA al repo (``mlartifacts/{name}``) — ninguna URI nueva contiene ``/Users/``.
  Correr el sync y la UI DESDE LA RAÍZ del repo. Los 13,165 runs históricos conservan su
  URI absoluta; ``experiments/backfill_mlflow_legacy.py`` los etiqueta y repara las raíces
  de experiment (ver ``docs/mlops_experimentos.md``).

AO4: cada run se crea con ``MlflowClient.create_run(start_time=rec["ts"])`` — la UI muestra
la fecha REAL del experimento, no la fecha del sync.

Decisión AO9 (documentada también en ``docs/mlops_experimentos.md``): MLflow es un ARCHIVO
HISTÓRICO sincronizado manualmente (``make mlflow-sync`` o ``experiments/sync_all.sh``),
NO un dashboard en vivo. El registro durable/canónico son los CSV/JSON commiteados en git.

Uso:  ante_nf/bin/python experiments/sync_mlflow.py
      ante_nf/bin/mlflow ui --backend-store-uri sqlite:///mlflow.db --default-artifact-root mlartifacts/
"""

from __future__ import annotations

import fcntl  # POSIX; el sync corre en macOS/Linux
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlflow

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "mlruns_staging"
DB_URI = f"sqlite:///{ROOT / 'mlflow.db'}"
# A2: raíz de artefactos RELATIVA al repo (portable). Nada de file:///Users/... en URIs nuevas.
ARTIFACTS_DIRNAME = "mlartifacts"
RECONCILIATION = ROOT / "reports" / "governance" / "mlflow_sync_reconciliation.json"
PANEL_REL = "data/processed/visa_panel_long.csv"

V1_COLLAPSE_REASON = (
    "v1 rec_id = hash de contenido {experiment, run_name, params, metrics}: líneas idénticas "
    "en contenido pero distintas en pipeline_run_id/tags/artifacts/ts comparten rec_id; solo la "
    "primera ocurrencia se ingiere a MLflow. Los records v2 (schema_version>=2) usan clave de "
    "EVENTO (incluye procedencia + ts + seq) y no colapsan."
)


def synced_ids() -> set[str]:
    """rec_id ya ingestados (tag en runs existentes) — para no duplicar."""
    out: set[str] = set()
    client = mlflow.tracking.MlflowClient()
    for exp in client.search_experiments():
        for run in client.search_runs([exp.experiment_id], max_results=50000):
            rid = run.data.tags.get("rec_id")
            if rid:
                out.add(rid)
    return out


def _experiment_id(name: str) -> str:
    """Experiment id; los nuevos se crean con artifact_location RELATIVA (portable).

    ⚠️ mlflow 3.x canonicaliza la ruta relativa a ABSOLUTA al crear el experiment;
    ``_portabilize`` la devuelve a la forma relativa tras la ingesta (solo filas nuevas).
    """
    exp = mlflow.get_experiment_by_name(name)
    if exp is None:
        return mlflow.create_experiment(name, artifact_location=f"{ARTIFACTS_DIRNAME}/{name}")
    return exp.experiment_id


def _portabilize(db_uri: str, root: Path, exp_ids: list[str], run_ids: list[str]) -> int:
    """Reescribe a rutas RELATIVAS las URIs de las filas RECIÉN creadas (A2: portable).

    mlflow canonicaliza ``artifact_location`` relativa → absoluta bajo el cwd; este paso
    la regresa a ``mlartifacts/...`` para experiments/runs creados en ESTA pasada. Las
    filas históricas NO se tocan (eso es del backfill, que solo etiqueta). Idempotente:
    una fila ya relativa no matchea los prefijos. Devuelve cuántas filas cambió.
    """
    if not db_uri.startswith("sqlite:///") or not (exp_ids or run_ids):
        return 0
    db_path = Path(db_uri.removeprefix("sqlite:///"))
    if not db_path.is_file():
        return 0
    prefixes = (f"file://{root}/", f"{root}/")
    changed = 0
    con = sqlite3.connect(db_path)
    try:
        for table, col, id_col, ids in (
            ("experiments", "artifact_location", "experiment_id", exp_ids),
            ("runs", "artifact_uri", "run_uuid", run_ids),
        ):
            if not ids:
                continue
            marks = ",".join("?" * len(ids))
            for pref in prefixes:
                # identificadores fijos del propio código (no input); los valores van parametrizados
                cur = con.execute(
                    f"UPDATE {table} SET {col} = substr({col}, ?) WHERE {col} LIKE ? AND {id_col} IN ({marks})",
                    (len(pref) + 1, pref + "%", *ids),
                )
                changed += cur.rowcount
        con.commit()
    finally:
        con.close()
    return changed


def _log_dataset_input(client: Any, rid: str, rec: dict) -> None:
    """Best-effort: registra el panel como input dataset del run (v2). Fallback = tags vp.*."""
    digest = rec.get("provenance", {}).get("data_hash", "")
    if not digest or digest == "unknown":
        return
    try:
        from mlflow.entities import Dataset, DatasetInput

        ds = Dataset(
            name="visa_panel_long",
            digest=digest.removeprefix("sha256:")[:16],
            source_type="local",
            source=json.dumps({"path": PANEL_REL}),
        )
        client.log_inputs(rid, datasets=[DatasetInput(dataset=ds, tags=[])])
    except Exception:  # noqa: BLE001 — la procedencia ya viaja en tags vp.*; no abortar el sync
        pass


def _ingest_v2_extras(client: Any, rid: str, rec: dict, ts_ms: int) -> tuple[str, int]:
    """Tags de procedencia + telemetría del record v2. Devuelve (status, end_time_ms)."""
    prov = rec.get("provenance", {})
    for key in ("data_hash", "code_sha", "recipe_version", "seed", "env_lock_hash", "seq"):
        if key in prov:
            client.set_tag(rid, f"vp.{key}", str(prov[key]))
    client.set_tag(rid, "content_hash", rec.get("content_hash", ""))
    client.set_tag(rid, "schema_version", str(rec.get("schema_version", 1)))
    _log_dataset_input(client, rid, rec)
    status, end_ms = "FINISHED", ts_ms
    tel = rec.get("telemetry")
    if tel:
        if tel.get("status") == "failed":
            status = "FAILED"
        client.set_tag(rid, "telemetry_status", tel.get("status", "ok"))
        if tel.get("warnings"):
            client.set_tag(rid, "telemetry_warnings", "; ".join(tel["warnings"])[:5000])
        if tel.get("exception"):
            exc = tel["exception"]
            client.set_tag(rid, "telemetry_exception", f"{exc.get('type', '?')}: {exc.get('message', '')}"[:5000])
        for key in ("duration_s", "rss_peak_mb", "gpu_mem_mb", "artifact_bytes"):
            if tel.get(key) is not None:
                client.log_metric(rid, f"telemetry_{key}", float(tel[key]), timestamp=ts_ms)
        if tel.get("duration_s") is not None:
            end_ms = ts_ms + int(float(tel["duration_s"]) * 1000)
    return status, end_ms


def _iter_staging_records() -> tuple[list[tuple[str, dict]], list[str]]:
    """[(experiment, record), ...] de todo el staging + lista de líneas corruptas."""
    records: list[tuple[str, dict]] = []
    corrupt: list[str] = []
    for jsonl in sorted(STAGING.glob("*.jsonl")):
        for n, line in enumerate(jsonl.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append((jsonl.stem, json.loads(line)))
            except json.JSONDecodeError:
                corrupt.append(f"{jsonl.name}:{n}")
    return records, corrupt


def _write_reconciliation(stats: dict) -> None:
    RECONCILIATION.parent.mkdir(parents=True, exist_ok=True)
    RECONCILIATION.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")


def main() -> None:
    mlflow.set_tracking_uri(DB_URI)
    client = mlflow.tracking.MlflowClient()

    STAGING.mkdir(exist_ok=True)
    lock_path = STAGING / ".sync.lock"
    with lock_path.open("a+") as lock_f:
        # serializa syncs concurrentes (la idempotencia hace no-op al segundo)
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            _sync(client)
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _sync(client: Any) -> None:
    seen = synced_ids()
    records, corrupt = _iter_staging_records()

    # Reconciliación v1: ocurrencias por rec_id (el hash de contenido colapsa eventos).
    v1_occurrences: dict[str, int] = {}
    for _, rec in records:
        if rec.get("schema_version", 1) < 2:
            rid = rec.get("rec_id", "")
            v1_occurrences[rid] = v1_occurrences.get(rid, 0) + 1
    v1_collapsed = {k: v for k, v in v1_occurrences.items() if v > 1}
    v1_extra_lines = sum(v - 1 for v in v1_collapsed.values())

    new = already = 0
    exp_ids: dict[str, str] = {}
    new_exp_ids: list[str] = []
    new_run_ids: list[str] = []
    for exp_name, rec in records:
        key = rec.get("rec_id") or ""
        if not key or key in seen:
            already += bool(key)
            continue
        if exp_name not in exp_ids:
            existed = mlflow.get_experiment_by_name(exp_name) is not None
            exp_ids[exp_name] = _experiment_id(exp_name)
            if not existed:
                new_exp_ids.append(exp_ids[exp_name])
        # AO4: preserve the record's REAL timestamp (ms) instead of the sync time.
        ts_ms = int(float(rec.get("ts", time.time())) * 1000)
        run = client.create_run(
            exp_ids[exp_name],
            start_time=ts_ms,
            tags={**rec["tags"], "rec_id": key},
            run_name=rec["run_name"],
        )
        rid = run.info.run_id
        for k, v in rec["params"].items():
            client.log_param(rid, k, v)
        for k, v in rec["metrics"].items():
            client.log_metric(rid, k, v, timestamp=ts_ms)
        status, end_ms = "FINISHED", ts_ms
        if rec.get("schema_version", 1) >= 2:
            status, end_ms = _ingest_v2_extras(client, rid, rec, ts_ms)
        for art in rec["artifacts"]:
            p = ROOT / art if not Path(art).is_absolute() else Path(art)
            if p.exists():
                client.log_artifact(rid, str(p))
        client.set_terminated(rid, status=status, end_time=end_ms)
        seen.add(key)
        new_run_ids.append(rid)
        new += 1

    # A2: mlflow canonicaliza a absoluta; regresar las filas NUEVAS a rutas relativas.
    portabilized = _portabilize(DB_URI, ROOT, new_exp_ids, new_run_ids)

    stats = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "staging_lines": len(records) + len(corrupt),
        "v1_lines": sum(1 for _, r in records if r.get("schema_version", 1) < 2),
        "v2_lines": sum(1 for _, r in records if r.get("schema_version", 1) >= 2),
        "corrupt_lines": corrupt,
        "unique_keys": len({r.get("rec_id") for _, r in records if r.get("rec_id")}),
        "ingested_new": new,
        "already_synced": already,
        "v1_collapsed": dict(sorted(v1_collapsed.items())),
        "v1_collapsed_extra_lines": v1_extra_lines,
        "portabilized_uris": portabilized,
        "reason": V1_COLLAPSE_REASON,
    }
    _write_reconciliation(stats)
    recon_path = RECONCILIATION.relative_to(ROOT) if RECONCILIATION.is_relative_to(ROOT) else RECONCILIATION
    print(
        f"sincronizados {new} runs nuevos ({already} ya presentes) a {DB_URI} · "
        f"dedup v1 EXPLÍCITA: {v1_extra_lines} líneas colapsadas en {len(v1_collapsed)} rec_id "
        f"(detalle: {recon_path}) · UI (desde la raíz del repo): "
        f"ante_nf/bin/mlflow ui --backend-store-uri sqlite:///mlflow.db "
        f"--default-artifact-root {ARTIFACTS_DIRNAME}/"
    )
    if corrupt:
        print(f"ADVERTENCIA: {len(corrupt)} línea(s) corrupta(s) en staging: {corrupt}")


if __name__ == "__main__":
    main()
