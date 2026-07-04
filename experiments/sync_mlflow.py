"""Ingesta los records JSONL de ``mlruns_staging/`` a MLflow (backend SQLite). Corre en ``ante_nf``.

MLflow vive solo en ``ante_nf`` (pandas<3). Este script lee lo que cualquier env escribió vía
``tracking.log_run`` y lo materializa como runs de MLflow, idempotente por ``rec_id`` (no
duplica al re-sincronizar). MLflow 3.x deprecó el file-store → backend SQLite (``mlflow.db``)
con artefactos en ``mlartifacts/``.

AO4: cada run se crea con ``MlflowClient.create_run(start_time=rec["ts"])`` — la UI muestra
la fecha REAL del experimento, no la fecha del sync (antes ``mlflow.start_run()`` estampaba
el momento de la ingesta y todo el archivo histórico parecía corrido el mismo día).

Decisión AO9 (documentada también en ``docs/mlops_experimentos.md``): MLflow es un ARCHIVO
HISTÓRICO sincronizado manualmente (``make mlflow-sync`` o ``experiments/sync_all.sh``),
NO un dashboard en vivo. El registro durable/canónico son los CSV/JSON commiteados en git.

Uso:  ante_nf/bin/python experiments/sync_mlflow.py
      ante_nf/bin/mlflow ui --backend-store-uri sqlite:///mlflow.db --default-artifact-root mlartifacts/
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import mlflow

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "mlruns_staging"
DB_URI = f"sqlite:///{ROOT / 'mlflow.db'}"
ARTIFACTS = (ROOT / "mlartifacts").as_uri()


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
    """Experiment id, creating it with an explicit artifact_location (SQLite backend)."""
    exp = mlflow.get_experiment_by_name(name)
    if exp is None:
        return mlflow.create_experiment(name, artifact_location=f"{ARTIFACTS}/{name}")
    return exp.experiment_id


def main() -> None:
    mlflow.set_tracking_uri(DB_URI)
    client = mlflow.tracking.MlflowClient()
    seen = synced_ids()
    new = 0
    for jsonl in sorted(STAGING.glob("*.jsonl")):
        exp_id = _experiment_id(jsonl.stem)
        for line in jsonl.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec["rec_id"] in seen:
                continue
            # AO4: preserve the record's REAL timestamp (ms) instead of the sync time.
            ts_ms = int(float(rec.get("ts", time.time())) * 1000)
            run = client.create_run(
                exp_id,
                start_time=ts_ms,
                tags={**rec["tags"], "rec_id": rec["rec_id"]},
                run_name=rec["run_name"],
            )
            rid = run.info.run_id
            for k, v in rec["params"].items():
                client.log_param(rid, k, v)
            for k, v in rec["metrics"].items():
                client.log_metric(rid, k, v, timestamp=ts_ms)
            for art in rec["artifacts"]:
                p = ROOT / art if not Path(art).is_absolute() else Path(art)
                if p.exists():
                    client.log_artifact(rid, str(p))
            client.set_terminated(rid, end_time=ts_ms)
            seen.add(rec["rec_id"])
            new += 1
    print(
        f"sincronizados {new} runs nuevos a {DB_URI} · UI: ante_nf/bin/mlflow ui "
        f"--backend-store-uri sqlite:///mlflow.db --default-artifact-root mlartifacts/"
    )


if __name__ == "__main__":
    main()
