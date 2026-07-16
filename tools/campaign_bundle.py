#!/usr/bin/env python
"""Bundle INMUTABLE content-addressed + puntero CURRENT por CAS = la AUTORIDAD del commit del merge de campaña
(P0R.5 · R9.2R12 · B148/B145). FUENTE ÚNICA: ningún consumidor implementa su propia resolución — todos pasan por
`open_current_bundle()` / `read_current_csv()`.

El problema raíz (B148): ocho ficheros CSV mutables + un recibo NO forman un commit atómico — tras el último
snapshot se puede mutar un output y el proceso "committea". La única cura es mover la AUTORIDAD a un bundle
inmutable, direccionado por contenido, apuntado por un ÚNICO puntero `CURRENT` que se actualiza por CAS atómico:
el commit cruza SÓLO cuando `CURRENT` apunta a un bundle válido.

Estructura de un bundle (`reports/campaign/.merge-bundles/<bundle_id>/`, dirs 0700, ficheros 0600, sin symlinks):
    outputs/campaign/<8 CSV sellados>   outputs/eval/<...>   manifest.json
`bundle_id = sha256(manifest canónico)` — el manifiesto lleva campaign_id, txid, los 8 inputs (nombre/tamaño/
sha256), los 8 outputs (nombre/filas/columnas/sha256), procedencia completa y la cabeza terminal del journal;
los timestamps son SÓLO informativos y se EXCLUYEN del `bundle_id`. Un bundle sellado nunca se reescribe.

`CURRENT` (`.merge-CURRENT`, 0600, nlink==1) = `{schema_version, campaign_id, bundle_id, previous_bundle_id}`.
Se promueve por CAS: ausente → `rename_noreplace`; existente → `rename_exchange` verificando el puntero
desplazado. Tras promover se RE-ABRE y se valida; sólo entonces el commit cruzó.

Todas las operaciones son fd-relativas y usan `tools.atomic_fs` (sin `os.replace`/`os.rename`). Este módulo NO
importa `merge_campaign_pools` (evita el ciclo); recibe fds y bytes ya sellados/verificados del productor.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat

from tools.atomic_fs import AtomicRenameError, AtomicUnsupportedError, rename_exchange, rename_noreplace

_BUNDLES_DIR = ".merge-bundles"
_STAGING_PREFIX = ".merge-staging"
_CURRENT_NAME = ".merge-CURRENT"
_MANIFEST = "manifest.json"
_SCHEMA_VERSION = 1
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LABELS = ("campaign", "eval")


class BundleError(Exception):
    """Fallo verificable de construcción/validación/resolución del bundle o del puntero CURRENT."""


def _canon(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _mkdir_governed(parent_fd: int, name: str) -> int:
    """Crea `name` 0700 bajo `parent_fd` (create-only) y lo abre exigiendo dir real/UID/modo EXACTO 0700."""
    os.mkdir(name, 0o700, dir_fd=parent_fd)  # EEXIST revienta (nombre de nonce → nunca debería existir)
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o700)
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise BundleError(f"dir {name!r} ajeno/no-dir/modo != 0700")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_dir(parent_fd: int, name: str) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        os.close(fd)
        raise BundleError(f"dir {name!r} ajeno/no-dir")
    return fd


def _write_file(dir_fd: int, name: str, data: bytes) -> str:
    """Escribe `data` en `name` 0600 (create-only, O_EXCL|O_NOFOLLOW) con fsync; devuelve su sha256."""
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        mv = memoryview(data)
        off = 0
        while off < len(mv):
            n = os.write(fd, mv[off:])
            if n <= 0:
                raise BundleError("escritura incompleta en el bundle")
            off += n
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(data).hexdigest()


def _read_file(dir_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise BundleError(f"fichero {name!r} ajeno/no-regular")
        out = b""
        while chunk := os.read(fd, 1 << 16):
            out += chunk
        return out
    finally:
        os.close(fd)


def _manifest_for(
    campaign_id: str | None, txid: str, inputs: list[dict], outputs: list[dict], provenance: dict
) -> dict:
    """Manifiesto EXACTO cuyo sha256 es el `bundle_id`. Sin timestamps (son informativos y no entran en el id)."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "txid": txid,
        "inputs": sorted(inputs, key=lambda d: d["name"]),
        "outputs": sorted(outputs, key=lambda d: (d["label"], d["name"])),
        "provenance": provenance,
    }


def build_and_commit(
    camp_fd: int,
    txid: str,
    campaign_id: str | None,
    outputs: list[dict],
    inputs: list[dict],
    provenance: dict,
) -> str:
    """Construye el bundle inmutable desde COPIAS selladas de los outputs, lo promueve content-addressed y hace
    CAS del puntero CURRENT. Devuelve el `bundle_id` (sha256 del manifiesto) SÓLO cuando CURRENT apunta a un
    bundle válido — ese es el punto de commit. `outputs` = [{label,name,bytes,rows,cols}]; `inputs` =
    [{name,size,sha256}]. Cualquier fallo eleva `BundleError`/`OSError` (el productor lo trata como abort)."""
    staging_name = f"{_STAGING_PREFIX}.{txid}"
    sfds: list[int] = []
    try:
        sroot = _mkdir_governed(camp_fd, staging_name)
        sfds.append(sroot)
        outs_root = _mkdir_governed(sroot, "outputs")
        sfds.append(outs_root)
        label_fds = {lab: _mkdir_governed(outs_root, lab) for lab in _LABELS}
        sfds.extend(label_fds.values())
        out_meta: list[dict] = []
        for o in outputs:
            sha = _write_file(label_fds[o["label"]], o["name"], o["bytes"])
            if sha != hashlib.sha256(o["bytes"]).hexdigest():  # defensa: el sellado no altera bytes
                raise BundleError(f"output {o['name']!r} sellado con digest distinto")
            out_meta.append({"label": o["label"], "name": o["name"], "rows": o["rows"], "cols": o["cols"], "sha256": sha})  # fmt: skip
        manifest = _manifest_for(campaign_id, txid, inputs, out_meta, provenance)
        bundle_id = hashlib.sha256(_canon(manifest)).hexdigest()
        _write_file(sroot, _MANIFEST, _canon(manifest))
        for fd in (outs_root, *label_fds.values(), sroot):
            os.fsync(fd)
        _promote_staging(camp_fd, staging_name, bundle_id, manifest)
        _cas_current(camp_fd, campaign_id, bundle_id)
        _verify_current(camp_fd, bundle_id)  # el commit cruza SÓLO aquí
        return bundle_id
    finally:
        for fd in sfds:
            try:
                os.close(fd)
            except OSError:
                pass


def _promote_staging(camp_fd: int, staging_name: str, bundle_id: str, manifest: dict) -> None:
    """Promueve el staging → `.merge-bundles/<bundle_id>/` con un solo `rename_noreplace`. Si el bundle ya existe
    (mismo contenido) se VALIDA byte a byte; si difiere, se BLOQUEA (content-addressed inmutable)."""
    try:
        os.mkdir(_BUNDLES_DIR, 0o700, dir_fd=camp_fd)
    except FileExistsError:
        pass
    broot = _open_dir(camp_fd, _BUNDLES_DIR)
    try:
        if stat.S_IMODE(os.fstat(broot).st_mode) & 0o022:
            raise BundleError(f"{_BUNDLES_DIR} escribible por grupo/otros")
        try:
            rename_noreplace(camp_fd, staging_name, broot, bundle_id)
        except FileExistsError:  # ya existe un bundle con ese id → DEBE ser byte-idéntico
            _assert_bundle_matches(broot, bundle_id, manifest)
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


def _assert_bundle_matches(broot: int, bundle_id: str, manifest: dict) -> None:
    bfd = _open_dir(broot, bundle_id)
    try:
        if _read_file(bfd, _MANIFEST) != _canon(manifest):
            raise BundleError(f"bundle {bundle_id} preexistente difiere del manifiesto actual (colisión de id)")
    finally:
        os.close(bfd)


def _read_current(camp_fd: int) -> dict | None:
    try:
        fd = os.open(_CURRENT_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=camp_fd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600:  # fmt: skip
            raise BundleError("CURRENT no-regular/ajeno/hardlink/modo != 0600")
        raw = b""
        while chunk := os.read(fd, 1 << 16):
            raw += chunk
    finally:
        os.close(fd)
    return json.loads(raw)


def _cas_current(camp_fd: int, campaign_id: str | None, bundle_id: str) -> None:
    """CAS del puntero CURRENT: ausente → `rename_noreplace`; existente → `rename_exchange` verificando el puntero
    desplazado (el previo). El nuevo puntero se escribe en un temporal 0600, se `fsync`ea y se promueve."""
    prev = _read_current(camp_fd)
    prev_id = prev.get("bundle_id") if prev else None
    pointer = {"schema_version": _SCHEMA_VERSION, "campaign_id": campaign_id, "bundle_id": bundle_id, "previous_bundle_id": prev_id}  # fmt: skip
    tmp_name = f"{_CURRENT_NAME}.tmp.{os.getpid()}.{bundle_id[:12]}"
    tmp_fd = os.open(tmp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=camp_fd)
    try:
        os.write(tmp_fd, _canon(pointer))
        os.fsync(tmp_fd)
    finally:
        os.close(tmp_fd)
    try:
        if prev is None:
            rename_noreplace(camp_fd, tmp_name, camp_fd, _CURRENT_NAME)
        else:
            rename_exchange(camp_fd, tmp_name, camp_fd, _CURRENT_NAME)  # el temporal queda con el puntero previo
            displaced = _read_pointer_at(camp_fd, tmp_name)
            if displaced is None or displaced.get("bundle_id") != prev_id:
                raise BundleError("el puntero CURRENT desplazado no era el esperado (actualización concurrente)")
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        raise BundleError(f"no se pudo hacer CAS del puntero CURRENT: {exc}") from exc
    os.fsync(camp_fd)


def _read_pointer_at(camp_fd: int, name: str) -> dict | None:
    try:
        return json.loads(_read_file(camp_fd, name))
    except OSError, ValueError, BundleError:
        return None


def _verify_current(camp_fd: int, expect_bundle_id: str) -> None:
    """Re-abre CURRENT, resuelve el bundle y VALIDA (estructura + hashes) — el commit cruza sólo si CURRENT
    apunta a un bundle válido con el id esperado."""
    cur = _read_current(camp_fd)
    if cur is None or cur.get("bundle_id") != expect_bundle_id:
        raise BundleError("CURRENT no apunta al bundle recién sellado")
    broot = _open_dir(camp_fd, _BUNDLES_DIR)
    try:
        validate_bundle(broot, expect_bundle_id)
    finally:
        os.close(broot)


def validate_bundle(bundles_root_fd: int, bundle_id: str) -> dict:
    """Valida ESTRUCTURA + HASHES de un bundle: `bundle_id == sha256(manifest)` y cada output sellado coincide con
    su sha256 del manifiesto. Devuelve el manifiesto. Eleva `BundleError` ante cualquier discrepancia."""
    bfd = _open_dir(bundles_root_fd, bundle_id)
    try:
        manifest = json.loads(_read_file(bfd, _MANIFEST))
        if hashlib.sha256(_canon(manifest)).hexdigest() != bundle_id:
            raise BundleError(f"bundle_id {bundle_id} != sha256(manifest)")
        outs_root = _open_dir(bfd, "outputs")
        try:
            for o in manifest["outputs"]:
                lab = _open_dir(outs_root, o["label"])
                try:
                    if hashlib.sha256(_read_file(lab, o["name"])).hexdigest() != o["sha256"]:
                        raise BundleError(f"output {o['label']}/{o['name']} no coincide con su sha256")
                finally:
                    os.close(lab)
        finally:
            os.close(outs_root)
        return manifest
    finally:
        os.close(bfd)


def open_current_bundle(camp_fd: int) -> tuple[str, dict]:
    """FUENTE ÚNICA de resolución para los consumidores: resuelve CURRENT → valida el bundle → (bundle_id,
    manifest). Eleva `BundleError` si no hay CURRENT o el bundle no valida."""
    cur = _read_current(camp_fd)
    if cur is None:
        raise BundleError("no hay puntero CURRENT (ninguna campaña committeada)")
    bundle_id = cur["bundle_id"]
    broot = _open_dir(camp_fd, _BUNDLES_DIR)
    try:
        return bundle_id, validate_bundle(broot, bundle_id)
    finally:
        os.close(broot)


def read_current_csv(camp_fd: int, label: str, name: str) -> bytes:
    """Lee un output oficial RESOLVIENDO por el bundle (nunca la proyección CSV mutable). Verifica el sha256
    contra el manifiesto."""
    bundle_id, manifest = open_current_bundle(camp_fd)
    entry = next((o for o in manifest["outputs"] if o["label"] == label and o["name"] == name), None)
    if entry is None:
        raise BundleError(f"{label}/{name} no está en el bundle {bundle_id}")
    broot = _open_dir(camp_fd, _BUNDLES_DIR)
    try:
        bfd = _open_dir(broot, bundle_id)
        try:
            outs = _open_dir(bfd, "outputs")
            try:
                lab = _open_dir(outs, label)
                try:
                    data = _read_file(lab, name)
                finally:
                    os.close(lab)
            finally:
                os.close(outs)
        finally:
            os.close(bfd)
    finally:
        os.close(broot)
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise BundleError(f"{label}/{name} en el bundle no coincide con su sha256")
    return data
