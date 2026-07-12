"""Backfill de los runs HISTÓRICOS de ``mlflow.db`` (A2): etiquetar, jamás fabricar.

Los 13,165 runs pre-v2 se ingirieron con ``artifact_uri`` absoluta (``file:///Users/...``)
y sin procedencia v2. Este script NO reescribe los runs ni inventa procedencia: solo

1. **Etiqueta** cada run legado (sin tag ``schema_version``) con ``legacy_status``:
   - ``invalid`` — el run no tiene ni una métrica (no cuenta como corrida exitosa).
   - ``legacy_complete`` — sus artefactos existen físicamente bajo ``mlartifacts/`` del repo.
   - ``legacy_metrics_only`` — métricas/params presentes, sin artefactos físicos (la mayoría).
2. **Repara la raíz de artefactos de los 19 experiments** a una ruta RELATIVA al repo
   (``mlartifacts/{name}``), para que los runs NUEVOS hereden URIs portables. Los
   ``artifact_uri`` históricos de cada run NO se tocan (quedan documentados en
   ``docs/mlops_experimentos.md``: se leen resolviendo el sufijo ``mlartifacts/...``
   contra la raíz del repo).

Idempotente (re-correr no cambia nada) y **dry-run por defecto** (usar ``--apply``).
stdlib puro (sqlite3): corre en el venv ``ante`` sin mlflow.

Uso:
    ante/bin/python experiments/backfill_mlflow_legacy.py            # dry-run (solo reporta)
    ante/bin/python experiments/backfill_mlflow_legacy.py --apply    # aplica (1 transacción)

Tras ``--apply`` el orquestador debe correr ``dvc commit mlflow.db.dvc`` (la db es
DVC-tracked y gitignored).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "mlflow.db"
ARTIFACTS_DIRNAME = "mlartifacts"

STATUS_INVALID = "invalid"
STATUS_COMPLETE = "legacy_complete"
STATUS_METRICS_ONLY = "legacy_metrics_only"


def _artifact_dir(artifact_uri: str, artifacts_root: Path) -> Path | None:
    """Ruta física local del artifact_uri histórico (sufijo tras ``mlartifacts/``)."""
    marker = f"{ARTIFACTS_DIRNAME}/"
    if marker not in artifact_uri:
        return None
    return artifacts_root / artifact_uri.split(marker, 1)[1]


def classify(con: sqlite3.Connection, artifacts_root: Path) -> dict[str, str]:
    """run_uuid -> legacy_status para los runs SIN tag schema_version (legados)."""
    cur = con.cursor()
    with_metrics = {r[0] for r in cur.execute("SELECT DISTINCT run_uuid FROM metrics")}
    v2_runs = {r[0] for r in cur.execute("SELECT run_uuid FROM tags WHERE key='schema_version'")}
    out: dict[str, str] = {}
    for run_uuid, artifact_uri in cur.execute("SELECT run_uuid, COALESCE(artifact_uri,'') FROM runs"):
        if run_uuid in v2_runs:
            continue  # runs v2: procedencia propia, no son legado
        if run_uuid not in with_metrics:
            out[run_uuid] = STATUS_INVALID
            continue
        adir = _artifact_dir(artifact_uri, artifacts_root)
        has_files = adir is not None and adir.is_dir() and any(adir.rglob("*"))
        out[run_uuid] = STATUS_COMPLETE if has_files else STATUS_METRICS_ONLY
    return out


def portable_roots(con: sqlite3.Connection) -> dict[str, str]:
    """experiment_id -> artifact_location relativa, para las raíces hoy absolutas."""
    cur = con.cursor()
    fixes: dict[str, str] = {}
    for exp_id, name, loc in cur.execute("SELECT experiment_id, name, artifact_location FROM experiments"):
        loc = loc or ""
        if loc.startswith(("file:///", "/")):  # absoluta (con o sin esquema) -> portable
            fixes[str(exp_id)] = f"{ARTIFACTS_DIRNAME}/{name}"
    return fixes


def apply_backfill(con: sqlite3.Connection, statuses: dict[str, str], roots: dict[str, str]) -> None:
    """Aplica tags + raíces en UNA transacción (INSERT OR REPLACE ⇒ idempotente)."""
    cur = con.cursor()
    cur.executemany(
        "INSERT OR REPLACE INTO tags (key, value, run_uuid) VALUES ('legacy_status', ?, ?)",
        [(status, run_uuid) for run_uuid, status in statuses.items()],
    )
    cur.executemany(
        "UPDATE experiments SET artifact_location = ? WHERE experiment_id = ?",
        [(loc, exp_id) for exp_id, loc in roots.items()],
    )
    con.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH, help="ruta a mlflow.db (default: raíz del repo)")
    parser.add_argument("--root", type=Path, default=ROOT, help="raíz del repo (donde vive mlartifacts/)")
    parser.add_argument("--apply", action="store_true", help="aplica los cambios (default: dry-run)")
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"ERROR: no existe {args.db}", file=sys.stderr)
        return 1

    mode = "rw" if args.apply else "ro"
    con = sqlite3.connect(f"file:{args.db}?mode={mode}", uri=True)
    try:
        statuses = classify(con, args.root / ARTIFACTS_DIRNAME)
        roots = portable_roots(con)
        counts = {s: sum(1 for v in statuses.values() if v == s) for s in sorted(set(statuses.values()))}
        report = {
            "mode": "apply" if args.apply else "dry-run",
            "db": str(args.db),
            "legacy_runs": len(statuses),
            "by_status": counts,
            "experiment_roots_to_fix": len(roots),
            "note": "solo tags legacy_status + raíces de experiment; procedencia desconocida queda unknown, jamás fabricada",
        }
        if args.apply:
            apply_backfill(con, statuses, roots)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
