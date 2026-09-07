"""Gobernanza del almacén: identidad del build, auditoría y huella de contenido.

Lo que responde «¿qué produjo exactamente esta base?»: la identidad del corte que se
graba en `etl_run` (sha de git y bandera de árbol sucio, sha256 del panel, del
`dvc.lock` y del lock de entorno, identificador de la corrida), el registro de
auditoría, y `content_fingerprint`, que es el contrato determinista del almacén.

El archivo `.duckdb` NO es byte-reproducible —el reloj de la bitácora y el orden interno
de DuckDB lo impiden—, y por eso queda fuera de DVC: lo que sí se puede comparar entre
dos builds es esta huella lógica.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

# Columnas de reloj de pared: quedan FUERA de la huella de contenido a propósito.
_VOLATILE_COLUMNS = {
    "etl_run": {"built_at_utc", "started_at", "completed_at"},
    "schema_version": {"applied_at"},
}


def _load_audit(
    con: duckdb.DuckDBPyConnection,
    schema_ver: int,
    run_info: dict | None,
    build_status: str,
    degradations: list[str],
) -> None:
    """Insert the single etl_run row LAST — it doubles as the completeness
    sentinel that vp_model.dataset._connect requires (M2). Identity fields come
    from run_info (CLI/env/derived); absent -> NULL, never fabricated.
    Wall-clock is legítimo here (bitácora) and only here."""
    counts = con.execute(
        "SELECT (SELECT count(*) FROM fact_priority), (SELECT count(*) FROM fact_dv_rank), "
        "(SELECT count(*) FROM fact_priority WHERE status = 'F'), "
        "(SELECT min(bulletin_date) FROM dim_date), (SELECT max(bulletin_date) FROM dim_date)"
    ).fetchone()
    assert counts is not None
    n_fp, n_dv, n_f, floor, ceiling = counts
    info = run_info or {}
    now = datetime.now(UTC)
    con.execute(
        "INSERT INTO etl_run (run_id, built_at_utc, schema_version, n_fact_priority, n_fact_dv, "
        "n_trainable_f, pct_trainable, panel_floor, panel_ceiling, pipeline_run_id, git_sha, git_dirty, "
        "panel_sha256, dvc_lock_sha256, env_lock_sha256, started_at, completed_at, build_status, degradations) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            now,
            schema_ver,
            n_fp,
            n_dv,
            n_f,
            n_f / n_fp if n_fp else 0.0,
            floor,
            ceiling,
            info.get("pipeline_run_id"),
            info.get("git_sha"),
            info.get("git_dirty"),
            info.get("panel_sha256"),
            info.get("dvc_lock_sha256"),
            info.get("env_lock_sha256"),
            info.get("started_at"),
            now,
            build_status,
            ", ".join(degradations) if degradations else None,
        ],
    )


def content_fingerprint(db_path: str | Path) -> str:
    """sha256 over the warehouse's LOGICAL content (every base table, all rows,
    fully ordered), excluding only the bitácora wall-clock columns.

    This is the determinism contract of the .duckdb: the FILE is not
    byte-reproducible (bitácora + DuckDB internal storage order — why it lives
    outside DVC; see dvc.yaml), but two builds from the same inputs must return
    the SAME fingerprint. The Parquet export is byte-deterministic on its own
    (explicit ORDER BY) and is the DVC-tracked binary.
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute("SET TimeZone='UTC'")
        digest = hashlib.sha256()
        names = [r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() ORDER BY table_name").fetchall()]
        for tbl in names:
            skip = _VOLATILE_COLUMNS.get(tbl, set())
            cols = [r[1] for r in con.execute(f"PRAGMA table_info('{tbl}')").fetchall() if r[1] not in skip]
            col_list = ", ".join(f'"{c}"' for c in cols)
            digest.update(f"## {tbl}({col_list})\n".encode())
            for row in con.execute(f'SELECT {col_list} FROM "{tbl}" ORDER BY {col_list}').fetchall():
                digest.update(repr(row).encode())
                digest.update(b"\n")
        return digest.hexdigest()
    finally:
        con.close()


def _file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _git_identity() -> tuple[str | None, bool | None]:
    """(full sha, dirty) derived from the actual repo; (None, None) when git is
    unavailable — an honest NULL, never a fabricated identity."""
    root = Path(__file__).resolve().parents[1]
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=root, check=False)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=root, check=False)
    except OSError:
        return None, None
    full = sha.stdout.strip()
    if sha.returncode != 0 or len(full) != 40:
        return None, None
    return full, bool(status.stdout.strip()) if status.returncode == 0 else None


def _run_identity(args: argparse.Namespace, started_at: datetime, panel_path: Path) -> dict:
    """H2: build identity — CLI flag first, env second, derived from the real
    repo/files third, NULL last. Nothing here is ever invented by SQL.

    `panel_path` llega por argumento: quien decide las rutas del build es
    `build_database`, y así una prueba que las redirige no tiene que parchear dos módulos.
    """
    git_sha = args.git_sha or os.environ.get("VP_GIT_SHA") or None
    dirty_raw = args.git_dirty or os.environ.get("VP_GIT_DIRTY", "").lower() or None
    git_dirty = {"true": True, "false": False}.get(dirty_raw) if dirty_raw else None
    if git_sha is None:
        git_sha, derived_dirty = _git_identity()
        if git_dirty is None:
            git_dirty = derived_dirty
    from vp_data.tracking import pipeline_run_id  # same id the ledger/manifest use (C3)

    env_lock = args.env_lock
    if env_lock is None:
        env_lock = Path(os.environ["VP_ENV_LOCK"]) if os.environ.get("VP_ENV_LOCK") else Path("locks/runtime.txt")
    return {
        "pipeline_run_id": args.pipeline_run_id or pipeline_run_id(),
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "panel_sha256": _file_sha256(panel_path),
        "dvc_lock_sha256": _file_sha256(args.dvc_lock),
        "env_lock_sha256": _file_sha256(env_lock),
        "started_at": started_at,
    }
