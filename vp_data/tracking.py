"""Tracking de experimentos ENV-AGNÓSTICO (puente entre los dos venv del proyecto).

MLflow exige ``pandas<3`` y el env principal ``ante`` usa pandas 3 → no se puede importar
mlflow ahí. Este módulo es **stdlib pura** (sin mlflow, sin vp_model): corre idéntico en
``ante`` (pandas 3, pool local) y ``ante_nf`` (pandas 2, deep global). Cada experimento
escribe records JSONL en ``mlruns_staging/{experiment}.jsonl``; ``experiments/sync_mlflow.py`` (en
``ante_nf``, con mlflow) los vuelca a ``mlruns/`` para la UI y la comparación.

Cada record es idempotente vía ``rec_id`` (hash de contenido) → re-sincronizar no duplica.

Uso:
    from vp_data import tracking
    tracking.log_run("pool_local", "ets_mexico_F1_FAD",
                     params={"model": "ets", "country": "mexico", "table": "FAD"},
                     metrics={"sel_mase": 0.117, "sel_smape": 0.30},
                     artifacts=["models/FAD/ets_mexico_F1.pkl"])
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # raíz del repo (el paquete vive un nivel abajo)
STAGING = ROOT / "mlruns_staging"


def _git() -> tuple[str, bool]:
    """(sha corto, dirty) para procedencia; tolerante si no hay git."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=False
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT, check=False
            ).stdout.strip()
        )
        return sha or "unknown", dirty
    except Exception:  # noqa: BLE001 — el tracking nunca debe abortar la corrida
        return "unknown", True


def log_run(
    experiment: str,
    run_name: str,
    params: dict,
    metrics: dict,
    tags: dict | None = None,
    artifacts: list[str] | None = None,
    ts: float | None = None,
) -> dict:
    """Anexa 1 record de corrida al staging JSONL del experimento. Devuelve el record.

    ``metrics`` con valores no finitos (NaN/inf) se omiten (mlflow los rechaza). El ``rec_id``
    hace idempotente la sincronización.
    """
    STAGING.mkdir(exist_ok=True)
    sha, dirty = _git()
    clean_metrics = {k: float(v) for k, v in metrics.items() if v is not None and math.isfinite(float(v))}
    stamp = ts if ts is not None else time.time()
    payload = {"experiment": experiment, "run_name": run_name, "params": params, "metrics": clean_metrics}
    rec_id = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    rec = {
        **payload,
        "tags": {**(tags or {}), "git_sha": sha, "git_dirty": str(dirty)},
        "artifacts": [a for a in (artifacts or []) if a],
        "ts": stamp,
        "rec_id": rec_id,
    }
    with (STAGING / f"{experiment}.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _selfcheck() -> None:
    import tempfile

    global STAGING
    with tempfile.TemporaryDirectory() as d:
        STAGING = Path(d)
        r = log_run("t", "r1", {"model": "ets"}, {"mase": 0.12, "bad": float("nan")})
        assert r["metrics"] == {"mase": 0.12}  # NaN filtrado
        assert len(r["rec_id"]) == 16
        line = json.loads((STAGING / "t.jsonl").read_text().strip())
        assert line["run_name"] == "r1" and "git_sha" in line["tags"]
    print("selfcheck OK (tracking JSONL env-agnóstico)")


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        _selfcheck()
