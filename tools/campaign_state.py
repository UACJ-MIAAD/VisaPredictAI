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

import datetime as dt
import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3
STATUSES = ("running", "computed", "failed", "validated", "published")
TERMINAL = frozenset({"failed", "published"})
PUBLISHABLE = "validated"
_ALLOWED = {
    "running": {"computed", "failed"},
    "computed": {"validated", "failed"},
    "validated": {"published", "failed"},
    "failed": set(),
    "published": set(),
}
# Identidad: NUNCA cambia tras sellar RUNNING. ★ M74-E-R12: incluye el sha256 del sello comparado en vivo.
_IDENTIDAD = ("campaign_id", "source_git_sha", "git_dirty", "panel_sha256", "started_at", "input_seal_sha256")
_IMMUTABLE = ("schema_version", *_IDENTIDAD)
_REQUIRED = frozenset({*_IMMUTABLE, "status", "revision"})
# Toda clave legitima de campaign.json (una clave fuera de aqui = esquema roto).
ALLOWED_KEYS = _REQUIRED | frozenset(
    {
        "completed_at",
        "failed_at",
        "validated_at",
        "published_at",
        "input_gate",
        "output_gate",
        "consistency",
        "exit_code",
        "signal",
        "failed_stage",
        "reason",
        "reviewed_by",
        "decision",
        "validation_receipt_sha256",
        "release_sha",
        "validation_receipt_path",
        "best_effort_failures",
    }
)
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GATE_OK = "passed"
#: H2 · «cómputo completo, propagación pendiente» NO es un fallo. La campaña llega a `computed`
#: con la consistencia en `pending` y es `validate` quien la vuelve a exigir en `passed`, tras
#: propagar. Antes, la consistencia rota mandaba la campaña a `failed`, que es TERMINAL: el
#: resultado positivo —que las cifras cambien— no tenía salida salvo repetir 8-11 h de cómputo.
_GATE_PENDING = "pending"
_GATE_VALUES = (_GATE_OK, _GATE_PENDING)
#: Claves que NO pueden coexistir con un estado: un `validated` que arrastre `failed_stage`
#: describe dos historias a la vez.
_STATE_FORBIDS: dict[str, tuple[str, ...]] = {
    "running": ("completed_at", "validated_at", "published_at", "failed_at", "failed_stage", "reason", "release_sha"),
    "computed": ("validated_at", "published_at", "failed_at", "failed_stage", "reason", "release_sha"),
    "validated": ("published_at", "failed_at", "failed_stage", "reason", "release_sha"),
    "published": ("failed_at", "failed_stage", "reason"),
    "failed": (),
}


def _valid_ts(v: object) -> bool:
    if not isinstance(v, str) or not v:
        return False
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return d.tzinfo is not None


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


#: Lo que cada estado DEBE llevar ya relleno. Un estado no es una etiqueta: es su evidencia.
_STATE_REQUIRES: dict[str, tuple[str, ...]] = {
    "running": (),
    "computed": ("completed_at", "input_gate", "output_gate", "consistency"),
    "failed": ("failed_at", "failed_stage", "reason"),
    "validated": (
        "completed_at",
        "input_gate",
        "output_gate",
        "consistency",
        "validated_at",
        "reviewed_by",
        "decision",
        "validation_receipt_sha256",
    ),
    "published": (
        "completed_at",
        "input_gate",
        "output_gate",
        "consistency",
        "validated_at",
        "reviewed_by",
        "decision",
        "validation_receipt_sha256",
        "published_at",
        "release_sha",
    ),
}


def _state_invariants(obj: dict) -> list[str]:
    """Invariantes POR ESTADO, exigidas al LEER y no sólo al transicionar.

    ⚠️ M72-R2 las añadió comprobando sólo ``is None``, y eso deja pasar una forja completa: un
    ``validated`` con ``input_gate="failed"``, ``reviewed_by=""``, ``revision=0`` y ``git_dirty``
    verdadero pasaba el esquema y abría la publicación (H3 de la auditoría ciega). Aquí se exige
    el VALOR, no la mera presencia.
    """
    probs: list[str] = []
    estado = obj.get("status")
    if not isinstance(estado, str) or estado not in STATUSES:
        return probs  # ya lo reporta validate_schema

    for campo in _STATE_REQUIRES[estado]:
        if obj.get(campo) is None:
            probs.append(f"campaign.json: estado '{estado}' exige {campo} no nulo")

    # claves incompatibles con el estado
    for campo in _STATE_FORBIDS[estado]:
        if obj.get(campo) is not None:
            probs.append(f"campaign.json: estado '{estado}' no admite {campo}")

    # revisión: sólo `running` recién sellado puede estar en 0
    rev = obj.get("revision")
    if isinstance(rev, int) and not isinstance(rev, bool) and estado != "running" and rev < 1:
        probs.append(f"campaign.json: estado '{estado}' exige revision >= 1 (hay {rev})")

    # los gates llevan VALOR, no cualquier cadena
    for campo in ("input_gate", "output_gate"):
        valor = obj.get(campo)
        if valor is not None and valor != _GATE_OK:
            probs.append(f"campaign.json: {campo} debe ser '{_GATE_OK}' (hay {valor!r})")
    consistencia = obj.get("consistency")
    if consistencia is not None and consistencia not in _GATE_VALUES:
        probs.append(f"campaign.json: consistency debe estar en {list(_GATE_VALUES)} (hay {consistencia!r})")
    # ★ `pending` sólo vive en `computed`: validar y publicar exigen la consistencia acreditada
    if consistencia == _GATE_PENDING and estado in {"validated", "published"}:
        probs.append(f"campaign.json: estado '{estado}' no admite consistency='{_GATE_PENDING}'")

    # cadenas que existen para que alguien las lea
    for campo in ("reviewed_by", "decision", "failed_stage", "reason"):
        valor = obj.get(campo)
        if valor is not None and (not isinstance(valor, str) or not valor.strip()):
            probs.append(f"campaign.json: {campo} no puede ser una cadena vacía")

    if estado in {"validated", "published"}:
        recibo = obj.get("validation_receipt_sha256")
        if recibo is not None and (not isinstance(recibo, str) or not _HEX64.match(recibo)):
            probs.append("campaign.json: validation_receipt_sha256 debe ser 64 hex")
        ruta = obj.get("validation_receipt_path")
        if ruta is not None and (not isinstance(ruta, str) or not ruta.strip()):
            probs.append("campaign.json: validation_receipt_path no puede ser una cadena vacía")
    if estado == "published":
        rel = obj.get("release_sha")
        if rel is not None and (not isinstance(rel, str) or not _HEX40.match(rel)):
            probs.append("campaign.json: release_sha debe ser hex de 40")

    fallos = obj.get("best_effort_failures")
    if fallos is not None and not (isinstance(fallos, list) and all(isinstance(x, str) for x in fallos)):
        probs.append("campaign.json: best_effort_failures debe ser una lista de cadenas")
    return probs


def validate_schema(obj: object) -> list[str]:
    """Esquema ESTRICTO: tipos exactos, formatos canonicos, sin claves desconocidas."""
    probs: list[str] = []
    if not isinstance(obj, dict):
        return ["campaign.json no es un objeto"]
    missing = _REQUIRED - set(obj.keys())
    if missing:
        probs.append(f"campaign.json: faltan campos {sorted(missing)}")
    unknown = set(obj.keys()) - ALLOWED_KEYS
    if unknown:
        probs.append(f"campaign.json: claves desconocidas {sorted(unknown)}")
    if obj.get("schema_version") != SCHEMA_VERSION:
        probs.append(f"campaign.json: schema_version {obj.get('schema_version')!r} != {SCHEMA_VERSION}")
    if obj.get("status") not in STATUSES:
        probs.append(f"campaign.json: status {obj.get('status')!r} fuera del enum {list(STATUSES)}")
    rev = obj.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        probs.append(f"campaign.json: revision {rev!r} debe ser int >= 0")
    cid = obj.get("campaign_id")
    if not isinstance(cid, str) or not cid.strip():
        probs.append("campaign.json: campaign_id vacio/no-string")
    sha = obj.get("source_git_sha")
    if not isinstance(sha, str) or not _HEX40.match(sha):
        probs.append("campaign.json: source_git_sha debe ser hex de 40 ([0-9a-f]{40})")
    ph = obj.get("panel_sha256")
    if not isinstance(ph, str) or not _SHA256.match(ph):
        probs.append("campaign.json: panel_sha256 debe ser 'sha256:'+64 hex (no 'n/d')")
    if not isinstance(obj.get("input_seal_sha256"), str) or not _HEX64.match(obj["input_seal_sha256"]):
        probs.append("campaign.json: input_seal_sha256 debe ser el sha256 (64 hex) del sello de entradas")
    # git_dirty EXACTAMENTE bool (isinstance excluye 1/0; bool es subclase de int)
    if not isinstance(obj.get("git_dirty"), bool):
        probs.append("campaign.json: git_dirty debe ser booleano exacto")
    if not _valid_ts(obj.get("started_at")):
        probs.append("campaign.json: started_at debe ser timestamp RFC 3339 con tz")
    probs += _state_invariants(obj)
    return probs


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
