"""Maquina de estados de una campana-transaccion inmutable (endurecida, ronda 10).

``campaign.json`` (schema_version 2) es la fuente unica de identidad Y estado de UNA campana.
Se escribe SIEMPRE de forma atomica (tmp + ``os.replace`` + fsync de archivo Y directorio),
NUNCA con ``printf``, y solo avanza por la maquina bajo ``flock`` con CAS de ``revision``:

    RUNNING --exito tecnico--> COMPUTED --gates+revision--> VALIDATED --publish--> PUBLISHED
       |                           |
       +--fallo/interrupcion-------+-------------------------> FAILED

Solo VALIDATED autoriza publicar. RUNNING/COMPUTED/FAILED (y un SIGKILL que deja RUNNING)
SIEMPRE bloquean; un estado terminal NUNCA retrocede. Stdlib-only.

Endurecimientos (auditoria 13-jul-2026 ronda 10, falsos verdes reproducidos):
* ``seal_running`` es CREATE-ONLY (O_EXCL): reiniciar una campana existente/terminal aborta;
* cada transicion toma ``flock`` exclusivo, RELEE, verifica estado+revision esperados y valida
  el objeto DESTINO antes de escribir: una carrera failed/computed no puede pisar al terminal;
* API tipada (``mark_computed/mark_failed/mark_validated``) con invariantes por estado: no se
  llega a computed/validated con gates, reviewer, recibo o timestamps en ``null``;
* esquema estricto: SHA git ``[0-9a-f]{40}``, hashes ``sha256:[0-9a-f]{64}``, timestamps
  RFC 3339 con tz, ``campaign_id`` no vacio, ``git_dirty`` bool EXACTO (sin coercion), y se
  rechazan claves desconocidas.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

# G7 · puerta 1: el esquema es UNA autoridad (`vp_model.campaign_schema`); la máquina lo usa desde allí.
from vp_model.campaign_schema import (
    _GATE_OK,
    _GATE_VALUES,
    _HEX40,
    _HEX64,
    _IDENTIDAD,
    _IMMUTABLE,
    ALLOWED_KEYS,
    SCHEMA_VERSION,
    STATUSES,
    _valid_ts,
    validate_schema,
)
from vp_model.campaign_schema import _GATE_PENDING as _GATE_PENDING  # reexportado: lo leen las pruebas

TERMINAL = frozenset({"failed", "published"})
PUBLISHABLE = "validated"
_ALLOWED = {
    "running": {"computed", "failed"},
    "computed": {"validated", "failed"},
    "validated": {"published", "failed"},
    "failed": set(),
    "published": set(),
}


def _no_dupes(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def loads(text: str) -> dict:
    """``json.loads`` que RECHAZA claves duplicadas (nadie esconde un segundo ``status``)."""
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


def _fsync_dir(p: Path) -> None:
    fd = os.open(str(p.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass  # algunos FS no soportan fsync de dir; el rename ya es atomico
    finally:
        os.close(fd)


def atomic_write(path: str | Path, obj: dict) -> None:
    """JSON atomico: tmp en el mismo dir + fsync de archivo + ``os.replace`` + fsync del dir."""
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
        _fsync_dir(p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def _campaign_lock(path: Path):
    """``flock`` exclusivo por campana (serializa transiciones concurrentes)."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def seal_running(path: str | Path, **identidad: object) -> dict:
    """Sella la campana en RUNNING. CREATE-ONLY: si ``campaign.json`` ya existe, ABORTA.

    ``identidad`` son EXACTAMENTE los campos inmutables salvo ``schema_version`` (campaign_id,
    source_git_sha, git_dirty, panel_sha256, started_at, input_seal_sha256): ni uno de más ni de menos.
    Reiniciar una campana existente es un error, no un reemplazo. ``git_dirty`` debe ser bool EXACTO."""
    p = Path(path)
    if set(identidad) != set(_IDENTIDAD) or not isinstance(identidad.get("git_dirty"), bool):
        raise ValueError(f"seal_running: identidad {sorted(identidad)} incompleta o git_dirty no booleano")
    obj = {
        "schema_version": SCHEMA_VERSION,
        **identidad,
        "status": "running",
        "revision": 0,
        "completed_at": None,
        "failed_at": None,
        "validated_at": None,
        "input_gate": None,
        "output_gate": None,
        "consistency": None,
        "reviewed_by": None,
    }
    problems = validate_schema(obj)
    if problems:
        raise ValueError(f"seal_running invalido: {problems}")
    p.parent.mkdir(parents=True, exist_ok=True)
    # create-only: O_EXCL falla si el destino existe
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as e:
        raise ValueError(f"seal_running: {p} ya existe — una campana no se reinicia, se crea nueva") from e
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(p)
    except BaseException:
        p.unlink(missing_ok=True)
        raise
    return obj


def _transition(path: str | Path, target: str, updates: dict, *, expected_revision: int | None = None) -> dict:
    """Primitive INTERNO: bajo lock, relee, verifica from-status/revision, valida DESTINO, escribe.

    Usar las APIs tipadas (mark_*). Fail-closed ante estado ausente/invalido, transicion no
    permitida, revision stale, mutacion de campo inmutable, clave desconocida o destino invalido.
    """
    p = Path(path)
    with _campaign_lock(p):
        obj = read(p)
        if obj is None:
            raise ValueError(f"campaign.json ausente/ilegible en {p}")
        problems = validate_schema(obj)
        if problems:
            raise ValueError(f"campaign.json invalido antes de la transicion: {problems}")
        cur = obj.get("status")
        allowed = _ALLOWED.get(cur, set()) if isinstance(cur, str) else set()
        if target not in STATUSES:
            raise ValueError(f"estado destino desconocido: {target!r}")
        if target not in allowed:
            raise ValueError(f"transicion no permitida: {cur} -> {target} (terminal no retrocede)")
        if expected_revision is not None and obj.get("revision") != expected_revision:
            raise ValueError(f"revision stale: esperada {expected_revision}, actual {obj.get('revision')}")
        for k in updates:
            if k in _IMMUTABLE:
                raise ValueError(f"intento de mutar campo inmutable: {k}")
            if k not in ALLOWED_KEYS:
                raise ValueError(f"update con clave desconocida: {k}")
        new = {**obj, **updates, "status": target, "revision": int(obj["revision"]) + 1}
        # valida el objeto DESTINO (no solo el origen) antes de persistir
        problems = validate_schema(new)
        if problems:
            raise ValueError(f"campaign.json DESTINO invalido: {problems}")
        atomic_write(p, new)
        return new


def mark_computed(
    path: str | Path,
    *,
    completed_at: str,
    input_gate: str,
    output_gate: str,
    consistency: str,
    exit_code: int = 0,
    best_effort_failures: list[str] | None = None,
    expected_revision: int | None = None,
) -> dict:
    """running -> computed. Exige exito tecnico y las dos puertas duras en 'passed'.

    ``consistency`` admite ``'pending'``: el computo termino y las cifras cambiaron, que es un
    resultado ESPERADO y no un fallo (H2). ``mark_validated`` es quien la exige en ``'passed'``,
    despues de propagar. ``best_effort_failures`` deja constancia de las etapas tolerables que
    fallaron, para que la revision humana no tenga que descubrirlas leyendo la bitacora (H16).
    """
    if exit_code != 0:
        raise ValueError(f"mark_computed: exit_code={exit_code} != 0")
    if not _valid_ts(completed_at):
        raise ValueError("mark_computed: completed_at invalido")
    if not (input_gate == output_gate == _GATE_OK):
        raise ValueError(f"mark_computed: input/output gate deben ser '{_GATE_OK}' ({input_gate}/{output_gate})")
    if consistency not in _GATE_VALUES:
        raise ValueError(f"mark_computed: consistency debe estar en {list(_GATE_VALUES)} (got {consistency})")
    updates: dict[str, object] = {
        "completed_at": completed_at,
        "input_gate": input_gate,
        "output_gate": output_gate,
        "consistency": consistency,
    }
    if best_effort_failures is not None:
        updates["best_effort_failures"] = list(best_effort_failures)
    return _transition(path, "computed", updates, expected_revision=expected_revision)


def mark_failed(
    path: str | Path,
    *,
    failed_stage: str,
    failed_at: str,
    reason: str,
    exit_code: int | None = None,
    signal: int | None = None,
    expected_revision: int | None = None,
) -> dict:
    """running|computed|validated -> failed (terminal). Exige etapa, timestamp y razon."""
    if not failed_stage or not reason:
        raise ValueError("mark_failed: failed_stage y reason son obligatorios")
    if not _valid_ts(failed_at):
        raise ValueError("mark_failed: failed_at invalido")
    upd: dict[str, object] = {"failed_stage": failed_stage, "failed_at": failed_at, "reason": reason}
    if exit_code is not None:
        upd["exit_code"] = exit_code
    if signal is not None:
        upd["signal"] = signal
    return _transition(path, "failed", upd, expected_revision=expected_revision)


def mark_validated(
    path: str | Path,
    *,
    validation_receipt_sha256: str,
    validation_receipt_path: str,
    reviewed_by: str,
    validated_at: str,
    decision: str,
    consistency: str = _GATE_OK,
    expected_revision: int | None = None,
) -> dict:
    """computed -> validated. Exige recibo con hash Y RUTA, revisor humano y decision explicita.

    ``consistency`` se vuelve a escribir aqui: si el computo quedo en ``'pending'`` porque las
    cifras cambiaron, validar exige haber propagado y acreditarla en ``'passed'`` (H2). El recibo
    se liga ademas por RUTA, para que su existencia y su hash sean verificables despues (H3).
    """
    if not isinstance(validation_receipt_sha256, str) or not _HEX64.match(validation_receipt_sha256):
        raise ValueError("mark_validated: validation_receipt_sha256 debe ser 64 hex")
    if not validation_receipt_path or not validation_receipt_path.strip():
        raise ValueError("mark_validated: validation_receipt_path obligatorio")
    if not reviewed_by or not reviewed_by.strip():
        raise ValueError("mark_validated: reviewed_by obligatorio")
    if not _valid_ts(validated_at):
        raise ValueError("mark_validated: validated_at invalido")
    if not decision or not decision.strip():
        raise ValueError("mark_validated: decision obligatoria")
    if consistency != _GATE_OK:
        raise ValueError(f"mark_validated: consistency debe ser '{_GATE_OK}' para validar (got {consistency})")
    return _transition(
        path,
        "validated",
        {
            "validation_receipt_sha256": validation_receipt_sha256,
            "validation_receipt_path": validation_receipt_path,
            "reviewed_by": reviewed_by,
            "validated_at": validated_at,
            "decision": decision,
            "consistency": consistency,
        },
        expected_revision=expected_revision,
    )


def mark_published(
    path: str | Path, *, published_at: str, release_sha: str, expected_revision: int | None = None
) -> dict:
    """validated -> published. SOLO el publicador (Phase F) con evidencia remota lo invoca."""
    if not _valid_ts(published_at):
        raise ValueError("mark_published: published_at invalido")
    if not isinstance(release_sha, str) or not _HEX40.match(release_sha):
        raise ValueError("mark_published: release_sha debe ser hex de 40")
    return _transition(
        path,
        "published",
        {"published_at": published_at, "release_sha": release_sha},
        expected_revision=expected_revision,
    )
