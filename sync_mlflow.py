"""Ingesta los records JSONL de ``mlruns_staging/`` a MLflow (backend SQLite). Corre en ``ante_nf``.

MLflow vive solo en ``ante_nf`` (pandas<3). Este script lee lo que cualquier env escribió vía
``tracking.log_run`` y lo materializa como runs de MLflow, idempotente por ``rec_id`` (no
duplica al re-sincronizar). MLflow 3.x deprecó el file-store → backend SQLite (``mlflow.db``)
con artefactos en ``mlartifacts/``.

Uso:  ante_nf/bin/python sync_mlflow.py
      ante_nf/bin/mlflow ui --backend-store-uri sqlite:///mlflow.db --default-artifact-root mlartifacts/
"""

from __future__ import annotations

import json
from pathlib import Path

import mlflow

ROOT = Path(__file__).resolve().parent
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


def _experiment(name: str) -> None:
    """set_experiment con artifact_location explícito (backend SQLite no lo infiere)."""
    if mlflow.get_experiment_by_name(name) is None:
        mlflow.create_experiment(name, artifact_location=f"{ARTIFACTS}/{name}")
    mlflow.set_experiment(name)


def main() -> None:
    mlflow.set_tracking_uri(DB_URI)
    seen = synced_ids()
    new = 0
    for jsonl in sorted(STAGING.glob("*.jsonl")):
        _experiment(jsonl.stem)
        for line in jsonl.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec["rec_id"] in seen:
                continue
            with mlflow.start_run(run_name=rec["run_name"]):
                mlflow.log_params(rec["params"])
                mlflow.log_metrics(rec["metrics"])
                mlflow.set_tags({**rec["tags"], "rec_id": rec["rec_id"]})
                for art in rec["artifacts"]:
                    p = ROOT / art if not Path(art).is_absolute() else Path(art)
                    if p.exists():
                        mlflow.log_artifact(str(p))
            seen.add(rec["rec_id"])
            new += 1
    print(
        f"sincronizados {new} runs nuevos a {DB_URI} · UI: ante_nf/bin/mlflow ui "
        f"--backend-store-uri sqlite:///mlflow.db --default-artifact-root mlartifacts/"
    )


if __name__ == "__main__":
    main()
