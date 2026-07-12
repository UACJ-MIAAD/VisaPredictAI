"""Tracking de experimentos ENV-AGNÓSTICO (puente entre los dos venv del proyecto).

MLflow exige ``pandas<3`` y el env principal ``ante`` usa pandas 3 → no se puede importar
mlflow ahí. Este módulo es **stdlib pura** (sin mlflow, sin vp_model): corre idéntico en
``ante`` (pandas 3, pool local) y ``ante_nf`` (pandas 2, deep global). Cada experimento
escribe records JSONL en ``mlruns_staging/{experiment}.jsonl``; ``experiments/sync_mlflow.py`` (en
``ante_nf``, con mlflow) los vuelca a ``mlflow.db`` para la UI y la comparación.

Contrato del record — **schema_version 2** (A2/A6, plan auditoría 2026-07-12):

- ``content_hash`` (16 hex) — hash de CONTENIDO ``{experiment, run_name, params, metrics}``,
  idéntico bit a bit al ``rec_id`` histórico (v1): dos corridas con las mismas métricas
  comparten content_hash aunque sean eventos distintos.
- ``rec_id`` (16 hex) — **clave de EVENTO**: hash de ``experiment + run_name +
  pipeline_run_id + data_hash + code_sha + recipe_version + seed + content_hash + ts + seq``.
  Fallbacks documentados: cualquier campo de procedencia ausente entra como ``"unknown"``
  (jamás se fabrica); ``seq`` (``pid:counter``) garantiza que dos eventos del mismo proceso
  en el mismo instante no colisionen. Dos eventos distintos ⇒ rec_id distinto.
- ``provenance`` — ``pipeline_run_id``, ``data_hash`` (sha256 del panel), ``code_sha``
  (SHA completo), ``recipe_version``, ``seed``, ``env_lock_hash`` (sha256 de ``locks/*.txt``),
  ``seq``.
- ``telemetry`` (opcional, A6) — ``status`` (ok/failed), ``duration_s``, ``rss_peak_mb``,
  ``gpu_mem_mb``, ``artifact_bytes``, ``warnings``, ``exception`` tipada. La emite el
  context-manager ``vp_model.tracking.track_run``.

Las líneas viejas SIN ``schema_version`` se tratan como **v1** (el sync las sigue leyendo).
La escritura es **transaccional**: lock de archivo (``fcntl.flock``) + append + flush +
fsync — N escritores paralelos no pierden ni mezclan registros.

Uso:
    from vp_data import tracking
    tracking.log_run("pool_local", "ets_mexico_F1_FAD",
                     params={"model": "ets", "country": "mexico", "table": "FAD"},
                     metrics={"sel_mase": 0.117, "sel_smape": 0.30},
                     artifacts=["models/FAD/ets_mexico_F1.pkl"])
"""

from __future__ import annotations

import fcntl  # POSIX (macOS/Linux — los únicos targets del proyecto)
import hashlib
import itertools
import json
import math
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from vp_data import config as _config

ROOT = Path(__file__).resolve().parents[1]  # raíz del repo (el paquete vive un nivel abajo)
STAGING = ROOT / "mlruns_staging"
LOCKS_DIR = ROOT / "locks"

SCHEMA_VERSION = 2
UNKNOWN = "unknown"

# seq de proceso: distingue dos eventos idénticos (mismo contenido, mismo ts) del mismo pid.
_SEQ = itertools.count()


def pipeline_run_id() -> str:
    """Identidad del run de pipeline que produjo esta corrida (C3, jerarquía de IDs).

    Precedencia (auditoría 12-jul-2026): ``CAMPAIGN_ID`` (sellado por
    run_rederivation.sh: TODOS los records de una campaña comparten un id) →
    ``VP_PIPELINE_RUN_ID`` (cron, =$GITHUB_RUN_ID) → ``GITHUB_RUN_ID`` (otras Actions)
    → ``local`` (escritorio). Enlaza DVC/ledger/manifiesto/JSONL: mismo id ⇒ misma corrida.
    """
    return (
        os.environ.get("CAMPAIGN_ID")
        or os.environ.get("VP_PIPELINE_RUN_ID")
        or os.environ.get("GITHUB_RUN_ID")
        or "local"
    )


def git_state() -> tuple[str, bool]:
    """(short sha, dirty) for provenance; tolerant when git is unavailable.

    Si ``CAMPAIGN_SHA`` está sellado (run_rederivation.sh exigió árbol limpio al inicio),
    se usa ESE sha fijo con dirty=False para TODOS los records de la campaña — así, aunque
    HEAD avance a mitad de corrida, los outputs no quedan con SHAs mezclados (bug 12-jul).
    """
    pinned = os.environ.get("CAMPAIGN_SHA")
    if pinned:
        return pinned[:7], False
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=False
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT, check=False
            ).stdout.strip()
        )
        return sha or UNKNOWN, dirty
    except Exception:  # noqa: BLE001 — el tracking nunca debe abortar la corrida
        return UNKNOWN, True


# Backwards-compat alias (AP5): external callers used the private name before the rename.
_git = git_state


@lru_cache(maxsize=1)
def code_sha() -> str:
    """SHA COMPLETO de HEAD (procedencia v2). ``unknown`` si git no está disponible.

    Cacheado por proceso: HEAD no cambia a mitad de una campaña y ahorra un
    subprocess por cada record (hay campañas de ~5,000 records). Si ``CAMPAIGN_SHA``
    está sellado, se usa ESE (identidad fija de campaña) en vez del HEAD vivo.
    """
    pinned = os.environ.get("CAMPAIGN_SHA")
    if pinned:
        return pinned
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=False
        ).stdout.strip()
        return sha or UNKNOWN
    except OSError, subprocess.SubprocessError, ValueError:
        return UNKNOWN


@lru_cache(maxsize=8)
def _file_sha256(path_str: str, mtime_ns: int, size: int) -> str:
    """sha256 de un archivo, cacheado por (path, mtime, size) — se recalcula si cambia."""
    h = hashlib.sha256()
    with open(path_str, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def panel_hash() -> str:
    """``sha256:<hex>`` del panel canónico (input dataset); ``unknown`` si no existe."""
    panel = ROOT / _config.PANEL_PATH
    try:
        st = panel.stat()
        return f"sha256:{_file_sha256(str(panel), st.st_mtime_ns, st.st_size)}"
    except OSError:
        return UNKNOWN


@lru_cache(maxsize=1)
def env_lock_hash() -> str:
    """``sha256:<hex>`` del entorno: hash conjunto de ``locks/*.txt``; ``unknown`` si faltan."""
    try:
        locks = sorted(LOCKS_DIR.glob("*.txt"))
        if not locks:
            return UNKNOWN
        h = hashlib.sha256()
        for p in locks:
            h.update(p.name.encode())
            h.update(p.read_bytes())
        return f"sha256:{h.hexdigest()}"
    except OSError:
        return UNKNOWN


def content_hash(experiment: str, run_name: str, params: dict, metrics: dict) -> str:
    """Hash de CONTENIDO — cálculo idéntico al ``rec_id`` v1 (compatibilidad histórica)."""
    payload = {"experiment": experiment, "run_name": run_name, "params": params, "metrics": metrics}
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _event_key(experiment: str, run_name: str, provenance: dict, content: str, ts: float) -> str:
    """Clave de EVENTO v2 (ver contrato en el docstring del módulo)."""
    basis = {
        "experiment": experiment,
        "run_name": run_name,
        "content_hash": content,
        "ts": ts,
        "pipeline_run_id": provenance["pipeline_run_id"],
        "data_hash": provenance["data_hash"],
        "code_sha": provenance["code_sha"],
        "recipe_version": provenance["recipe_version"],
        "seed": provenance["seed"],
        "seq": provenance["seq"],
    }
    return hashlib.sha1(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _locked_append(path: Path, line: str) -> None:
    """Append transaccional (A6): flock exclusivo + write + flush + fsync."""
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _clean_telemetry(telemetry: dict | None) -> dict[str, Any] | None:
    """Normaliza el bloque de telemetría (numéricos no finitos → None; status acotado)."""
    if telemetry is None:
        return None
    out: dict[str, Any] = dict(telemetry)
    out["status"] = "failed" if str(out.get("status", "ok")).lower() == "failed" else "ok"
    for key in ("duration_s", "rss_peak_mb", "gpu_mem_mb", "artifact_bytes"):
        v = out.get(key)
        if v is not None:
            try:
                fv = float(v)
                out[key] = None if not math.isfinite(fv) else (int(fv) if key == "artifact_bytes" else fv)
            except TypeError, ValueError:
                out[key] = None
        else:
            out[key] = None
    out["warnings"] = [str(w) for w in (out.get("warnings") or [])]
    exc = out.get("exception")
    if exc is not None:
        out["exception"] = {"type": str(exc.get("type", UNKNOWN)), "message": str(exc.get("message", ""))[:500]}
    else:
        out["exception"] = None
    return out


def log_run(
    experiment: str,
    run_name: str,
    params: dict,
    metrics: dict,
    tags: dict | None = None,
    artifacts: list[str] | None = None,
    ts: float | None = None,
    *,
    data_hash: str | None = None,
    recipe_version: str | None = None,
    seed: int | str | None = None,
    telemetry: dict | None = None,
) -> dict:
    """Anexa 1 record v2 al staging JSONL del experimento. Devuelve el record.

    ``metrics`` con valores no finitos (NaN/inf) se omiten (mlflow los rechaza).

    Procedencia (fallbacks — nunca se fabrica):
    - ``data_hash``: si no se pasa, sha256 del panel canónico; ``unknown`` si no existe.
    - ``recipe_version``: kwarg → ``params["recipe_version"]`` → ``unknown``.
    - ``seed``: kwarg → ``params["seed"]`` → ``unknown``.
    - ``code_sha``/``env_lock_hash``: derivados del repo; ``unknown`` si no resolubles.

    ``rec_id`` es la clave de EVENTO (no colisiona entre eventos distintos);
    ``content_hash`` conserva la semántica del ``rec_id`` v1.
    """
    staging = STAGING
    staging.mkdir(exist_ok=True)
    sha, dirty = git_state()
    clean_metrics = {k: float(v) for k, v in metrics.items() if v is not None and math.isfinite(float(v))}
    stamp = ts if ts is not None else time.time()
    resolved_seed: int | str = UNKNOWN
    raw_seed = seed if seed is not None else params.get("seed")
    if raw_seed is not None:
        try:
            resolved_seed = int(raw_seed)
        except TypeError, ValueError:
            resolved_seed = str(raw_seed)
    provenance = {
        "pipeline_run_id": pipeline_run_id(),
        "data_hash": data_hash if data_hash is not None else panel_hash(),
        "code_sha": code_sha(),
        "recipe_version": str(recipe_version if recipe_version is not None else params.get("recipe_version", UNKNOWN)),
        "seed": resolved_seed,
        "env_lock_hash": env_lock_hash(),
        "seq": f"{os.getpid()}:{next(_SEQ)}",
    }
    chash = content_hash(experiment, run_name, params, clean_metrics)
    rec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "experiment": experiment,
        "run_name": run_name,
        "params": params,
        "metrics": clean_metrics,
        # C3: identidad jerárquica — cada record queda enlazable al run del pipeline
        # que lo produjo (mismo pipeline_run_id ⇒ ledger/manifiesto/JSONL de una corrida).
        "tags": {
            **(tags or {}),
            "git_sha": sha,
            "git_dirty": str(dirty),
            "pipeline_run_id": provenance["pipeline_run_id"],
        },
        "artifacts": [a for a in (artifacts or []) if a],
        "ts": stamp,
        "provenance": provenance,
        "content_hash": chash,
        "rec_id": _event_key(experiment, run_name, provenance, chash, stamp),
    }
    tel = _clean_telemetry(telemetry)
    if tel is not None:
        rec["telemetry"] = tel
    _locked_append(staging / f"{experiment}.jsonl", json.dumps(rec) + "\n")
    return rec


def _selfcheck() -> None:
    import tempfile

    global STAGING
    with tempfile.TemporaryDirectory() as d:
        STAGING = Path(d)
        r = log_run("t", "r1", {"model": "ets"}, {"mase": 0.12, "bad": float("nan")})
        assert r["metrics"] == {"mase": 0.12}  # NaN filtrado
        assert len(r["rec_id"]) == 16 and len(r["content_hash"]) == 16
        assert r["schema_version"] == SCHEMA_VERSION
        r2 = log_run("t", "r1", {"model": "ets"}, {"mase": 0.12})
        assert r2["content_hash"] == r["content_hash"]  # mismo contenido
        assert r2["rec_id"] != r["rec_id"]  # eventos DISTINTOS no colisionan
        line = json.loads((STAGING / "t.jsonl").read_text().splitlines()[0])
        assert line["run_name"] == "r1" and "git_sha" in line["tags"]
        assert line["tags"]["pipeline_run_id"] and line["provenance"]["data_hash"]
    print("selfcheck OK (tracking JSONL env-agnóstico, schema v2)")


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        _selfcheck()
