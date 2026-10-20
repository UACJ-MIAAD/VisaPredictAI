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
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from vp_model import campaign_evidence as _ce
from vp_model.campaign_evidence import SNAPSHOT as COMPUTED_SNAPSHOT

# G7 · puerta 1: el esquema es UNA autoridad (`vp_model.campaign_schema`); la máquina lo usa desde allí.
from vp_model.campaign_schema import (
    _GATE_OK,
    _GATE_VALUES,
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


#: G7 · desbloqueo: locks que ESTE hilo ya tiene. La publicación de una release toma el lock de la campaña para toda
#: su sección (puerta → `dvc push` → `published`) y dentro escribe el estado con la API de siempre, que vuelve a pedirlo:
#: con `flock` por descriptor, pedirlo dos veces desde el mismo proceso se bloquearía consigo mismo. Es por HILO: otro
#: hilo del mismo proceso (una revalidación concurrente) sigue esperando el lock como cualquier otro proceso.
_TOMADOS = threading.local()


@contextmanager
def _campaign_lock(path: Path):
    """``flock`` exclusivo por campana (serializa transiciones concurrentes); reentrante dentro del mismo hilo."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    clave = str(lock.parent.resolve() / lock.name)  # misma clave exista o no el archivo (y con enlaces resueltos)
    tomados: set[str] = getattr(_TOMADOS, "rutas", set())
    _TOMADOS.rutas = tomados
    if clave in tomados:
        yield
        return
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        tomados.add(clave)
        yield
    finally:
        tomados.discard(clave)
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


def _transition(
    path: str | Path,
    target: str,
    updates: dict,
    *,
    expected_revision: int | None = None,
    expected_state_sha256: str | None = None,
) -> dict:
    """Primitive INTERNO: bajo lock, relee, verifica from-status/revision, valida DESTINO, escribe.

    Usar las APIs tipadas (mark_*). Fail-closed ante estado ausente/invalido, transicion no
    permitida, revision stale, mutacion de campo inmutable, clave desconocida o destino invalido.
    """
    p = Path(path)
    with _campaign_lock(p):
        try:
            datos = p.read_bytes()
            obj = loads(datos.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"campaign.json ausente/ilegible en {p}") from exc
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
        # ★ G7 · desbloqueo: CAS también sobre la HUELLA de los bytes acreditados, no sólo sobre `revision`
        if expected_state_sha256 is not None and hashlib.sha256(datos).hexdigest() != expected_state_sha256:
            raise ValueError("huella del estado distinta de la acreditada: alguien reescribió campaign.json")
        for k in updates:
            if k in _IMMUTABLE or k == "computed_sha256":
                raise ValueError(f"intento de mutar campo inmutable: {k}")
            if k not in ALLOWED_KEYS:
                raise ValueError(f"update con clave desconocida: {k}")
        if cur == "computed" and target == "validated":
            # G7 · B5a: la evidencia del cómputo se guarda ANTES de reescribir el estado, bajo el mismo lock.
            updates = {**updates, "computed_sha256": _sellar_computo(p)}
        new = {**obj, **updates, "status": target, "revision": int(obj["revision"]) + 1}
        # valida el objeto DESTINO (no solo el origen) antes de persistir
        problems = validate_schema(new)
        if problems:
            raise ValueError(f"campaign.json DESTINO invalido: {problems}")
        atomic_write(p, new)
        return new


def _sellar_computo(p: Path) -> str:
    """Copia CREATE-ONLY y exacta del `campaign.json` en `computed` (`COMPUTED_SNAPSHOT`); devuelve su sha256.

    Si una validación anterior se interrumpió después de la copia, la copia existente sólo se acepta si es idéntica
    byte a byte: cualquier otra cosa es una «evidencia» que no es este cómputo, y la transición se niega.
    """
    datos = p.read_bytes()
    copia = p.parent / COMPUTED_SNAPSHOT
    try:
        fd = os.open(str(copia), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
    except FileExistsError:
        if copia.read_bytes() != datos:
            raise ValueError(f"evidencia del cómputo: {copia.name} ya existe con otros bytes; no se valida") from None
        return hashlib.sha256(datos).hexdigest()
    escrita = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(datos)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(copia)
        escrita = True
    finally:
        if not escrita:  # una copia a medias no puede quedar como «evidencia»
            copia.unlink(missing_ok=True)
    return hashlib.sha256(datos).hexdigest()


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


# --------------------------------------------------------------------------------------------------------------------
# G7 · desbloqueo — REVALIDACIÓN gobernada de una madre `validated` (ADR 0003, adenda)
# --------------------------------------------------------------------------------------------------------------------
#: Directorio, junto a `campaign.json`, con la historia de las autorizaciones sustituidas. Cada revalidación deja, en este
#: orden y CREATE-ONLY: la copia exacta del recibo que sustituye (`rNNNN.recibo.json`), la del estado que sustituye
#: (`rNNNN.campaign.json`) y el REGISTRO que encadena ambas (`rNNNN-rMMMM.revalidation.json`). El registro es el punto de
#: compromiso de la historia y se escribe ANTES de promocionar el estado: si la promoción cae, el registro queda
#: «preparado sin promocionar» y la puerta se cierra hasta completar (no autoriza el permiso nuevo ni revive el viejo).
HISTORIAL = "campaign.validations"
REVALIDATION_SCHEMA = "campaign-revalidation/1"
_REGISTRO_KEYS = frozenset(
    {
        "schema",
        "campaign_id",
        "computed_sha256",
        "from_revision",
        "to_revision",
        "from_state_sha256",
        "previous_receipt_sha256",
        "receipt_sha256",
        "receipt_path",
        "reviewed_by",
        "decision",
        "validated_at",
    }
)
#: Lo único que una revalidación cambia del estado vivo: la autorización y su revisión.
_REVALIDA = ("validation_receipt_sha256", "validation_receipt_path", "reviewed_by", "decision", "validated_at")


def _nombres(desde: int) -> tuple[str, str, str]:
    return (
        f"r{desde:04d}.recibo.json",
        f"r{desde:04d}.campaign.json",
        f"r{desde:04d}-r{desde + 1:04d}.revalidation.json",
    )


def _crear_exacto(destino: Path, datos: bytes) -> None:
    """CREATE-ONLY y atómico (temporal + `link`): nunca queda un archivo a medias con el nombre final. Si el destino
    existe, sólo se acepta idéntico byte a byte (un reintento); cualquier otra cosa es una historia que no es ésta."""
    if destino.exists():
        if destino.read_bytes() != datos:
            raise ValueError(f"historial: {destino.name} ya existe con otros bytes")
        return
    fd, tmp = tempfile.mkstemp(dir=str(destino.parent), prefix=".hist.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(datos)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o444)
        os.link(tmp, destino)
        _fsync_dir(destino)
    except FileExistsError:
        if destino.read_bytes() != datos:
            raise ValueError(f"historial: {destino.name} apareció con otros bytes") from None
    finally:
        Path(tmp).unlink(missing_ok=True)


def _registro_bytes(registro: dict) -> bytes:
    return (json.dumps(registro, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _leer_registro(ruta: Path) -> dict:
    obj = loads(ruta.read_text(encoding="utf-8"))
    if set(obj) != _REGISTRO_KEYS or obj.get("schema") != REVALIDATION_SCHEMA:
        raise ValueError(f"{ruta.name}: registro de revalidación con otro esquema o claves")
    for k in ("from_revision", "to_revision"):
        if isinstance(obj[k], bool) or not isinstance(obj[k], int):
            raise ValueError(f"{ruta.name}: {k} no es un entero")
    if obj["to_revision"] != obj["from_revision"] + 1:
        raise ValueError(f"{ruta.name}: una revalidación sube la revisión exactamente una vez")
    return obj


def historial_problemas(path: str | Path, obj: dict) -> list[str]:
    """La historia de autorizaciones de una madre `validated`, ACREDITADA al leer (la puerta de publicación la exige).

    Sin historial (la madre sólo se validó una vez): nada que acreditar. Con historial, los registros forman UNA cadena
    de revisiones consecutivas cuyo último eslabón es el estado vivo; cada eslabón conserva, byte a byte, el estado y el
    recibo que sustituyó; y ninguno apunta más allá del estado vivo (un registro «preparado sin promocionar», o un estado
    devuelto a mano a una revisión anterior, cierran la puerta).
    """
    carpeta = Path(path).parent / HISTORIAL
    try:
        registros = sorted(
            (_leer_registro(r) for r in carpeta.glob("r*-r*.revalidation.json")), key=lambda r: r["from_revision"]
        )
        # el ancla de la cadena: la validación ORIGINAL es la revisión del cómputo + 1 (`computed → validated`)
        base = int(loads((Path(path).parent / COMPUTED_SNAPSHOT).read_text(encoding="utf-8"))["revision"]) + 1
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return [f"historial de validaciones ilegible: {exc}"]
    rev = obj.get("revision")
    if not registros:
        # copias sin registro (caída antes del compromiso) son inertes; pero un estado más allá de la validación
        # original sin registros que lo expliquen es una historia BORRADA, no una que nunca existió
        return [] if rev == base else [f"historial: el estado está en r{rev} y no hay registros desde r{base}"]
    if registros[0]["from_revision"] != base:
        return [f"historial: la cadena empieza en r{registros[0]['from_revision']} y la validación original es r{base}"]
    pendientes = [r for r in registros if r["to_revision"] > rev]
    if pendientes:
        return [
            f"revalidación r{pendientes[0]['from_revision']}→r{pendientes[0]['to_revision']} preparada sin promocionar "
            "(o estado devuelto a una revisión anterior): complétala con el MISMO recibo"
        ]
    problemas: list[str] = []
    for previo, siguiente in zip(registros, registros[1:], strict=False):
        if siguiente["from_revision"] != previo["to_revision"]:
            problemas.append(
                f"historial: la cadena se corta entre r{previo['to_revision']} y r{siguiente['from_revision']}"
            )
    ultimo = registros[-1]
    if ultimo["to_revision"] != rev:
        problemas.append(
            f"historial: el último registro termina en r{ultimo['to_revision']} y el estado está en r{rev}"
        )
    for campo, clave in (("receipt_sha256", "validation_receipt_sha256"), ("receipt_path", "validation_receipt_path"),
                         ("reviewed_by", "reviewed_by"), ("decision", "decision"), ("validated_at", "validated_at")):  # fmt: skip
        if ultimo[campo] != obj.get(clave):
            problemas.append(
                f"historial: el último registro dice {campo}={ultimo[campo]!r} y el estado {obj.get(clave)!r}"
            )
    for r in registros:
        problemas += _eslabon_problemas(carpeta, r, obj)
    return problemas


def _eslabon_problemas(carpeta: Path, r: dict, obj: dict) -> list[str]:
    problemas: list[str] = []
    if (r["campaign_id"], r["computed_sha256"]) != (obj.get("campaign_id"), obj.get("computed_sha256")):
        problemas.append(f"historial r{r['from_revision']}: es de otra campaña u otro cómputo")
    nombre_recibo, nombre_estado, _ = _nombres(r["from_revision"])
    estado, recibo = carpeta / nombre_estado, carpeta / nombre_recibo
    if not estado.is_file() or hashlib.sha256(estado.read_bytes()).hexdigest() != r["from_state_sha256"]:
        problemas.append(f"historial r{r['from_revision']}: falta o cambió la copia del estado sustituido")
        return problemas
    if not recibo.is_file() or hashlib.sha256(recibo.read_bytes()).hexdigest() != r["previous_receipt_sha256"]:
        problemas.append(f"historial r{r['from_revision']}: falta o cambió la copia del recibo sustituido")
    try:
        previo = loads(estado.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [*problemas, f"historial r{r['from_revision']}: estado sustituido ilegible ({exc})"]
    if (previo.get("status"), previo.get("revision"), previo.get("validation_receipt_sha256")) != (
        "validated",
        r["from_revision"],
        r["previous_receipt_sha256"],
    ):
        problemas.append(f"historial r{r['from_revision']}: la copia no es el `validated` cuyo permiso se sustituyó")
    return problemas


def revalidate(
    path: str | Path,
    *,
    validation_receipt_sha256: str,
    validation_receipt_path: str,
    reviewed_by: str,
    validated_at: str,
    decision: str,
    expected_revision: int,
    expected_state_sha256: str,
) -> dict:
    """validated → validated con un permiso NUEVO. No es una transición genérica: `_ALLOWED` sigue sin admitirla.

    Bajo el lock de la campaña, con CAS sobre la revisión Y la huella de los bytes acreditados: re-acredita la evidencia
    del cómputo, rechaza un recibo reciclado o una ruta que pisaría el anterior, conserva create-only el recibo y el
    estado que se sustituyen, escribe el registro que los encadena y sólo entonces promociona el estado (revisión + 1).
    Un reintento tras una caída entre registro y promoción completa con el MISMO recibo (y la misma `validated_at`).
    """
    if not isinstance(validation_receipt_sha256, str) or not _HEX64.match(validation_receipt_sha256):
        raise ValueError("revalidate: validation_receipt_sha256 debe ser 64 hex")
    if not validation_receipt_path or not Path(validation_receipt_path).is_absolute():
        raise ValueError("revalidate: validation_receipt_path debe ser una ruta absoluta")
    if not reviewed_by or not reviewed_by.strip() or not decision or not decision.strip():
        raise ValueError("revalidate: reviewed_by y decision son obligatorios")
    if not _valid_ts(validated_at):
        raise ValueError("revalidate: validated_at invalido")
    p = Path(path)
    with _campaign_lock(p):
        datos = p.read_bytes()
        obj = loads(datos.decode("utf-8"))
        problemas = validate_schema(obj)
        if problemas:
            raise ValueError(f"campaign.json invalido antes de revalidar: {problemas}")
        if obj["status"] != PUBLISHABLE:
            raise ValueError(f"sólo se revalida una madre '{PUBLISHABLE}' (está en '{obj['status']}')")
        if obj["revision"] != expected_revision:
            raise ValueError(f"revision stale: esperada {expected_revision}, actual {obj['revision']}")
        sha_estado = hashlib.sha256(datos).hexdigest()
        if sha_estado != expected_state_sha256:
            raise ValueError("huella del estado distinta de la acreditada: alguien reescribió campaign.json")
        try:
            computado, _vivo = _ce.evidencia_computada(p)
        except _ce.EvidenciaError as exc:
            raise ValueError(f"la evidencia del cómputo no se acredita: {exc}") from exc
        if computado != obj.get("computed_sha256"):
            raise ValueError("la evidencia del cómputo no es la que el estado sella")
        if validation_receipt_sha256 == obj["validation_receipt_sha256"]:
            raise ValueError("el recibo nuevo recicla el permiso vigente (mismo sha256)")
        anterior = Path(obj["validation_receipt_path"])
        if Path(validation_receipt_path) == anterior:
            raise ValueError("el recibo nuevo pisaría la ruta del permiso que sustituye")
        try:
            bytes_anterior = anterior.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"el permiso anterior ya no se puede leer ({exc}): no se sustituye lo que no se conserva"
            ) from exc
        if hashlib.sha256(bytes_anterior).hexdigest() != obj["validation_receipt_sha256"]:
            raise ValueError("el permiso anterior cambió desde su validación: no se conserva lo que se va a sustituir")
        carpeta = p.parent / HISTORIAL
        actuales = historial_problemas(p, obj)
        carpeta.mkdir(exist_ok=True)
        nombre_recibo, nombre_estado, nombre_registro = _nombres(obj["revision"])
        registro_ruta = carpeta / nombre_registro
        preparado = _leer_registro(registro_ruta) if registro_ruta.exists() else None
        if actuales and not (preparado and all("preparada sin promocionar" in a for a in actuales)):
            raise ValueError(f"el historial de validaciones no se acredita: {actuales}")
        registro = {
            "schema": REVALIDATION_SCHEMA,
            "campaign_id": obj["campaign_id"],
            "computed_sha256": obj["computed_sha256"],
            "from_revision": obj["revision"],
            "to_revision": obj["revision"] + 1,
            "from_state_sha256": sha_estado,
            "previous_receipt_sha256": obj["validation_receipt_sha256"],
            "receipt_sha256": validation_receipt_sha256,
            "receipt_path": validation_receipt_path,
            "reviewed_by": reviewed_by,
            "decision": decision,
            "validated_at": validated_at,
        }
        if preparado is not None:
            iguales = {k: preparado[k] for k in _REGISTRO_KEYS - {"validated_at"}}
            if iguales != {k: registro[k] for k in _REGISTRO_KEYS - {"validated_at"}}:
                raise ValueError(
                    "hay una revalidación preparada sin promocionar con OTRO recibo: complétala con el mismo recibo"
                )
            registro["validated_at"] = preparado["validated_at"]  # el reintento completa la MISMA revalidación
        _crear_exacto(carpeta / nombre_recibo, bytes_anterior)
        _crear_exacto(carpeta / nombre_estado, datos)
        _crear_exacto(registro_ruta, _registro_bytes(registro))
        nuevo = {**obj, **{k: registro[k] for k in ("reviewed_by", "decision", "validated_at")},
                 "validation_receipt_sha256": validation_receipt_sha256,
                 "validation_receipt_path": validation_receipt_path, "revision": obj["revision"] + 1}  # fmt: skip
        problemas = validate_schema(nuevo)
        if problemas:
            raise ValueError(f"campaign.json DESTINO invalido: {problemas}")
        atomic_write(p, nuevo)
        return nuevo
