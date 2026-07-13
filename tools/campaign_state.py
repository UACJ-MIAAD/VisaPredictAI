"""Maquina de estados de una campana-transaccion inmutable (auditoria 13-jul-2026 ronda 9).

``campaign.json`` (schema_version 2) es la fuente unica de identidad Y estado de UNA campana.
Se escribe SIEMPRE de forma atomica (tmp + ``os.replace``), nunca con ``printf``, y solo
avanza por la maquina:

    RUNNING --exito tecnico--> COMPUTED --gates+revision--> VALIDATED --publish--> PUBLISHED
       |                           |
       +--fallo/interrupcion-------+-------------------------> FAILED

Solo VALIDATED autoriza publicar. RUNNING/COMPUTED/FAILED (y un SIGKILL que deja RUNNING)
SIEMPRE bloquean. Stdlib-only. La validacion cruzada rica (recibo, HEAD, staging, hashes de
artefactos) vive en tools/finalize_campaign.py y el publicador; aqui solo el esquema + estado.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SCHEMA_VERSION = 2
STATUSES = ("running", "computed", "failed", "validated", "published")
PUBLISHABLE = "validated"
# Transiciones permitidas (de -> conjunto de destinos legitimos).
_ALLOWED = {
    "running": {"computed", "failed"},
    "computed": {"validated", "failed"},
    "validated": {"published", "failed"},
    "failed": set(),
    "published": set(),
}
# Campos que NUNCA cambian tras sellar RUNNING (identidad de la transaccion).
_IMMUTABLE = ("schema_version", "campaign_id", "source_git_sha", "git_dirty", "panel_sha256", "started_at")
_REQUIRED = frozenset(
    {"schema_version", "campaign_id", "status", "source_git_sha", "git_dirty", "panel_sha256", "started_at"}
)


def _no_dupes(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def loads(text: str) -> dict:
    """``json.loads`` que RECHAZA claves duplicadas (un atacante no puede esconder un segundo
    ``status`` que un parser laxo tomaria)."""
    obj = json.loads(text, object_pairs_hook=_no_dupes)
    if not isinstance(obj, dict):
        raise ValueError("campaign.json no es un objeto")
    return obj


def read(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return loads(p.read_text())
    except OSError, ValueError, json.JSONDecodeError:
        return None


def atomic_write(path: str | Path, obj: dict) -> None:
    """Escribe ``obj`` como JSON de forma atomica: tmp en el mismo dir + ``os.replace``.

    Una campana fallida a mitad de escritura nunca deja un ``campaign.json`` truncado."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".campaign.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def validate_schema(obj: object) -> list[str]:
    """Esquema completo: tipos exactos + status en enum. Devuelve la lista de problemas."""
    probs: list[str] = []
    if not isinstance(obj, dict):
        return ["campaign.json no es un objeto"]
    missing = _REQUIRED - set(obj.keys())
    if missing:
        probs.append(f"campaign.json: faltan campos {sorted(missing)}")
    if obj.get("schema_version") != SCHEMA_VERSION:
        probs.append(f"campaign.json: schema_version {obj.get('schema_version')!r} != {SCHEMA_VERSION}")
    if obj.get("status") not in STATUSES:
        probs.append(f"campaign.json: status {obj.get('status')!r} fuera del enum {list(STATUSES)}")
    for field in ("campaign_id", "source_git_sha", "panel_sha256", "started_at"):
        if field in obj and not isinstance(obj.get(field), str):
            probs.append(f"campaign.json: {field} debe ser string")
    if "git_dirty" in obj and not isinstance(obj.get("git_dirty"), bool):
        probs.append("campaign.json: git_dirty debe ser booleano")
    if isinstance(obj.get("source_git_sha"), str) and len(obj["source_git_sha"]) != 40:
        probs.append("campaign.json: source_git_sha debe ser el SHA COMPLETO de 40 caracteres")
    return probs


def seal_running(
    path: str | Path, *, campaign_id: str, source_git_sha: str, git_dirty: bool, panel_sha256: str, started_at: str
) -> dict:
    """Sella la campana en estado RUNNING (identidad fija). Atomica."""
    obj = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "status": "running",
        "source_git_sha": source_git_sha,
        "git_dirty": bool(git_dirty),
        "panel_sha256": panel_sha256,
        "started_at": started_at,
        "completed_at": None,
        "validated_at": None,
        "input_gate": None,
        "output_gate": None,
        "consistency": None,
        "exit_code": None,
        "failed_stage": None,
        "reviewed_by": None,
    }
    problems = validate_schema(obj)
    if problems:
        raise ValueError(f"seal_running invalido: {problems}")
    atomic_write(path, obj)
    return obj


def transition(path: str | Path, target: str, **updates) -> dict:
    """Avanza el estado a ``target`` si la transicion es legitima y no toca campos inmutables.

    Fail-closed: estado ausente/invalido, transicion no permitida, o intento de mutar un campo
    de identidad -> ValueError. Escritura atomica.
    """
    obj = read(path)
    if obj is None:
        raise ValueError(f"campaign.json ausente/ilegible en {path}")
    problems = validate_schema(obj)
    if problems:
        raise ValueError(f"campaign.json invalido antes de la transicion: {problems}")
    cur = obj.get("status")
    allowed = _ALLOWED.get(cur, set()) if isinstance(cur, str) else set()
    if target not in STATUSES:
        raise ValueError(f"estado destino desconocido: {target!r}")
    if target not in allowed:
        raise ValueError(f"transicion no permitida: {cur} -> {target}")
    for k in updates:
        if k in _IMMUTABLE:
            raise ValueError(f"intento de mutar campo inmutable: {k}")
    new = {**obj, **updates, "status": target}
    atomic_write(path, new)
    return new
