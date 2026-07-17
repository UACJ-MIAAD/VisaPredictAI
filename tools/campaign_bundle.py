#!/usr/bin/env python
"""Bundle INMUTABLE content-addressed + puntero CURRENT por CAS = la AUTORIDAD del commit del merge de campaña
(P0R.5 · B148/B145 · Incrementos 1R/1R2 · B155-B174). FUENTE ÚNICA: ningún consumidor implementa su propia
resolución — todos pasan por `open_current_bundle()` / `read_current_csv()`.

El problema raíz (B148): ocho ficheros CSV mutables + un recibo NO forman un commit atómico. La cura es mover la
AUTORIDAD a un bundle inmutable direccionado por contenido, apuntado por un ÚNICO puntero `CURRENT` actualizado por
CAS atómico: el commit cruza SÓLO cuando `CURRENT` apunta a un bundle válido.

Contrato CERRADO (B159/B168/B169/B171): manifiesto con claves exactas, `bool != int` estricto (sin coerción),
8 inputs `aq_pool_{nongbm,gbm}_{FAD,DFF}_{family,employment}.csv`, 4 outputs `campaign_pool_*` (campaign) + 4
`model_comparison_*` (eval) con NOMBRES esperados, sha256 64-hex, filas/columnas verificadas CONTRA EL CSV real
(header, nº real de columnas/filas, sin columnas duplicadas), procedencia oficial con git 40-hex + hashes 64-hex +
journal_heads acotado + python/plataforma/perfil/variante. `bundle_id = sha256(manifiesto canónico)`. La validación
exige inventario físico EXACTO e identidad gobernada de CADA fichero (regular, UID, nlink==1, sin escritura grupo/
otros, snapshot pre/post) y de CADA directorio (modo EXACTO 0700).

CURRENT (`.merge-CURRENT`, 0600, nlink==1) = `{schema_version, campaign_id, bundle_id, previous_bundle_id}` con
ESQUEMA CERRADO (B168). CAS (B165/B166/B167): la preparación mantiene VIVO el fd del bundle sellado; `commit_current`
RE-valida el bundle preparado (a través del fd, contra rebind) ANTES y DESPUÉS del CAS y NO confía en los atributos
entregados. El fd del puntero temporal y el del CURRENT anterior se mantienen ABIERTOS durante todo el CAS; tras el
`rename_exchange` se verifica SIMULTÁNEAMENTE que CURRENT liga al fd temporal nuevo y el desplazado al fd anterior.
Carrera → compensación por otro exchange con verificación física de ambos lados; si no se puede probar, se PRESERVA
todo y se eleva `BundleRollbackIncompleteError` (jamás éxito). Nunca se borra un objeto cuyo binding no coincida con
un fd de la transacción. Una autoridad previa inválida BLOQUEA el nuevo commit (B173), no se repara en silencio.

Todas las operaciones son fd-relativas y usan `tools.atomic_fs` (sin `os.replace`/`os.rename`). Este módulo NO
importa `merge_campaign_pools` (evita el ciclo); recibe bytes ya sellados/verificados del productor (que relee cada
output desde su fd CERTIFICADO con snapshot pre/post y revalida el digest antes de pasarlos: B158/B164).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import stat

from tools.atomic_fs import AtomicRenameError, AtomicUnsupportedError, rename_exchange, rename_noreplace
from tools.governed_fs import GovernedQuarantine, GovernedQuarantineError, GovernedRemovalError, OwnedLease
from tools.governed_read import read_governed_bytes, relative_name_problem

# B198: el contrato CSV se ANCLA por sha256 pineado (no una caché mutable). `_csv_columns()` RELEE el fichero y
# verifica su hash en CADA uso → mutar una caché en memoria no puede aflojarlo, y mutar el fichero rompe el hash. El
# mismo hash entra en la procedencia (→ manifiesto → bundle_id), ligando cada bundle a un contrato exacto.
_CSV_CONTRACT_SHA256 = "1784f9b080a852885625d502e9110e74992bba2f442419a7c9a516373936f67e"
_CSV_CONTRACT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "security", "campaign_bundle_contract.json")  # fmt: skip


def _csv_columns() -> tuple[str, ...]:
    with open(_CSV_CONTRACT_PATH, "rb") as fh:
        raw = fh.read()
    if hashlib.sha256(raw).hexdigest() != _CSV_CONTRACT_SHA256:
        raise BundleValidationError("el contrato CSV en disco no coincide con el sha256 pineado (B198)")
    contract = json.loads(raw)
    cols = contract.get("columns")
    if not (isinstance(cols, list) and cols and contract.get("encoding") == "utf-8"):
        raise BundleValidationError("contrato CSV inválido")
    return tuple(cols)  # inmutable: no se puede reescribir el resultado


_BUNDLES_DIR = ".merge-bundles"
_STAGING_PREFIX = ".merge-staging"
_CURRENT_NAME = ".merge-CURRENT"
_CURRENT_TMP_PREFIX = ".merge-CURRENT.tmp"
_MANIFEST = "manifest.json"
_OUTPUTS_DIR = "outputs"
_SCHEMA_VERSION = 1
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LABELS = ("campaign", "eval")

_EXPECTED_INPUTS = frozenset(
    f"aq_pool_{kind}_{table}_{block}.csv"
    for kind in ("nongbm", "gbm")
    for table in ("FAD", "DFF")
    for block in ("family", "employment")
)
_EXPECTED_OUTPUTS: dict[str, frozenset[str]] = {
    "campaign": frozenset(
        f"campaign_pool_{table}_{block}.csv" for table in ("FAD", "DFF") for block in ("family", "employment")
    ),
    "eval": frozenset(
        {
            "model_comparison_FAD21.csv",
            "model_comparison_EB_FAD21.csv",
            "model_comparison_DFF21.csv",
            "model_comparison_EB_DFF21.csv",
        }
    ),
}
_REQUIRED_PROVENANCE = frozenset(
    {
        "mode",
        "git_head",
        "git_tree",
        "git_dirty",
        "env_id",
        "code_sha_merge_campaign_pools",
        "code_sha_campaign_bundle",
        "code_sha_atomic_fs",
        "code_sha_governed_read",
        "code_sha_execution_contract",
        "csv_contract_sha256",
        "journal_heads",
        "python",
        "platform",
        "profile",
        "variant",
    }
)
_PROVENANCE_MODES = frozenset({"official", "legacy_import", "test"})
_PROV_MODULE_HASHES = (
    "code_sha_merge_campaign_pools",
    "code_sha_campaign_bundle",
    "code_sha_atomic_fs",
    "code_sha_governed_read",
)
_MANIFEST_KEYS = frozenset({"schema_version", "campaign_id", "txid", "inputs", "outputs", "provenance"})
_INPUT_KEYS = frozenset({"name", "size", "sha256"})
_OUTPUT_KEYS = frozenset({"label", "name", "rows", "cols", "sha256"})
_POINTER_KEYS = frozenset({"schema_version", "campaign_id", "bundle_id", "previous_bundle_id"})


class BundleError(Exception):
    """Base: fallo verificable del bundle o del puntero CURRENT."""


class BundleValidationError(BundleError):
    """Estructura/esquema/identidad/inventario/hash inválidos."""


class BundleConcurrencyError(BundleError):
    """Actualización concurrente de CURRENT detectada; NO se cruzó el commit (estado incompleto compensado)."""


class BundleRollbackIncompleteError(BundleError):
    """Un rollback/compensación no pudo restaurar el estado previo verificable — estado PRESERVADO, no éxito."""


class CommittedStateError(BundleError):
    """El CAS de CURRENT ya cruzó (autoridad válida y durable); un fallo posterior NO es un rollback."""


# --------------------------------------------------- helpers base ---------------------------------------------------


def _canon(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _no_dup_keys(pairs: list[tuple]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise BundleValidationError(f"clave JSON duplicada: {k!r}")
        out[k] = v
    return out


def _strict_loads(raw: bytes) -> object:
    """Parsea JSON rechazando claves duplicadas; CADA error de parseo se traduce a `BundleValidationError` (B168)."""
    try:
        return json.loads(raw, object_pairs_hook=_no_dup_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleValidationError(f"JSON malformado: {exc}") from exc


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)  # bool NO es int aceptable (True/False se rechazan)


def _is_pos_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def _is_hex(x: object, n: int) -> bool:
    return isinstance(x, str) and len(x) == n and all(c in "0123456789abcdef" for c in x)


def _is_hex64(x: object) -> bool:
    return _is_hex(x, 64)


def _require_relative(kind: str, name: object) -> None:
    if not isinstance(name, str):
        raise BundleValidationError(f"{kind} no-string: {name!r}")
    problem = relative_name_problem(name)
    if problem is not None:
        raise BundleValidationError(f"{kind} inseguro {name!r}: {problem}")


def _ident(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


def _snap(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink, st.st_mode)


def _write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    off = 0
    while off < len(mv):
        n = os.write(fd, mv[off:])
        if n <= 0:
            raise BundleError("escritura incompleta (os.write devolvió <= 0)")
        off += n


def _mkdir_governed(parent_fd: int, name: str) -> int:
    """Crea `name` 0700 bajo `parent_fd` (create-only) y lo abre exigiendo dir real/UID/modo EXACTO 0700."""
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o700)
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise BundleValidationError(f"dir {name!r} ajeno/no-dir/modo != 0700")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_dir(parent_fd: int, name: str, *, mode: int | None = None) -> int:
    """Abre `name` como directorio gobernado (real, UID actual). Con `mode` exige el modo EXACTO (B172)."""
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise BundleValidationError(f"dir {name!r} ajeno/no-dir")
        if mode is not None and stat.S_IMODE(st.st_mode) != mode:
            raise BundleValidationError(f"dir {name!r} modo {oct(stat.S_IMODE(st.st_mode))} != {oct(mode)}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _seal_file(dir_fd: int, name: str, data: bytes) -> str:
    """Sella `data` en `name` 0600 (create-only, O_EXCL|O_NOFOLLOW) con fsync; devuelve su sha256."""
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(data).hexdigest()


def _read_sealed(dir_fd: int, name: str) -> bytes:
    """Lee `name` con identidad gobernada COMPLETA (regular, UID actual, nlink==1, sin escritura grupo/otros,
    snapshot fstat pre/post) vía `read_governed_bytes`. Cualquier problema eleva `BundleValidationError`."""
    data, problem = read_governed_bytes(dir_fd, name)
    if problem is not None or data is None:
        raise BundleValidationError(f"fichero sellado {name!r} no gobernado: {problem}")
    return data


def _listdir_exact(dir_fd: int, expected: set[str], where: str) -> None:
    """Inventario físico EXACTO: `os.listdir(dir_fd)` == `expected` (ni ficheros de más ni de menos). B160."""
    actual = set(os.listdir(dir_fd))
    if actual != expected:
        extra = actual - expected
        missing = expected - actual
        raise BundleValidationError(f"inventario de {where} no exacto: extra={sorted(extra)} falta={sorted(missing)}")


def _verify_csv(data: bytes, rows: int, cols: int) -> None:
    """B169/B186: relee el CSV sellado y verifica el header EXACTO contra el contrato (mismos nombres, mismo orden,
    sin renombrar/reordenar/faltar/sobrar/duplicar), UTF-8 SIN BOM, `cols` = nº real de columnas del contrato, y el
    nº REAL de filas (todas del mismo ancho) — cierra metadatos falsos y headers arbitrarios (a,b)."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise BundleValidationError("CSV sellado con BOM no autorizado")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundleValidationError(f"CSV sellado no es UTF-8: {exc}") from exc
    columns = _csv_columns()
    if cols != len(columns):
        raise BundleValidationError(f"cols del manifiesto {cols} != contrato {len(columns)}")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise BundleValidationError("CSV sellado vacío (sin header)") from None
    if tuple(header) != columns:
        raise BundleValidationError(f"header CSV != contrato (nombres/orden): {header}")
    n = 0
    for row in reader:
        if len(row) != len(columns):
            raise BundleValidationError(f"fila CSV con {len(row)} columnas != {len(columns)}")
        n += 1
    if n != rows:
        raise BundleValidationError(f"CSV con {n} filas reales != manifiesto {rows}")


# --------------------------------------------------- esquemas cerrados ---------------------------------------------------


def _validate_manifest(manifest: object) -> dict:
    """Contrato CERRADO del manifiesto (B159/B169/B171). No toca disco. Eleva `BundleValidationError`."""
    if not isinstance(manifest, dict) or set(manifest.keys()) != _MANIFEST_KEYS:
        raise BundleValidationError(f"claves del manifiesto != {sorted(_MANIFEST_KEYS)}")
    if not (_is_int(manifest["schema_version"]) and manifest["schema_version"] == _SCHEMA_VERSION):
        raise BundleValidationError(f"schema_version inválido: {manifest['schema_version']!r}")
    _validate_campaign_id(manifest["campaign_id"])
    if not (isinstance(manifest["txid"], str) and manifest["txid"]):
        raise BundleValidationError("txid vacío/no-str")
    inputs = manifest["inputs"]
    if not isinstance(inputs, list) or len(inputs) != len(_EXPECTED_INPUTS):
        raise BundleValidationError(f"inputs debe tener exactamente {len(_EXPECTED_INPUTS)} entradas")
    seen_in: set[str] = set()
    for e in inputs:
        if not isinstance(e, dict) or set(e.keys()) != _INPUT_KEYS:
            raise BundleValidationError(f"input con claves != {sorted(_INPUT_KEYS)}: {e!r}")
        _require_relative("input", e["name"])
        if e["name"] not in _EXPECTED_INPUTS or e["name"] in seen_in:
            raise BundleValidationError(f"input inesperado/duplicado: {e['name']!r}")
        seen_in.add(e["name"])
        if not _is_pos_int(e["size"]) or not _is_hex64(e["sha256"]):
            raise BundleValidationError(f"size/sha256 de input inválido: {e!r}")
    if seen_in != set(_EXPECTED_INPUTS):
        raise BundleValidationError("conjunto de inputs != esperado")
    outputs = manifest["outputs"]
    expected_n = sum(len(v) for v in _EXPECTED_OUTPUTS.values())
    if not isinstance(outputs, list) or len(outputs) != expected_n:
        raise BundleValidationError(f"outputs debe tener exactamente {expected_n} entradas")
    seen_out: dict[str, set[str]] = {lab: set() for lab in _LABELS}
    for e in outputs:
        if not isinstance(e, dict) or set(e.keys()) != _OUTPUT_KEYS:
            raise BundleValidationError(f"output con claves != {sorted(_OUTPUT_KEYS)}: {e!r}")
        lab = e["label"]
        if lab not in _LABELS:
            raise BundleValidationError(f"label inválido: {lab!r}")
        _require_relative("output", e["name"])
        if e["name"] not in _EXPECTED_OUTPUTS[lab] or e["name"] in seen_out[lab]:
            raise BundleValidationError(f"output inesperado/duplicado {lab}/{e['name']!r}")
        seen_out[lab].add(e["name"])
        if not _is_pos_int(e["rows"]) or not _is_pos_int(e["cols"]):  # B169: bool/str rechazados, sin coerción
            raise BundleValidationError(f"rows/cols inválidos (no entero positivo) en {lab}/{e['name']}")
        if not _is_hex64(e["sha256"]):
            raise BundleValidationError(f"sha256 de output inválido: {e['sha256']!r}")
    for lab in _LABELS:
        if seen_out[lab] != set(_EXPECTED_OUTPUTS[lab]):
            raise BundleValidationError(f"conjunto de outputs {lab} != esperado")
    _validate_provenance(manifest["provenance"])
    return manifest


def _validate_campaign_id(cid: object) -> None:
    if cid is None:
        return
    if not isinstance(cid, str) or not cid.strip():
        raise BundleValidationError(f"campaign_id vacío/whitespace/no-str: {cid!r}")


def _validate_provenance(prov: object) -> None:
    """B171: procedencia oficial con esquema CERRADO — git 40-hex|None, hashes 64-hex, contrato 64-hex|None,
    journal_heads acotado (claves en labels, valores 64-hex|None), python/plataforma no vacíos, perfil/variante
    str|None. Sin comodines ('x' como git, hashes nulos, journal arbitrario)."""
    if not isinstance(prov, dict) or set(prov.keys()) != _REQUIRED_PROVENANCE:
        raise BundleValidationError(f"provenance con claves != {sorted(_REQUIRED_PROVENANCE)}")
    if prov["mode"] not in _PROVENANCE_MODES:  # B187: modo explícito (official exige git no-nulo)
        raise BundleValidationError(f"provenance.mode inválido: {prov['mode']!r}")
    if prov["mode"] == "official" and not _is_hex(prov["git_head"], 40):
        raise BundleValidationError("provenance oficial exige git_head 40-hex no nulo")
    if prov["git_head"] is not None and not _is_hex(prov["git_head"], 40):
        raise BundleValidationError(f"git_head no es sha git 40-hex|None: {prov['git_head']!r}")
    if prov["git_tree"] is not None and not _is_hex(prov["git_tree"], 40):
        raise BundleValidationError(f"git_tree no es sha git 40-hex|None: {prov['git_tree']!r}")
    if prov["git_dirty"] is not None and not isinstance(prov["git_dirty"], bool):
        raise BundleValidationError("git_dirty no es bool|None")
    if prov["env_id"] is not None and not _is_hex64(prov["env_id"]):
        raise BundleValidationError("env_id no es sha256|None")
    for k in _PROV_MODULE_HASHES:
        if not _is_hex64(prov[k]):
            raise BundleValidationError(f"{k} no es sha256: {prov[k]!r}")
    if prov["code_sha_execution_contract"] is not None and not _is_hex64(prov["code_sha_execution_contract"]):
        raise BundleValidationError("code_sha_execution_contract no sha256|None")
    if not _is_hex64(prov["csv_contract_sha256"]) or prov["csv_contract_sha256"] != _CSV_CONTRACT_SHA256:  # B198
        raise BundleValidationError("csv_contract_sha256 no liga al contrato CSV pineado")
    heads = prov["journal_heads"]
    if not isinstance(heads, dict):
        raise BundleValidationError("journal_heads no es dict")
    for lab, val in heads.items():
        if lab not in _LABELS or (val is not None and not _is_hex64(val)):
            raise BundleValidationError(f"journal_heads inválido: {lab!r} -> {val!r}")
    for k in ("python", "platform"):
        if not (isinstance(prov[k], str) and prov[k].strip()):
            raise BundleValidationError(f"provenance.{k} vacío/no-str")
    for k in ("profile", "variant"):
        if prov[k] is not None and not isinstance(prov[k], str):
            raise BundleValidationError(f"provenance.{k} no str|None")
    if prov["mode"] == "official":  # B199: 'official' exige los marcadores del run GOBERNADO, no solo git
        if not _is_hex64(prov["env_id"]):
            raise BundleValidationError("official exige env_id 64-hex (run gobernado)")
        if not _is_hex(prov["git_tree"], 40) or prov["git_dirty"] is not False:
            raise BundleValidationError("official exige git_tree 40-hex y git_dirty=false")
        if not (isinstance(prov["profile"], str) and prov["profile"].strip()):
            raise BundleValidationError("official exige profile no vacío")


def _validate_pointer(obj: object) -> dict:
    """B168: esquema CERRADO del puntero CURRENT."""
    if not isinstance(obj, dict) or set(obj.keys()) != _POINTER_KEYS:
        raise BundleValidationError(f"pointer con claves != {sorted(_POINTER_KEYS)}")
    if not (_is_int(obj["schema_version"]) and obj["schema_version"] == _SCHEMA_VERSION):
        raise BundleValidationError(f"schema_version inválido en pointer: {obj['schema_version']!r}")
    _validate_campaign_id(obj["campaign_id"])
    if not _is_hex64(obj["bundle_id"]):
        raise BundleValidationError(f"bundle_id inválido en pointer: {obj['bundle_id']!r}")
    if obj["previous_bundle_id"] is not None and not _is_hex64(obj["previous_bundle_id"]):
        raise BundleValidationError(f"previous_bundle_id inválido: {obj['previous_bundle_id']!r}")
    return obj


def _manifest_for(campaign_id: str | None, txid: str, inputs: list[dict], outputs: list[dict], provenance: dict) -> dict:  # fmt: skip
    return {
        "schema_version": _SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "txid": txid,
        "inputs": sorted(inputs, key=lambda d: d["name"]),
        "outputs": sorted(outputs, key=lambda d: (d["label"], d["name"])),
        "provenance": provenance,
    }


# --------------------------------------------------- validación de bundle ---------------------------------------------------


def _validate_bundle_at(bfd: int, bundle_id: str) -> dict:
    """Valida un bundle COMPLETO a través de un fd YA ABIERTO del directorio del bundle (inmune a rebind del
    nombre): esquema cerrado + `bundle_id == sha256(manifest)` + inventario físico EXACTO + modo 0700 de cada
    subdirectorio + identidad gobernada, sha256 y estructura CSV de cada output sellado."""
    manifest = _validate_manifest(_strict_loads(_read_sealed(bfd, _MANIFEST)))
    if hashlib.sha256(_canon(manifest)).hexdigest() != bundle_id:
        raise BundleValidationError(f"bundle_id {bundle_id} != sha256(manifest)")
    _listdir_exact(bfd, {_MANIFEST, _OUTPUTS_DIR}, f"bundle {bundle_id}")
    outs_root = _open_dir(bfd, _OUTPUTS_DIR, mode=0o700)
    try:
        _listdir_exact(outs_root, set(_LABELS), "outputs/")
        for lab in _LABELS:
            lfd = _open_dir(outs_root, lab, mode=0o700)
            try:
                entries = [e for e in manifest["outputs"] if e["label"] == lab]
                _listdir_exact(lfd, {e["name"] for e in entries}, f"outputs/{lab}")
                for o in entries:
                    data = _read_sealed(lfd, o["name"])
                    if hashlib.sha256(data).hexdigest() != o["sha256"]:
                        raise BundleValidationError(f"output {lab}/{o['name']} no coincide con su sha256")
                    _verify_csv(data, o["rows"], o["cols"])
            finally:
                os.close(lfd)
    finally:
        os.close(outs_root)
    return manifest


def validate_bundle(bundles_root_fd: int, bundle_id: str) -> dict:
    """Valida un bundle COMPLETO abriendo su directorio 0700 y delegando en `_validate_bundle_at`."""
    if not _is_hex64(bundle_id):
        raise BundleValidationError(f"bundle_id no es sha256: {bundle_id!r}")
    bfd = _open_dir(bundles_root_fd, bundle_id, mode=0o700)
    try:
        return _validate_bundle_at(bfd, bundle_id)
    finally:
        os.close(bfd)


# --------------------------------------------------- limpieza fail-closed ---------------------------------------------------


def _cleanup_staging(camp_fd: int, staging_name: str, staging_fd: int) -> None:
    """B170/B180/B205: si el staging SOBREVIVE (colisión/error; en éxito el rename lo consumió), lo RETIRA por
    cuarentena SOURCE-CAS ligada a su fd VIVO. B205: SÓLO `FileNotFoundError` cuenta como ausente; cualquier otra
    condición (symlink, permisos, tipo) llega a la cuarentena, cuyo source-CAS verifica ANTES de retirar y restaura un
    objeto ajeno a su ruta oficial."""
    if staging_fd < 0:
        return  # nunca se creó
    try:
        os.stat(staging_name, dir_fd=camp_fd, follow_symlinks=False)  # ¿hay algo en el nombre? (incluye symlink)
    except FileNotFoundError:
        return  # B205: SÓLO ausente si de verdad no existe (el rename lo consumió)
    lease = OwnedLease(staging_fd, is_dir=True)  # ligado al inode vivo del staging
    quar = GovernedQuarantine(camp_fd, secrets.token_hex(12))
    try:
        quar.quarantine(camp_fd, staging_name, lease)  # source-CAS: verifica antes de retirar (B207)
    except (GovernedRemovalError, GovernedQuarantineError) as exc:
        raise BundleRollbackIncompleteError(str(exc)) from exc
    finally:
        for e in quar.close():  # B193/B204: los errores de cierre no se descartan
            raise BundleRollbackIncompleteError(f"cierre de cuarentena de staging falló: {e}")


# ------------------------------------------- preparar / validar / commit -------------------------------------------


class _PreparedBundle:
    """Bundle inmutable YA promovido a `.merge-bundles/<bundle_id>/`, con el fd del directorio del bundle VIVO
    (B165): `commit_current` re-valida A TRAVÉS de este fd (inmune a rebind) y NO confía en atributos MUTABLES.
    `campaign_id` es una PROPIEDAD derivada del manifiesto validado (no un atributo mutable separado, B175); el
    handle es de USO ÚNICO. CURRENT aún NO tocado. Context manager: cierra el fd al salir."""

    __slots__ = ("camp_fd", "bundle_id", "manifest", "_bundle_fd", "_ident", "_used")
    camp_fd: int
    bundle_id: str
    manifest: dict
    _bundle_fd: int
    _ident: tuple[int, int]
    _used: bool

    def __init__(self, camp_fd: int, bundle_id: str, campaign_id: str | None, manifest: dict) -> None:
        if manifest.get("campaign_id") != campaign_id:  # B175: el manifiesto es la ÚNICA fuente de campaign_id
            raise BundleValidationError("campaign_id del handle diverge del manifiesto")
        self.camp_fd = camp_fd
        self.bundle_id = bundle_id
        self.manifest = manifest
        self._used = False
        broot = _open_dir(camp_fd, _BUNDLES_DIR, mode=0o700)
        try:
            try:
                self._bundle_fd = _open_dir(broot, bundle_id, mode=0o700)
            except FileNotFoundError as exc:  # B165: un bundle_id fabricado/inexistente se rechaza gobernado
                raise BundleValidationError(f"bundle {bundle_id!r} inexistente (prepared fabricado)") from exc
        finally:
            os.close(broot)
        self._ident = _ident(os.fstat(self._bundle_fd))

    @property
    def campaign_id(self) -> str | None:  # B175: read-only, derivado del manifiesto — no se puede mutar el puntero
        return self.manifest["campaign_id"]

    def _consume(self) -> None:
        if self._used:  # B175: handle de uso único (segunda llamada a commit_current se rechaza)
            raise BundleValidationError("_PreparedBundle ya consumido (uso único)")
        if self._bundle_fd < 0:
            raise BundleValidationError("_PreparedBundle cerrado")
        self._used = True

    def revalidate(self) -> None:
        """Re-valida el bundle COMPLETO a través del fd vivo y comprueba que el NOMBRE siga ligado al MISMO inode
        (rebind desde `prepare` → B165). Rechaza `_Prepared` fabricados o bundles alterados."""
        broot = _open_dir(self.camp_fd, _BUNDLES_DIR, mode=0o700)
        try:
            namefd = _open_dir(broot, self.bundle_id, mode=0o700)
            try:
                if _ident(os.fstat(namefd)) != self._ident:
                    raise BundleValidationError("el nombre del bundle fue re-ligado a otro inode desde prepare (B165)")
            finally:
                os.close(namefd)
        finally:
            os.close(broot)
        if _ident(os.fstat(self._bundle_fd)) != self._ident:
            raise BundleValidationError("el fd del bundle preparado cambió de identidad (B165)")
        got = _validate_bundle_at(self._bundle_fd, self.bundle_id)
        if _canon(got) != _canon(self.manifest):
            raise BundleValidationError("el manifiesto del bundle cambió desde prepare (B165)")

    def close(self) -> None:
        fd = getattr(self, "_bundle_fd", -1)
        if fd is not None and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        self._bundle_fd = -1

    def __enter__(self) -> _PreparedBundle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def prepare_bundle(
    camp_fd: int,
    txid: str,
    campaign_id: str | None,
    outputs: list[dict],
    inputs: list[dict],
    provenance: dict,
) -> _PreparedBundle:
    """Construye y promueve el bundle inmutable content-addressed SIN tocar CURRENT. `outputs` =
    [{label,name,bytes,rows,cols}] (bytes YA certificados por el productor), `inputs` = [{name,bytes}] (bytes
    reales, para tamaño/hash — nunca reconstruidos: B164). Valida el manifiesto CERRADO y verifica filas/columnas
    CONTRA el CSV real (B169) antes de sellar; promueve inmutable (colisión → validación COMPLETA del preexistente,
    B161); limpieza de staging FAIL-CLOSED en CADA salida (B170). Devuelve un `_PreparedBundle` con el fd vivo."""
    if not isinstance(txid, str) or not txid:
        raise BundleValidationError("txid vacío")
    in_meta = []
    sealed: dict[tuple[str, str], bytes] = {}
    for i in inputs:
        _require_relative("input", i["name"])
        b = i["bytes"]
        if not isinstance(b, (bytes, bytearray)):
            raise BundleValidationError(f"bytes de input {i['name']!r} no son bytes")
        in_meta.append({"name": i["name"], "size": len(b), "sha256": hashlib.sha256(b).hexdigest()})
    out_meta = []
    for o in outputs:
        if o["label"] not in _LABELS:
            raise BundleValidationError(f"label inválido: {o['label']!r}")
        _require_relative("output", o["name"])
        b = o["bytes"]
        if not isinstance(b, (bytes, bytearray)):
            raise BundleValidationError(f"bytes de output {o['name']!r} no son bytes")
        if not _is_pos_int(o["rows"]) or not _is_pos_int(o["cols"]):  # B169: sin int(); bool/str rechazados
            raise BundleValidationError(f"rows/cols de {o['label']}/{o['name']} no son enteros positivos")
        frozen = bytes(b)  # B175: COPIA inmutable de los bytes del llamador (no retener bytearray mutable)
        _verify_csv(frozen, o["rows"], o["cols"])  # B169/B186: header exacto + filas reales
        out_meta.append({"label": o["label"], "name": o["name"], "rows": o["rows"], "cols": o["cols"], "sha256": hashlib.sha256(frozen).hexdigest()})  # fmt: skip
        sealed[(o["label"], o["name"])] = frozen
    manifest = _validate_manifest(_manifest_for(campaign_id, txid, in_meta, out_meta, provenance))
    bundle_id = hashlib.sha256(_canon(manifest)).hexdigest()

    staging_name = f"{_STAGING_PREFIX}.{secrets.token_hex(12)}"
    committed_or_collided = False
    sroot = -1  # B180/Fase4: fd VIVO del dir de staging, retenido desde la creación hasta promoción/cuarentena
    try:
        sroot = _mkdir_governed(camp_fd, staging_name)
        outs_root = _mkdir_governed(sroot, _OUTPUTS_DIR)
        try:
            for lab in _LABELS:
                lfd = _mkdir_governed(outs_root, lab)
                try:
                    for name in sorted(_EXPECTED_OUTPUTS[lab]):
                        _seal_file(lfd, name, sealed[(lab, name)])
                    os.fsync(lfd)
                finally:
                    os.close(lfd)
            os.fsync(outs_root)
        finally:
            os.close(outs_root)
        _seal_file(sroot, _MANIFEST, _canon(manifest))
        os.fsync(sroot)
        _promote_staging(camp_fd, staging_name, bundle_id, manifest)
        committed_or_collided = True
    finally:
        try:  # B170/B180/B185: limpieza fail-closed vía cuarentena move-only, ligada al fd vivo del staging
            _cleanup_staging(camp_fd, staging_name, sroot)
        except (GovernedRemovalError, GovernedQuarantineError, BundleError, OSError) as exc:
            if committed_or_collided:
                raise CommittedStateError(f"bundle promovido pero el staging {staging_name} no se limpió: {exc}") from exc  # fmt: skip
            raise BundleRollbackIncompleteError(f"no se pudo limpiar el staging {staging_name}: {exc}") from exc
        finally:
            if sroot >= 0:
                os.close(sroot)
    prepared = _PreparedBundle(camp_fd, bundle_id, campaign_id, manifest)
    try:
        prepared.revalidate()
    except BaseException:
        prepared.close()
        raise
    return prepared


def validate_prepared_bundle(prepared: _PreparedBundle) -> None:
    prepared.revalidate()


def _promote_staging(camp_fd: int, staging_name: str, bundle_id: str, manifest: dict) -> None:
    """Promueve staging → `.merge-bundles/<bundle_id>/` con un `rename_noreplace`. Colisión → valida el bundle
    preexistente COMPLETO (B161) e iguala el manifiesto; si difiere, BLOQUEA (el staging se limpia en `finally`)."""
    created = False
    try:
        os.mkdir(_BUNDLES_DIR, 0o700, dir_fd=camp_fd)
        created = True
    except FileExistsError:
        pass
    if created:  # recién creado: forzar 0700 exacto (umask-independiente) SÓLO en la creación
        fd = _open_dir(camp_fd, _BUNDLES_DIR)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    broot = _open_dir(camp_fd, _BUNDLES_DIR, mode=0o700)  # B188: una raíz preexistente en 0777 BLOQUEA (no se repara)
    try:
        try:
            rename_noreplace(camp_fd, staging_name, broot, bundle_id)
        except FileExistsError:
            existing = validate_bundle(broot, bundle_id)  # B161: valida el preexistente COMPLETO, no solo manifest
            if _canon(existing) != _canon(manifest):
                raise BundleValidationError(f"bundle {bundle_id} preexistente difiere (colisión de id)") from None
            return
        except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
            raise BundleError(f"no se pudo promover el bundle: {exc}") from exc
        bfd = _open_dir(broot, bundle_id, mode=0o700)
        try:
            os.fsync(bfd)
        finally:
            os.close(bfd)
        os.fsync(broot)
    finally:
        os.close(broot)


# --------------------------------------------------- CAS de CURRENT ---------------------------------------------------


def _open_pointer_governed(dir_fd: int, name: str) -> tuple[int, bytes, dict, tuple[int, int]]:
    """Abre un puntero con identidad gobernada COMPLETA (regular, UID, nlink==1, modo EXACTO 0600, snapshot pre/
    post) + esquema CERRADO. Devuelve `(fd, raw, pointer, (dev,ino))`; el llamador es dueño del fd. Propaga
    `FileNotFoundError` si ausente; cualquier otro problema → `BundleValidationError`."""
    _require_relative("pointer", name)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600:  # fmt: skip
            raise BundleValidationError("pointer no-regular/ajeno/hardlink/modo != 0600")
        raw = b""
        while chunk := os.read(fd, 1 << 16):
            raw += chunk
        if _snap(os.fstat(fd)) != _snap(st):
            raise BundleValidationError("pointer mutado durante la lectura")
        pointer = _validate_pointer(_strict_loads(raw))
    except BaseException:
        os.close(fd)
        raise
    return fd, raw, pointer, _ident(st)


def _read_current(camp_fd: int) -> tuple[dict, bytes, tuple[int, int]] | None:
    """Lee CURRENT gobernado (esquema cerrado). Devuelve `(pointer, raw, (dev,ino))` o None si no existe."""
    try:
        fd, raw, pointer, ptr_ident = _open_pointer_governed(camp_fd, _CURRENT_NAME)
    except FileNotFoundError:
        return None
    os.close(fd)
    return pointer, raw, ptr_ident


def _name_ident(dir_fd: int, name: str) -> tuple[int, int] | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        return _ident(os.fstat(fd))
    finally:
        os.close(fd)


def _capture(dir_fd: int, name: str) -> tuple[tuple[int, int], bytes] | None:
    """Captura `(ident, bytes)` de `name` tal cual está (sin gobernanza) — para poder RESTAURAR verbatim el valor
    concurrente que un exchange desplazó. Devuelve None si el objeto no existe."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        ident = _ident(os.fstat(fd))
        data = b""
        while chunk := os.read(fd, 1 << 16):
            data += chunk
        return ident, data
    finally:
        os.close(fd)


def _pointer_matches(dir_fd: int, name: str, ident: tuple[int, int], content: bytes) -> bool:
    """B166: `name` liga EXACTAMENTE al inode `ident` Y su contenido son los bytes `content`. Cierra tanto el swap
    de inode como la sustitución de contenido en sitio (O_TRUNC sobre el mismo inode)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        if _ident(os.fstat(fd)) != ident:
            return False
        data = b""
        while chunk := os.read(fd, 1 << 16):
            data += chunk
        return data == content
    finally:
        os.close(fd)


class CommitCertificate:
    """Prueba estructurada del commit del bundle (B189): CURRENT y el bundle se certificaron como UNA unidad. NO se
    devuelve un simple string."""

    __slots__ = ("bundle_id", "pointer_digest", "pointer_inode", "previous_bundle_id")

    def __init__(self, bundle_id: str, pointer_digest: str, pointer_inode: tuple[int, int], previous_bundle_id: str | None) -> None:  # fmt: skip
        self.bundle_id = bundle_id
        self.pointer_digest = pointer_digest
        self.pointer_inode = pointer_inode
        self.previous_bundle_id = previous_bundle_id


def _quarantine_pointer(quar: GovernedQuarantine, camp_fd: int, name: str, fd: int, content: bytes) -> None:
    """MOVE-ONLY: mueve el puntero `name` (que la tx posee: `fd` vivo + `content` conocido) a la cuarantena y lo
    PRESERVA. Un objeto ajeno/mutado se preserva como FOREIGN y eleva incompleto. Nunca borra (B191/B192)."""
    lease = OwnedLease(fd, is_dir=False, known_digest=hashlib.sha256(content).hexdigest())
    try:
        quar.quarantine(camp_fd, name, lease)
    except (GovernedRemovalError, GovernedQuarantineError) as exc:  # B204: clasifica también errores de cuarentena
        raise BundleRollbackIncompleteError(str(exc)) from exc


def _validate_authority(camp_fd: int, bundle_id: str) -> None:
    broot = _open_dir(camp_fd, _BUNDLES_DIR, mode=0o700)
    try:
        validate_bundle(broot, bundle_id)
    except FileNotFoundError as exc:  # una autoridad que apunta a un bundle inexistente es inválida (B183)
        raise BundleValidationError(f"la autoridad apunta a un bundle inexistente {bundle_id}") from exc
    finally:
        os.close(broot)


def _fsync_typed(camp_fd: int) -> None:
    """B182: un fsync fallido DESPUÉS del CAS es un fallo post-commit tipado, no un OSError crudo escapando."""
    try:
        os.fsync(camp_fd)
    except OSError as exc:
        raise CommittedStateError(f"fsync post-CAS de durabilidad falló: {exc}") from exc


def _certify_current(camp_fd: int, prepared: _PreparedBundle, pbytes: bytes, tmp_ident: tuple[int, int], prev_id: str | None) -> CommitCertificate:  # fmt: skip
    """Certifica CURRENT + bundle como UNA unidad (B189): CURRENT liga EXACTAMENTE a mi puntero (gobernado nlink/modo/
    esquema + identidad + contenido), el bundle es válido a través del fd vivo, y CURRENT SIGUE ligado a mi puntero
    TRAS validar (cierra el swap-durante-certificación). Devuelve un `CommitCertificate`."""
    fd, raw, pointer, ident = _open_pointer_governed(camp_fd, _CURRENT_NAME)  # B177: nlink==1/modo/esquema
    try:
        if raw != pbytes or pointer["bundle_id"] != prepared.bundle_id or ident != tmp_ident:
            raise BundleValidationError("CURRENT no es mi puntero certificable")
    finally:
        os.close(fd)
    prepared.revalidate()  # B178: el bundle apuntado es válido y su nombre no fue re-ligado
    if prev_id is not None:  # B197: la autoridad previa referenciada sigue siendo válida en la linealización
        _validate_authority(camp_fd, prev_id)
    if not _pointer_matches(camp_fd, _CURRENT_NAME, tmp_ident, pbytes):  # B189: CURRENT no cambió durante la validación
        raise BundleValidationError("CURRENT fue cambiado durante la certificación (B189)")
    return CommitCertificate(prepared.bundle_id, hashlib.sha256(pbytes).hexdigest(), tmp_ident, prev_id)


def commit_current(prepared: _PreparedBundle) -> CommitCertificate:
    """PUNTO DE COMMIT del bundle (uso ÚNICO). `campaign_id` sale del manifiesto validado (B175). Bajo una cuarentena
    MOVE-ONLY: certifica CURRENT+bundle como una unidad (B189), compensa ante CUALQUIER Exception antes de certificar
    (B190), y CADA temporal creado se retira por cuarentena move-only en toda salida (B195/B196). Devuelve un
    `CommitCertificate`."""
    camp_fd = prepared.camp_fd
    prepared._consume()  # B175: handle de uso único
    prepared.revalidate()  # B165: re-validar el bundle a través del fd vivo antes de nada

    # B204: gestión MANUAL de la cuarentena para clasificar sus errores de cierre por si el commit YA cruzó.
    quar = GovernedQuarantine(camp_fd, secrets.token_hex(12))
    cert: CommitCertificate | None = None
    primary: BaseException | None = None
    try:
        cert = _run_commit(camp_fd, quar, prepared)
    except BaseException as exc:  # noqa: BLE001 — se re-eleva tras adjuntar los errores de cierre (B204)
        primary = exc
    close_errs = quar.close()  # B193/B204: los errores de cierre NUNCA se descartan
    if primary is not None:
        if close_errs:
            primary.add_note("errores de cierre de cuarentena: " + "; ".join(close_errs))
        raise primary
    if close_errs:  # sin excepción primaria: clasificar según si el commit cruzó (B204)
        joined = "; ".join(close_errs)
        if cert is not None:
            raise CommittedStateError(f"commit certificado pero el cierre de cuarentena falló: {joined}")
        raise BundleRollbackIncompleteError(f"cierre de cuarentena falló: {joined}")
    assert cert is not None  # el commit cruzó sin excepción → hay certificado
    return cert


def _run_commit(camp_fd: int, quar: GovernedQuarantine, prepared: _PreparedBundle) -> CommitCertificate:
    try:
        prev_fd, prev_raw, prev_pointer, prev_ident = _open_pointer_governed(camp_fd, _CURRENT_NAME)
    except FileNotFoundError:
        prev_fd, prev_raw, prev_pointer, prev_ident = -1, None, None, None
    try:
        prev_id = prev_pointer["bundle_id"] if prev_pointer is not None else None
        if prev_pointer is not None:  # B173: autoridad previa inválida BLOQUEA
            _validate_authority(camp_fd, prev_pointer["bundle_id"])
        pointer = {"schema_version": _SCHEMA_VERSION, "campaign_id": prepared.manifest["campaign_id"], "bundle_id": prepared.bundle_id, "previous_bundle_id": prev_id}  # fmt: skip
        pbytes = _canon(pointer)
        tmp_name = f"{_CURRENT_TMP_PREFIX}.{secrets.token_hex(12)}"
        tmp_fd = os.open(tmp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=camp_fd)
        try:
            _write_all(tmp_fd, pbytes)
            os.fsync(tmp_fd)
            tmp_ident = _ident(os.fstat(tmp_fd))
            if os.fstat(tmp_fd).st_nlink != 1:  # B177/B195: hardlink del temporal → se retira por cuarentena
                _quarantine_pointer(quar, camp_fd, tmp_name, tmp_fd, pbytes)
                raise BundleValidationError("puntero temporal con nlink != 1")
            if not _pointer_matches(camp_fd, tmp_name, tmp_ident, pbytes):  # B166: identidad Y contenido
                _quarantine_pointer(quar, camp_fd, tmp_name, tmp_fd, pbytes)
                raise BundleValidationError("el puntero temporal no liga a su fd/contenido tras escribir")
            return _cas_pointer(camp_fd, quar, prepared, tmp_name, tmp_fd, tmp_ident, pbytes, prev_fd, prev_ident, prev_raw, prev_pointer)  # fmt: skip
        finally:
            os.close(tmp_fd)
    finally:
        if prev_fd >= 0:
            os.close(prev_fd)


def _cas_pointer(camp_fd: int, quar: GovernedQuarantine, prepared: _PreparedBundle, tmp_name: str, tmp_fd: int, tmp_ident: tuple[int, int], pbytes: bytes, prev_fd: int, prev_ident: tuple[int, int] | None, prev_raw: bytes | None, prev_pointer: dict | None) -> CommitCertificate:  # fmt: skip
    """CAS con estado explícito. Tras el rename se CERTIFICA (identidad+contenido+nlink+bundle+previo) como una unidad
    ANTES de declarar el commit; CUALQUIER Exception (no solo BundleError, B190) → compensa. Los temporales se retiran
    por cuarentena MOVE-ONLY en toda salida (B195/B196). Devuelve un `CommitCertificate`."""
    prev_id = prev_pointer["bundle_id"] if prev_pointer is not None else None
    if prev_pointer is None:
        try:
            rename_noreplace(camp_fd, tmp_name, camp_fd, _CURRENT_NAME)
        except FileExistsError as exc:
            _quarantine_pointer(quar, camp_fd, tmp_name, tmp_fd, pbytes)  # B181/B196: retira mi temporal
            raise BundleConcurrencyError(f"CURRENT apareció durante el CAS inicial: {exc}") from exc
        except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
            _quarantine_pointer(quar, camp_fd, tmp_name, tmp_fd, pbytes)
            raise BundleError(f"no se pudo hacer CAS inicial de CURRENT: {exc}") from exc
        try:
            cert = _certify_current(camp_fd, prepared, pbytes, tmp_ident, None)
        except Exception:  # noqa: BLE001 — B190: compensa CUALQUIER excepción (no BaseException) antes de certificar
            _quarantine_pointer(quar, camp_fd, _CURRENT_NAME, tmp_fd, pbytes)  # move-only: CURRENT sale → ausencia
            raise
        _fsync_typed(camp_fd)
        return cert

    if prev_ident is None or prev_raw is None or prev_fd < 0:
        raise BundleError("estado inconsistente: puntero previo sin identidad/bytes/fd")
    _validate_authority(camp_fd, prev_pointer["bundle_id"])  # B184: re-validar el previo JUSTO antes del exchange
    try:
        rename_exchange(camp_fd, tmp_name, camp_fd, _CURRENT_NAME)  # tmp <-> CURRENT
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        _quarantine_pointer(quar, camp_fd, tmp_name, tmp_fd, pbytes)  # B196: el temporal no debe quedar residual
        raise BundleError(f"no se pudo hacer CAS (exchange) de CURRENT: {exc}") from exc
    displaced = _capture(camp_fd, tmp_name)  # lo que estaba en CURRENT justo antes de MI exchange
    cur_ok = _pointer_matches(camp_fd, _CURRENT_NAME, tmp_ident, pbytes)
    if cur_ok and displaced == (prev_ident, prev_raw):  # swap legítimo: desplazado == el previo EXACTO
        try:
            cert = _certify_current(camp_fd, prepared, pbytes, tmp_ident, prev_id)  # B189/B197: unidad + previo válido
        except Exception:  # noqa: BLE001 — B190: compensa CUALQUIER excepción (no BaseException) antes de certificar
            _compensate(camp_fd, quar, tmp_name, tmp_fd, tmp_ident, pbytes, prev_fd, prev_raw, displaced)
            raise
        _fsync_typed(camp_fd)
        _quarantine_pointer_prev(quar, camp_fd, tmp_name, prev_fd, prev_raw)  # retira el previo desplazado (move-only)
        return cert
    # Carrera: CURRENT había cambiado. Restaurar el valor CONCURRENTE (el desplazado real), no el previo stale.
    _compensate(camp_fd, quar, tmp_name, tmp_fd, tmp_ident, pbytes, prev_fd, prev_raw, displaced)
    raise BundleConcurrencyError("CURRENT fue modificado concurrentemente; commit abortado y compensado")


def _quarantine_pointer_prev(quar: GovernedQuarantine, camp_fd: int, name: str, prev_fd: int, prev_raw: bytes) -> None:
    """Retira el previo DESPLAZADO (ahora en `name`) por cuarentena move-only, ligado a `prev_fd` (su inode)."""
    lease = OwnedLease(prev_fd, is_dir=False, known_digest=hashlib.sha256(prev_raw).hexdigest())
    try:
        quar.quarantine(camp_fd, name, lease)
    except (GovernedRemovalError, GovernedQuarantineError) as exc:  # B204: post-commit ⇒ CommittedStateError
        raise CommittedStateError(f"commit certificado pero el previo desplazado no se pudo poner en cuarentena: {exc}") from exc  # fmt: skip


def _compensate(camp_fd: int, quar: GovernedQuarantine, tmp_name: str, tmp_fd: int, tmp_ident: tuple[int, int], pbytes: bytes, prev_fd: int, prev_raw: bytes, displaced: tuple[tuple[int, int], bytes] | None) -> None:  # fmt: skip
    """Deshace un exchange que clobbereó un CURRENT concurrente: segundo exchange que restaura el valor CONCURRENTE
    (`displaced`) y verifica FÍSICAMENTE ambos lados por identidad Y contenido. La autoridad concurrente restaurada
    debe apuntar a un bundle VÁLIDO (B183). Sólo entonces retira mi puntero por cuarentena move-only. Si no se puede
    probar la restauración, PRESERVA todo y eleva `BundleRollbackIncompleteError` (B167)."""
    if displaced is None:
        raise BundleRollbackIncompleteError("el valor concurrente desplazado desapareció; estado PRESERVADO")
    try:
        rename_exchange(camp_fd, tmp_name, camp_fd, _CURRENT_NAME)
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        raise BundleRollbackIncompleteError(f"no se pudo compensar el CAS (estado PRESERVADO): {exc}") from exc
    if not _pointer_matches(camp_fd, _CURRENT_NAME, displaced[0], displaced[1]) or not _pointer_matches(camp_fd, tmp_name, tmp_ident, pbytes):  # fmt: skip
        raise BundleRollbackIncompleteError("tras compensar, CURRENT no volvió al valor concurrente; PRESERVADO")
    restored = _read_current(camp_fd)  # B183: la autoridad concurrente restaurada debe ser gobernada y válida
    if restored is None:
        raise BundleRollbackIncompleteError("tras compensar, CURRENT quedó ausente; PRESERVADO")
    try:
        _validate_authority(camp_fd, restored[0]["bundle_id"])
    except BundleError as exc:
        raise BundleRollbackIncompleteError(f"la autoridad concurrente restaurada es inválida: {exc}") from exc
    _quarantine_pointer(quar, camp_fd, tmp_name, tmp_fd, pbytes)  # retira mi puntero por cuarentena move-only


def build_and_commit(
    camp_fd: int,
    txid: str,
    campaign_id: str | None,
    outputs: list[dict],
    inputs: list[dict],
    provenance: dict,
) -> str:
    """Envoltura: prepara+valida el bundle inmutable (fd vivo) y hace CAS de CURRENT. Devuelve el `bundle_id` del
    `CommitCertificate` SÓLO cuando CURRENT apunta al bundle válido (punto de commit)."""
    with prepare_bundle(camp_fd, txid, campaign_id, outputs, inputs, provenance) as prepared:
        return commit_current(prepared).bundle_id


# --------------------------------------- resolución para consumidores (snapshot) ---------------------------------------


class _BundleSnapshot:
    """B163/B174: snapshot vivo — resuelve CURRENT UNA vez bajo el fd del puntero, valida el bundle A TRAVÉS del
    MISMO fd que se queda abierto (sin re-abrir por ruta) y mantiene ABIERTOS todos los fds (bundles/bundle/outputs/
    labels) durante la sesión. En cualquier fallo parcial cierra TODOS los fds ya abiertos (sin fugas)."""

    __slots__ = ("camp_fd", "bundle_id", "manifest", "_fds", "_labs")
    camp_fd: int
    bundle_id: str
    manifest: dict
    _fds: list[int]
    _labs: dict[str, int]

    def __init__(self, camp_fd: int) -> None:
        self.camp_fd = camp_fd
        self._fds = []  # B174: inicializar ANTES de cualquier apertura
        self._labs = {}
        try:
            cur = _read_current(camp_fd)
            if cur is None:
                raise BundleValidationError("no hay puntero CURRENT (ninguna campaña committeada)")
            self.bundle_id = cur[0]["bundle_id"]
            broot = _open_dir(camp_fd, _BUNDLES_DIR, mode=0o700)
            self._fds.append(broot)
            try:
                bfd = _open_dir(broot, self.bundle_id, mode=0o700)
            except FileNotFoundError as exc:  # CURRENT apunta a un bundle inexistente → autoridad inválida
                raise BundleValidationError(f"CURRENT apunta a un bundle inexistente {self.bundle_id}") from exc
            self._fds.append(bfd)
            self.manifest = _validate_bundle_at(bfd, self.bundle_id)  # valida A TRAVÉS del fd que retengo
            outs = _open_dir(bfd, _OUTPUTS_DIR, mode=0o700)
            self._fds.append(outs)
            for lab in _LABELS:
                lfd = _open_dir(outs, lab, mode=0o700)
                self._fds.append(lfd)
                self._labs[lab] = lfd
        except BaseException:
            self.close()
            raise

    def read(self, label: str, name: str) -> bytes:
        entry = next((o for o in self.manifest["outputs"] if o["label"] == label and o["name"] == name), None)
        if entry is None:
            raise BundleValidationError(f"{label}/{name} no está en el bundle {self.bundle_id}")
        data = _read_sealed(self._labs[label], name)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise BundleValidationError(f"{label}/{name} en el bundle no coincide con su sha256")
        return data

    def close(self) -> None:
        for fd in reversed(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds, self._labs = [], {}

    def __enter__(self) -> _BundleSnapshot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_current_snapshot(camp_fd: int) -> _BundleSnapshot:
    """FUENTE ÚNICA de resolución multi-lectura: `with open_current_snapshot(camp_fd) as s: s.read(...)`."""
    return _BundleSnapshot(camp_fd)


def open_current_bundle(camp_fd: int) -> tuple[str, dict]:
    """Resuelve CURRENT → valida el bundle COMPLETO → `(bundle_id, manifest)`. Eleva `BundleError` si no hay
    CURRENT o el bundle no valida."""
    with _BundleSnapshot(camp_fd) as snap:
        return snap.bundle_id, snap.manifest


def read_current_csv(camp_fd: int, label: str, name: str) -> bytes:
    """Lee un output oficial RESOLVIENDO por el bundle bajo un ÚNICO snapshot (nunca la proyección CSV mutable)."""
    with _BundleSnapshot(camp_fd) as snap:
        return snap.read(label, name)


if __name__ == "__main__":
    raise SystemExit("tools.campaign_bundle es una biblioteca; la CLI validate-current llega en el Incremento 2C")
