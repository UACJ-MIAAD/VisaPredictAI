#!/usr/bin/env python
"""B332: identidad de un import por DESCRIPTOR gobernado — NO por comparación de strings.

`deep_smoke.identity_problems` sólo comparaba el `spec.origin` como texto (`realpath` + `startswith`), así que un
`__spec__.origin` FORJADO que apuntara a un fichero inexistente bajo `sys.prefix` pasaba el chequeo sin abrir nada. Aquí
el origin de un módulo se ABRE gobernado: descenso `openat` componente-a-componente desde `sys.prefix`, `O_NOFOLLOW` en
cada componente (ningún symlink en la cadena), invariantes de directorio, leaf regular / uid permitido / `nlink==1` /
sin escritura grupo-otros, hash SHA-256 desde el MISMO descriptor, re-`fstat` idéntico tras el hash. Además se CRUZA con
`packages_distributions()[module]` (proveedor EXACTO) y con `Distribution.files`/`locate_file` (pertenencia REAL). Se
rechaza namespace / built-in / frozen / inexistente / dist-info duplicada / fichero no inventariado. Devuelve un
`ImportIdentity {module, distribution, origin, origin_sha256}` en orden canónico, o la lista de problemas (recibo vacío).

Stdlib-only; el descenso reutiliza el substrato gobernado de `governance_snapshot` (misma frontera honesta: evita
symlinks, objetos especiales y mutación de inode DURANTE la lectura; no es criptográfico contra un root hostil que
alterne y restaure el árbol entre `fstat`s)."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass

from tools.governance_snapshot import StatSnapshot, _dir_problem, _rel_parts
from tools.lock_contracts import _norm

_GO_WRITE = 0o022  # bits de escritura grupo/otros — prohibidos en el origin y en la cadena de directorios
_READ_CHUNK = 1 << 16


@dataclass(frozen=True, slots=True)
class ImportIdentity:
    """Identidad CERTIFICADA de un import del stack (orden canónico de campos). RC-3: un módulo puede estar provisto por
    VARIAS distribuciones (`providers`); `origin_owners` son los providers cuyo RECORD contiene el origen (no vacío)."""

    module: str
    distribution: str  # distribución PRIMARIA
    providers: tuple[str, ...]  # todas las distribuciones que proveen el módulo (PEP-503, ordenadas)
    origin: str  # relativo a sys.prefix, POSIX
    origin_sha256: str
    origin_owners: tuple[str, ...]  # providers cuyo RECORD contiene el origen (subconjunto no vacío de providers)


def _hash_leaf(lfd: int, uid: int) -> tuple[str | None, str | None]:
    """Del leaf ya abierto (`O_NOFOLLOW|O_NONBLOCK`): exige regular / sin setuid-setgid-sticky / sin escritura g-o /
    dueño `uid` / `nlink==1`, hashea desde el MISMO fd y re-`fstat`ea idéntico. Devuelve `(problema, sha256)`."""
    try:
        st0 = os.fstat(lfd)  # B293: el MISMO objeto valida y (abajo) se compara
    except OSError as exc:
        return f"fstat del origin falló ({exc})", None
    if not stat.S_ISREG(st0.st_mode):
        return "el origin no es un fichero regular", None
    if st0.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        return "el origin tiene bits setuid/setgid/sticky", None
    if st0.st_mode & _GO_WRITE:
        return f"el origin es escribible por grupo/otros (modo {oct(stat.S_IMODE(st0.st_mode))})", None
    if st0.st_uid != uid:
        return f"el origin tiene dueño uid {st0.st_uid} != {uid}", None
    if st0.st_nlink != 1:
        return f"el origin tiene nlink {st0.st_nlink} != 1 (hardlink)", None
    h = hashlib.sha256()
    size = 0
    while True:
        try:
            chunk = os.read(lfd, _READ_CHUNK)
        except OSError as exc:
            return f"error de lectura del origin ({exc})", None
        if not chunk:
            break
        size += len(chunk)
        h.update(chunk)
    try:
        st1 = os.fstat(lfd)  # B293: re-fstat tras el hash
    except OSError as exc:
        return f"re-fstat del origin falló ({exc})", None
    if StatSnapshot.of(st0).identity() != StatSnapshot.of(st1).identity() or size != st1.st_size:
        return "el inode del origin cambió durante la lectura", None
    return None, "sha256:" + h.hexdigest()


def _governed_read_under_prefix(sys_prefix: str, rel: str, uid: int) -> tuple[str | None, str | None]:
    """Descenso `openat` componente-a-componente desde `sys_prefix` hasta `rel` (POSIX relativo), `O_NOFOLLOW` en cada
    componente (ningún symlink en la cadena), invariantes de directorio y hash gobernado del leaf. Devuelve
    `(sha256, problema)`; `sha256` es None si hubo problema. Cierra TODOS los fds aunque uno falle."""
    parts = _rel_parts(rel)
    if parts is None:
        return None, f"origin rel {rel!r}: ruta relativa POSIX inválida"
    dir_comps, leaf = parts[:-1], parts[-1]
    all_fds: list[int] = []
    primary: str | None = None
    sha: str | None = None
    close_errors: list[str] = []
    try:
        try:
            base_fd = os.open(sys_prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            return None, f"sys.prefix {sys_prefix!r} no abrible como directorio no-symlink ({exc})"
        all_fds.append(base_fd)
        cur = base_fd
        for comp in dir_comps:
            try:
                nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cur)
            except OSError as exc:
                primary = f"componente {comp!r} no es directorio no-symlink abrible ({exc})"
                break
            all_fds.append(nfd)
            try:
                st_dir = os.fstat(nfd)
            except OSError as exc:
                primary = f"fstat del directorio {comp!r} falló ({exc})"
                break
            dprob = _dir_problem(comp, st_dir, uid)
            if dprob is not None:
                primary = dprob
                break
            cur = nfd
        if primary is None:
            try:
                lfd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=cur)
            except OSError as exc:
                primary = f"leaf {leaf!r} no abrible sin seguir symlink ({exc})"
            else:
                all_fds.append(lfd)
                primary, sha = _hash_leaf(lfd, uid)
    # red de seguridad: ninguna OSError operacional escapa cruda (KI/SE no son OSError → propagan)
    except OSError as exc:
        primary = f"error de sistema inesperado ({exc})"
    finally:
        for fd in reversed(all_fds):
            try:
                os.close(fd)
            except OSError as exc:
                close_errors.append(str(exc))
    if primary is not None:
        return None, (
            f"{primary} | además fallo al cerrar fd(s): {'; '.join(close_errors)}" if close_errors else primary
        )
    if close_errors:  # el camino iba a tener éxito pero un cierre falló → FALLO
        return None, f"fallo al cerrar fd(s): {'; '.join(close_errors)}"
    return sha, None


def governed_identity(
    module: str,
    *,
    providers: list[str],
    primary: str,
    origin: str | None,
    providing: list[str],
    provider_files: dict[str, list[str] | None],
    sys_prefix: str,
    uid: int | None = None,
) -> tuple[list[str], ImportIdentity | None]:
    """RC-3: certifica la identidad de `module` por DESCRIPTOR, modelando MÚLTIPLES `providers` (p.ej. mlflow ← mlflow /
    mlflow-skinny / mlflow-tracing). Exige que `packages_distributions().get(module)` (`providing`) sea EXACTAMENTE el
    conjunto de `providers`. `origin` es `__spec__.origin` (None para namespace/built-in/frozen). `provider_files` mapea
    cada provider a las rutas ABSOLUTAS de su RECORD (o None si no declara). El origen debe abrirse gobernado bajo
    `sys.prefix` (openat/O_NOFOLLOW, hash del fd) y PERTENECER al RECORD de AL MENOS UN provider (`origin_owners`, no vacío).
    `primary` es la distribución primaria del recibo. Devuelve `(problemas, ImportIdentity | None)`."""
    probs: list[str] = []
    if uid is None:
        uid = os.getuid()
    want = sorted({_norm(p) for p in providers})
    provided = sorted({_norm(p) for p in providing})  # cruce EXACTO con packages_distributions
    if provided != want:
        probs.append(f"{module}: packages_distributions {provided} != providers {want}")
    if origin is None:  # namespace / built-in / frozen — sin origin certificable
        probs.append(f"{module}: sin origin certificable (namespace/built-in/frozen)")
        return probs, None
    if not os.path.isabs(origin):
        probs.append(f"{module}: origin {origin!r} no es una ruta absoluta")
        return probs, None
    rel = os.path.relpath(
        origin, sys_prefix
    )  # bajo sys.prefix por COMPONENTES (el descenso O_NOFOLLOW prueba no-symlink)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        probs.append(f"{module}: origin {origin!r} fuera de sys.prefix {sys_prefix!r}")
        return probs, None
    rel_posix = rel.replace(os.sep, "/")
    sha, gprob = _governed_read_under_prefix(sys_prefix, rel_posix, uid)
    if gprob is not None:
        probs.append(f"{module}: {gprob}")
        return probs, None
    # pertenencia REAL: el origin debe estar en el RECORD de AL MENOS UN provider (owners = quiénes lo declaran).
    origin_real = os.path.realpath(origin)
    owners: list[str] = []
    for p in providers:
        files = provider_files.get(p)
        if files is None:
            continue  # sin RECORD: ese provider no puede reclamar el origen (no es fatal si otro sí lo hace)
        if origin_real in {os.path.realpath(f) for f in files}:
            owners.append(_norm(p))
    if not owners:
        probs.append(f"{module}: origin no pertenece al RECORD de ningún provider {want}")
    if probs or sha is None:
        return probs, None
    return probs, ImportIdentity(
        module=module,
        distribution=primary,
        providers=tuple(want),
        origin=rel_posix,
        origin_sha256=sha,
        origin_owners=tuple(sorted(owners)),
    )
