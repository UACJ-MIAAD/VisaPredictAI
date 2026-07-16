#!/usr/bin/env python
"""Bundle INMUTABLE content-addressed + puntero CURRENT por CAS = la AUTORIDAD del commit del merge de campaña
(P0R.5 · R9.2R12 · B148/B145 · Incremento 1R B155-B164). FUENTE ÚNICA: ningún consumidor implementa su propia
resolución — todos pasan por `open_current_bundle()` / `read_current_csv()`.

El problema raíz (B148): ocho ficheros CSV mutables + un recibo NO forman un commit atómico — tras el último
snapshot se puede mutar un output y el proceso "committea". La única cura es mover la AUTORIDAD a un bundle
inmutable, direccionado por contenido, apuntado por un ÚNICO puntero `CURRENT` que se actualiza por CAS atómico:
el commit cruza SÓLO cuando `CURRENT` apunta a un bundle válido.

Contrato CERRADO del manifiesto (B159/B160): exactamente 8 inputs `aq_pool_{nongbm,gbm}_{FAD,DFF}_{family,
employment}.csv`, 4 outputs `campaign_pool_*` (label campaign) + 4 `model_comparison_*` (label eval), claves de
manifiesto exactas, tipos estrictos (bool != int), sha256 de 64 hex, tamaños/filas/columnas enteros positivos,
procedencia oficial completa (hashes de los módulos de la ruta de confianza + cabezas terminales de journal).
`bundle_id = sha256(manifiesto canónico)`; los timestamps son informativos y NO entran en el id. La validación
exige ADEMÁS inventario físico EXACTO (ni ficheros de más ni de menos) e identidad gobernada de cada fichero
sellado (regular, UID actual, nlink==1, sin escritura de grupo/otros, snapshot fstat pre/post).

`CURRENT` (`.merge-CURRENT`, 0600, nlink==1) = `{schema_version, campaign_id, bundle_id, previous_bundle_id}`.
CAS (B156/B157): ausente → `rename_noreplace`; existente → `rename_exchange` verificando que el desplazado sea el
puntero previo EXACTO (bytes). Si NO coincide (carrera) se COMPENSA con un segundo exchange que restaura el
puntero concurrente, se verifica físicamente y se eleva `BundleConcurrencyError` SIN devolver éxito. Escritura del
puntero con bucle `_write_all` + `O_EXCL|O_NOFOLLOW` + fsync + relectura. Tras un CAS válido, cualquier fallo
posterior es `CommittedStateError`, nunca un rollback silencioso.

Todas las operaciones son fd-relativas y usan `tools.atomic_fs` (sin `os.replace`/`os.rename`). Este módulo NO
importa `merge_campaign_pools` (evita el ciclo); recibe bytes ya sellados/verificados del productor (el productor
relee cada output desde su fd CERTIFICADO con snapshot pre/post y revalida el digest antes de pasarlos: B158/B164).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat

from tools.atomic_fs import AtomicRenameError, AtomicUnsupportedError, rename_exchange, rename_noreplace
from tools.governed_read import read_governed_bytes, relative_name_problem

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
        "git_head",
        "code_sha_merge_campaign_pools",
        "code_sha_campaign_bundle",
        "code_sha_atomic_fs",
        "code_sha_governed_read",
        "code_sha_execution_contract",
        "journal_heads",
    }
)
_MANIFEST_KEYS = frozenset({"schema_version", "campaign_id", "txid", "inputs", "outputs", "provenance"})
_INPUT_KEYS = frozenset({"name", "size", "sha256"})
_OUTPUT_KEYS = frozenset({"label", "name", "rows", "cols", "sha256"})


class BundleError(Exception):
    """Base: fallo verificable del bundle o del puntero CURRENT."""


class BundleValidationError(BundleError):
    """Estructura/esquema/identidad/inventario/hash inválidos."""


class BundleConcurrencyError(BundleError):
    """Actualización concurrente de CURRENT detectada; NO se cruzó el commit (estado incompleto compensado)."""


class BundleRollbackIncompleteError(BundleError):
    """Un rollback/compensación no pudo restaurar el estado previo verificable."""


class CommittedStateError(BundleError):
    """El CAS de CURRENT ya cruzó; un fallo posterior NO es un rollback — el commit está comprometido."""


# --------------------------------------------------- helpers ---------------------------------------------------


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
    return json.loads(raw, object_pairs_hook=_no_dup_keys)


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)  # bool NO es int aceptable (True/False se rechazan)


def _is_pos_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def _is_hex64(x: object) -> bool:
    return isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x)


def _require_relative(kind: str, name: object) -> None:
    if not isinstance(name, str):
        raise BundleValidationError(f"{kind} no-string: {name!r}")
    problem = relative_name_problem(name)
    if problem is not None:
        raise BundleValidationError(f"{kind} inseguro {name!r}: {problem}")


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


def _open_dir(parent_fd: int, name: str, *, require_private: bool = False) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise BundleValidationError(f"dir {name!r} ajeno/no-dir")
        if require_private and stat.S_IMODE(st.st_mode) & 0o022:
            raise BundleValidationError(f"dir {name!r} escribible por grupo/otros")
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


def _validate_manifest(manifest: object) -> dict:
    """Contrato CERRADO del manifiesto (B159). No toca disco. Eleva `BundleValidationError` ante cualquier
    desviación: claves exactas, tipos estrictos (bool != int), conjuntos de nombres esperados, sha256 64-hex,
    enteros positivos, labels válidos, sin duplicados, procedencia completa."""
    if not isinstance(manifest, dict):
        raise BundleValidationError("manifiesto no es objeto")
    if set(manifest.keys()) != _MANIFEST_KEYS:
        raise BundleValidationError(f"claves del manifiesto != {sorted(_MANIFEST_KEYS)}: {sorted(manifest.keys())}")
    if manifest["schema_version"] is not _SCHEMA_VERSION and not (
        _is_int(manifest["schema_version"]) and manifest["schema_version"] == _SCHEMA_VERSION
    ):
        raise BundleValidationError(f"schema_version inválido: {manifest['schema_version']!r}")
    cid = manifest["campaign_id"]
    if cid is not None and not isinstance(cid, str):
        raise BundleValidationError(f"campaign_id no str|None: {cid!r}")
    if isinstance(cid, bool):
        raise BundleValidationError("campaign_id bool")
    if not isinstance(manifest["txid"], str) or not manifest["txid"]:
        raise BundleValidationError("txid vacío/no-str")
    # inputs
    inputs = manifest["inputs"]
    if not isinstance(inputs, list) or len(inputs) != len(_EXPECTED_INPUTS):
        raise BundleValidationError(f"inputs debe tener exactamente {len(_EXPECTED_INPUTS)} entradas")
    seen_in: set[str] = set()
    for e in inputs:
        if not isinstance(e, dict) or set(e.keys()) != _INPUT_KEYS:
            raise BundleValidationError(f"input con claves != {sorted(_INPUT_KEYS)}: {e!r}")
        _require_relative("input", e["name"])
        if e["name"] not in _EXPECTED_INPUTS:
            raise BundleValidationError(f"input inesperado: {e['name']!r}")
        if e["name"] in seen_in:
            raise BundleValidationError(f"input duplicado: {e['name']!r}")
        seen_in.add(e["name"])
        if not _is_pos_int(e["size"]):
            raise BundleValidationError(f"size de input inválido: {e['size']!r}")
        if not _is_hex64(e["sha256"]):
            raise BundleValidationError(f"sha256 de input inválido: {e['sha256']!r}")
    if seen_in != set(_EXPECTED_INPUTS):
        raise BundleValidationError("conjunto de inputs != esperado")
    # outputs
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
        if e["name"] not in _EXPECTED_OUTPUTS[lab]:
            raise BundleValidationError(f"output inesperado para {lab}: {e['name']!r}")
        if e["name"] in seen_out[lab]:
            raise BundleValidationError(f"output duplicado {lab}/{e['name']!r}")
        seen_out[lab].add(e["name"])
        if not _is_pos_int(e["rows"]) or not _is_pos_int(e["cols"]):
            raise BundleValidationError(f"rows/cols inválidos en {lab}/{e['name']}")
        if not _is_hex64(e["sha256"]):
            raise BundleValidationError(f"sha256 de output inválido: {e['sha256']!r}")
    for lab in _LABELS:
        if seen_out[lab] != set(_EXPECTED_OUTPUTS[lab]):
            raise BundleValidationError(f"conjunto de outputs {lab} != esperado")
    # provenance
    prov = manifest["provenance"]
    if not isinstance(prov, dict) or set(prov.keys()) != _REQUIRED_PROVENANCE:
        raise BundleValidationError(f"provenance con claves != {sorted(_REQUIRED_PROVENANCE)}")
    if prov["git_head"] is not None and not (isinstance(prov["git_head"], str) and prov["git_head"]):
        raise BundleValidationError("git_head no str|None")
    for k in ("code_sha_merge_campaign_pools", "code_sha_campaign_bundle", "code_sha_atomic_fs", "code_sha_governed_read"):  # fmt: skip
        if not _is_hex64(prov[k]):
            raise BundleValidationError(f"{k} no es sha256: {prov[k]!r}")
    ec = prov["code_sha_execution_contract"]
    if ec is not None and not _is_hex64(ec):
        raise BundleValidationError(f"code_sha_execution_contract no sha256|None: {ec!r}")
    if not isinstance(prov["journal_heads"], dict):
        raise BundleValidationError("journal_heads no es dict")
    return manifest


def _manifest_for(campaign_id: str | None, txid: str, inputs: list[dict], outputs: list[dict], provenance: dict) -> dict:  # fmt: skip
    return {
        "schema_version": _SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "txid": txid,
        "inputs": sorted(inputs, key=lambda d: d["name"]),
        "outputs": sorted(outputs, key=lambda d: (d["label"], d["name"])),
        "provenance": provenance,
    }


def _rmtree_at(parent_fd: int, name: str) -> None:
    """Remoción recursiva fd-relativa de un árbol de STAGING (best-effort; nunca sigue symlinks). Idempotente."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError:
        return
    try:
        for entry in os.listdir(fd):
            try:
                st = os.lstat(entry, dir_fd=fd)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                _rmtree_at(fd, entry)
            else:
                try:
                    os.unlink(entry, dir_fd=fd)
                except OSError:
                    pass
    finally:
        os.close(fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        pass


# ------------------------------------------- preparar / validar / commit -------------------------------------------


class _Prepared:
    """Bundle inmutable YA promovido a `.merge-bundles/<bundle_id>/` y validado, pero CURRENT aún NO tocado."""

    __slots__ = ("camp_fd", "bundle_id", "campaign_id", "manifest")

    def __init__(self, camp_fd: int, bundle_id: str, campaign_id: str | None, manifest: dict) -> None:
        self.camp_fd = camp_fd
        self.bundle_id = bundle_id
        self.campaign_id = campaign_id
        self.manifest = manifest


def prepare_bundle(
    camp_fd: int,
    txid: str,
    campaign_id: str | None,
    outputs: list[dict],
    inputs: list[dict],
    provenance: dict,
) -> _Prepared:
    """Construye y promueve el bundle inmutable content-addressed SIN tocar CURRENT. `outputs` =
    [{label,name,bytes,rows,cols}] (bytes = copia YA certificada por el productor), `inputs` = [{name,bytes}]
    (bytes reales del fichero, para tamaño/hash — NUNCA reconstruidos: B164). Valida el manifiesto CERRADO antes
    de sellar; promueve inmutable (colisión → validación COMPLETA del preexistente, B161); limpia el staging en
    CADA caso (B162). Devuelve `_Prepared`. Eleva `BundleValidationError`/`BundleError`."""
    if not isinstance(txid, str) or not txid:
        raise BundleValidationError("txid vacío")
    in_meta = []
    for i in inputs:
        _require_relative("input", i["name"])
        b = i["bytes"]
        in_meta.append({"name": i["name"], "size": len(b), "sha256": hashlib.sha256(b).hexdigest()})
    out_meta = []
    for o in outputs:
        if o["label"] not in _LABELS:
            raise BundleValidationError(f"label inválido: {o['label']!r}")
        _require_relative("output", o["name"])
        out_meta.append(
            {
                "label": o["label"],
                "name": o["name"],
                "rows": int(o["rows"]),
                "cols": int(o["cols"]),
                "sha256": hashlib.sha256(o["bytes"]).hexdigest(),
            }
        )
    manifest = _validate_manifest(_manifest_for(campaign_id, txid, in_meta, out_meta, provenance))
    bundle_id = hashlib.sha256(_canon(manifest)).hexdigest()

    staging_name = f"{_STAGING_PREFIX}.{secrets.token_hex(12)}"
    by_name = {(o["label"], o["name"]): o for o in outputs}
    try:
        sroot = _mkdir_governed(camp_fd, staging_name)
        try:
            outs_root = _mkdir_governed(sroot, _OUTPUTS_DIR)
            try:
                for lab in _LABELS:
                    lfd = _mkdir_governed(outs_root, lab)
                    try:
                        for name in sorted(_EXPECTED_OUTPUTS[lab]):
                            o = by_name[(lab, name)]
                            sha = _seal_file(lfd, name, o["bytes"])
                            if sha != hashlib.sha256(o["bytes"]).hexdigest():
                                raise BundleValidationError(f"output {lab}/{name} sellado con digest distinto")
                        os.fsync(lfd)
                    finally:
                        os.close(lfd)
                os.fsync(outs_root)
            finally:
                os.close(outs_root)
            _seal_file(sroot, _MANIFEST, _canon(manifest))
            os.fsync(sroot)
        finally:
            os.close(sroot)
        _promote_staging(camp_fd, staging_name, bundle_id, manifest)
    finally:
        _rmtree_at(camp_fd, staging_name)  # B162: staging jamás queda suelto (éxito, colisión o error)
    prepared = _Prepared(camp_fd, bundle_id, campaign_id, manifest)
    validate_prepared_bundle(prepared)
    return prepared


def validate_prepared_bundle(prepared: _Prepared) -> dict:
    broot = _open_dir(prepared.camp_fd, _BUNDLES_DIR, require_private=True)
    try:
        return validate_bundle(broot, prepared.bundle_id)
    finally:
        os.close(broot)


def _promote_staging(camp_fd: int, staging_name: str, bundle_id: str, manifest: dict) -> None:
    """Promueve staging → `.merge-bundles/<bundle_id>/` con un solo `rename_noreplace`. Si el bundle ya existe se
    VALIDA COMPLETO (estructura + inventario + hashes), no sólo `manifest.json` (B161); si difiere, BLOQUEA."""
    try:
        os.mkdir(_BUNDLES_DIR, 0o700, dir_fd=camp_fd)
    except FileExistsError:
        pass
    broot = _open_dir(camp_fd, _BUNDLES_DIR, require_private=True)
    try:
        try:
            rename_noreplace(camp_fd, staging_name, broot, bundle_id)
        except FileExistsError:  # ya existe un bundle con ese id → DEBE validar COMPLETO e igualar el manifiesto
            existing = validate_bundle(broot, bundle_id)
            if _canon(existing) != _canon(manifest):
                raise BundleValidationError(f"bundle {bundle_id} preexistente difiere (colisión de id)") from None
            return
        except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
            raise BundleError(f"no se pudo promover el bundle: {exc}") from exc
        bfd = _open_dir(broot, bundle_id)
        try:
            os.fsync(bfd)
        finally:
            os.close(bfd)
        os.fsync(broot)
    finally:
        os.close(broot)


def validate_bundle(bundles_root_fd: int, bundle_id: str) -> dict:
    """Valida un bundle COMPLETO: esquema cerrado + `bundle_id == sha256(manifest)` + inventario físico EXACTO
    (bundle dir, outputs/, cada label) + identidad gobernada y sha256 de cada output sellado. Devuelve el
    manifiesto. Eleva `BundleValidationError` ante cualquier discrepancia."""
    if not _is_hex64(bundle_id):
        raise BundleValidationError(f"bundle_id no es sha256: {bundle_id!r}")
    bfd = _open_dir(bundles_root_fd, bundle_id)
    try:
        manifest = _validate_manifest(_strict_loads(_read_sealed(bfd, _MANIFEST)))
        if hashlib.sha256(_canon(manifest)).hexdigest() != bundle_id:
            raise BundleValidationError(f"bundle_id {bundle_id} != sha256(manifest)")
        _listdir_exact(bfd, {_MANIFEST, _OUTPUTS_DIR}, f"bundle {bundle_id}")
        outs_root = _open_dir(bfd, _OUTPUTS_DIR)
        try:
            _listdir_exact(outs_root, set(_LABELS), "outputs/")
            for lab in _LABELS:
                lfd = _open_dir(outs_root, lab)
                try:
                    names = {o["name"] for o in manifest["outputs"] if o["label"] == lab}
                    _listdir_exact(lfd, names, f"outputs/{lab}")
                    for o in (e for e in manifest["outputs"] if e["label"] == lab):
                        if hashlib.sha256(_read_sealed(lfd, o["name"])).hexdigest() != o["sha256"]:
                            raise BundleValidationError(f"output {lab}/{o['name']} no coincide con su sha256")
                finally:
                    os.close(lfd)
        finally:
            os.close(outs_root)
        return manifest
    finally:
        os.close(bfd)


def _read_current(camp_fd: int) -> tuple[dict, bytes, tuple[int, int]] | None:
    """Lee CURRENT con identidad gobernada. Devuelve `(pointer, bytes_crudos, (dev,ino))` o None si no existe."""
    try:
        fd = os.open(_CURRENT_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=camp_fd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600:  # fmt: skip
            raise BundleValidationError("CURRENT no-regular/ajeno/hardlink/modo != 0600")
        raw = b""
        while chunk := os.read(fd, 1 << 16):
            raw += chunk
        if _snap(os.fstat(fd)) != _snap(st):
            raise BundleValidationError("CURRENT mutado durante la lectura")
        pointer = _strict_loads(raw)
    finally:
        os.close(fd)
    if not isinstance(pointer, dict) or not _is_hex64(pointer.get("bundle_id")):
        raise BundleValidationError("CURRENT con bundle_id inválido")
    return pointer, raw, (st.st_dev, st.st_ino)


def _snap(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink, st.st_mode)


def commit_current(prepared: _Prepared) -> str:
    """PUNTO DE COMMIT: hace CAS del puntero CURRENT hacia `prepared.bundle_id`. Ausente → `rename_noreplace`;
    existente → `rename_exchange` verificando que el desplazado sea el puntero previo EXACTO (bytes). Carrera →
    compensa con un segundo exchange, verifica y eleva `BundleConcurrencyError` (NO devuelve éxito, B156). Tras un
    CAS válido, cualquier fallo es `CommittedStateError` (B157). Devuelve el `bundle_id`."""
    camp_fd = prepared.camp_fd
    prev = _read_current(camp_fd)
    prev_pointer, prev_bytes, prev_ident = prev if prev is not None else (None, None, None)
    prev_id = prev_pointer["bundle_id"] if prev_pointer else None
    pointer = {"schema_version": _SCHEMA_VERSION, "campaign_id": prepared.campaign_id, "bundle_id": prepared.bundle_id, "previous_bundle_id": prev_id}  # fmt: skip
    tmp_name = f"{_CURRENT_TMP_PREFIX}.{secrets.token_hex(12)}"
    tmp_fd = os.open(tmp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=camp_fd)
    try:
        _write_all(tmp_fd, _canon(pointer))
        os.fsync(tmp_fd)
    except BaseException:
        os.close(tmp_fd)
        _quiet_unlink(camp_fd, tmp_name)
        raise
    os.close(tmp_fd)

    committed = False
    try:
        if prev is None:
            try:
                rename_noreplace(camp_fd, tmp_name, camp_fd, _CURRENT_NAME)
            except (AtomicRenameError, AtomicUnsupportedError, FileExistsError, OSError, ValueError) as exc:
                raise BundleConcurrencyError(f"CURRENT apareció durante el CAS inicial: {exc}") from exc
            committed = True
        else:
            try:
                rename_exchange(camp_fd, tmp_name, camp_fd, _CURRENT_NAME)  # tmp queda con el puntero desplazado
            except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
                raise BundleError(f"no se pudo hacer CAS (exchange) de CURRENT: {exc}") from exc
            displaced = _read_pointer_bytes(camp_fd, tmp_name)
            if displaced != prev_bytes:
                # Carrera: alguien cambió CURRENT entre la lectura y el exchange. CURRENT quedó apuntando a MI
                # bundle (clobber del concurrente). COMPENSAR: segundo exchange restaura el puntero concurrente.
                _compensate(camp_fd, tmp_name, displaced)
                raise BundleConcurrencyError("CURRENT fue modificado concurrentemente; commit abortado y compensado")
            committed = True  # CAS legítimo: el commit CRUZÓ aquí
    finally:
        if not committed:
            _quiet_unlink(camp_fd, tmp_name)

    # --- a partir de aquí el commit está COMPROMETIDO: todo fallo es CommittedStateError, jamás rollback ---
    try:
        os.fsync(camp_fd)
        if prev is not None:
            _quiet_unlink(camp_fd, tmp_name)  # tmp = puntero previo superseded (lineage en previous_bundle_id)
        _verify_current(camp_fd, prepared.bundle_id)
    except BundleError as exc:
        raise CommittedStateError(f"CURRENT cruzó pero la finalización falló: {exc}") from exc
    except OSError as exc:
        raise CommittedStateError(f"CURRENT cruzó pero la finalización falló: {exc}") from exc
    return prepared.bundle_id


def _compensate(camp_fd: int, tmp_name: str, concurrent_bytes: bytes | None) -> None:
    """Deshace un exchange que clobbereó un CURRENT concurrente: segundo exchange (restaura el concurrente en
    CURRENT, mi puntero vuelve a tmp), verifica FÍSICAMENTE ambos lados (CURRENT == bytes concurrentes capturados;
    mi puntero de vuelta en tmp) y retira mi puntero. Si no puede restaurar de forma verificable, eleva
    `BundleRollbackIncompleteError` (estado incompleto, jamás éxito)."""
    if concurrent_bytes is None:
        raise BundleRollbackIncompleteError("no se pudo leer el puntero concurrente desplazado; sin compensación")
    try:
        rename_exchange(camp_fd, tmp_name, camp_fd, _CURRENT_NAME)  # restaura el concurrente en CURRENT
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        raise BundleRollbackIncompleteError(f"no se pudo compensar el CAS concurrente: {exc}") from exc
    try:
        restored = _read_current(camp_fd)
    except BundleError as exc:
        raise BundleRollbackIncompleteError(f"tras compensar, CURRENT no valida: {exc}") from exc
    if restored is None or restored[1] != concurrent_bytes:
        raise BundleRollbackIncompleteError("tras compensar, CURRENT no quedó igual al puntero concurrente")
    if _read_pointer_bytes(camp_fd, tmp_name) is None:
        raise BundleRollbackIncompleteError("tras compensar, mi puntero no volvió a tmp (lado inesperado)")
    _quiet_unlink(camp_fd, tmp_name)  # mi puntero withdrawn; nunca fue autoridad


def _read_pointer_bytes(camp_fd: int, name: str) -> bytes | None:
    try:
        return _read_sealed(camp_fd, name)
    except OSError, BundleError:
        return None


def _quiet_unlink(camp_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=camp_fd)
    except OSError:
        pass


def _verify_current(camp_fd: int, expect_bundle_id: str) -> None:
    cur = _read_current(camp_fd)
    if cur is None or cur[0]["bundle_id"] != expect_bundle_id:
        raise BundleValidationError("CURRENT no apunta al bundle recién sellado")
    broot = _open_dir(camp_fd, _BUNDLES_DIR)
    try:
        validate_bundle(broot, expect_bundle_id)
    finally:
        os.close(broot)


def build_and_commit(
    camp_fd: int,
    txid: str,
    campaign_id: str | None,
    outputs: list[dict],
    inputs: list[dict],
    provenance: dict,
) -> str:
    """Envoltura: prepara+valida el bundle inmutable y hace CAS de CURRENT. Devuelve el `bundle_id` SÓLO cuando
    CURRENT apunta al bundle válido (punto de commit)."""
    prepared = prepare_bundle(camp_fd, txid, campaign_id, outputs, inputs, provenance)
    return commit_current(prepared)


# --------------------------------------- resolución para consumidores (snapshot) ---------------------------------------


class _BundleSnapshot:
    """B163: snapshot vivo — resuelve CURRENT UNA vez, valida y mantiene ABIERTOS los fds (bundles/bundle/outputs/
    labels) durante toda la sesión de lectura, de modo que varias lecturas NO puedan mezclar dos versiones de
    CURRENT ni reabrir por ruta tras validar. Context manager."""

    __slots__ = ("camp_fd", "bundle_id", "manifest", "_broot", "_bfd", "_outs", "_labs")
    camp_fd: int
    bundle_id: str
    manifest: dict
    _broot: int | None
    _bfd: int | None
    _outs: int | None
    _labs: dict[str, int]

    def __init__(self, camp_fd: int) -> None:
        cur = _read_current(camp_fd)
        if cur is None:
            raise BundleValidationError("no hay puntero CURRENT (ninguna campaña committeada)")
        self.camp_fd = camp_fd
        self.bundle_id = cur[0]["bundle_id"]
        self._broot = _open_dir(camp_fd, _BUNDLES_DIR, require_private=True)
        try:
            self.manifest = validate_bundle(self._broot, self.bundle_id)
            self._bfd = _open_dir(self._broot, self.bundle_id)
            self._outs = _open_dir(self._bfd, _OUTPUTS_DIR)
            self._labs = {lab: _open_dir(self._outs, lab) for lab in _LABELS}
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
        for fd in (*getattr(self, "_labs", {}).values(), getattr(self, "_outs", None), getattr(self, "_bfd", None), getattr(self, "_broot", None)):  # fmt: skip
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._labs, self._outs, self._bfd, self._broot = {}, None, None, None

    def __enter__(self) -> _BundleSnapshot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_current_snapshot(camp_fd: int) -> _BundleSnapshot:
    """FUENTE ÚNICA de resolución multi-lectura para consumidores: `with open_current_snapshot(camp_fd) as s: ...`."""
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
