#!/usr/bin/env python
"""B286-A: substrate de UN SOLO snapshot gobernado (P0R.5). Los gates de gobernanza leían sus entradas por RUTA
(cada uno con su propia apertura), lo que multiplicaba la superficie TOCTOU. `GovernanceSnapshot` centraliza la lectura
gobernada — anclada en `/`, descenso `openat` componente a componente, invariantes de directorio, leaf regular con modo
exacto, UN SOLO `fstat` por checkpoint (validar==sellar, B293), identidad COMPLETA en la revalidación final (B288),
límites de tamaño y errores de cierre superficiados (B282) — y RETIENE los bytes+identidad sellados para que ningún
consumidor reabra por ruta.

Stdlib-only y sin efectos secundarios (no importa ningún módulo de autoridad). Frontera honesta: evita symlinks, objetos
especiales, rebind visible y mutación de inode DURANTE la lectura; NO es una instantánea criptográfica contra un proceso
hostil root/uid-actual que alterne y restaure el árbol entre checkpoints — la autoridad externa definitiva es el ruleset
(B291) + la revisión del diff.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from contextlib import AbstractContextManager
from dataclasses import dataclass

_CONTRACT_MAX_BYTES = 1 << 20  # 1 MiB
_AUTHORITY_MAX_BYTES = 4 << 20  # 4 MiB
_SOURCE_MAX_BYTES = 4 << 20  # 4 MiB
_SNAPSHOT_TOTAL_MAX_BYTES = 64 << 20  # 64 MiB (suma de todo lo leído por la instancia)
_DIR_GO_WRITE = 0o022  # bits de escritura grupo/otros — prohibidos en directorios gobernados


class GovernanceSnapshotError(Exception):
    """Fallo fail-closed de una lectura gobernada (identidad/modo/tamaño/cierre/ruta)."""


@dataclass(frozen=True)
class StatSnapshot:
    dev: int
    ino: int
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def of(cls, st: os.stat_result) -> StatSnapshot:
        return cls(st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def identity(self) -> tuple:
        """B288: identidad COMPLETA para revalidar (no sólo dev/ino)."""
        return (self.dev, self.ino, self.mode, self.uid, self.nlink, self.ctime_ns)


@dataclass(frozen=True)
class GovernedEntry:
    rel: str
    data: bytes
    sha256: str
    stat: StatSnapshot


def _rel_parts(rel: str) -> list[str] | None:
    """Gramática POSIX relativa CERRADA: sin NUL, no absoluta, sin `.`/`..`, sin componentes vacíos (ni doble slash)."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts


def _dir_problem(comp: str, st: os.stat_result, uid: int) -> str | None:
    """B282: invariantes de un directorio de la cadena: real, sin escritura g/o (caza 0777/0775), dueño root/uid-actual,
    sin setuid/setgid."""
    if not stat.S_ISDIR(st.st_mode):
        return f"componente {comp!r} no es un directorio"
    if st.st_mode & _DIR_GO_WRITE:
        return f"directorio {comp!r} escribible por grupo/otros (modo {oct(stat.S_IMODE(st.st_mode))})"
    if st.st_uid not in (0, uid):
        return f"directorio {comp!r} con dueño uid {st.st_uid} (ni root ni el actual)"
    if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
        return f"directorio {comp!r} con setuid/setgid"
    return None


class GovernanceSnapshot(AbstractContextManager):
    """Lector gobernado inyectable (root por defecto = raíz del repo). `read()` produce un `GovernedEntry` sellado;
    `tracked()` lista ficheros versionados; `reverify()` re-lee lo cacheado y exige identidad+bytes idénticos."""

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root)
        self._uid = os.getuid()
        self._cache: dict[str, GovernedEntry] = {}
        self._total = 0

    def __exit__(self, *exc: object) -> None:
        self._cache.clear()

    # -- lectura gobernada -------------------------------------------------
    def read(self, rel: str, *, exact_mode: int = 0o644, max_bytes: int = _SOURCE_MAX_BYTES) -> GovernedEntry:
        if rel in self._cache:
            return self._cache[rel]
        entry, err = self._read_once(rel, exact_mode=exact_mode, max_bytes=max_bytes)
        if entry is None:
            raise GovernanceSnapshotError(err or f"{rel}: lectura gobernada fallida")
        self._total += entry.stat.size
        if self._total > _SNAPSHOT_TOTAL_MAX_BYTES:
            raise GovernanceSnapshotError(f"{rel}: el snapshot total excede {_SNAPSHOT_TOTAL_MAX_BYTES} (B282)")
        self._cache[rel] = entry
        return entry

    def _read_once(self, rel: str, *, exact_mode: int, max_bytes: int) -> tuple[GovernedEntry | None, str | None]:
        parts = _rel_parts(rel)
        if parts is None:
            return None, f"{rel}: ruta relativa POSIX inválida (fail-closed)"
        dir_comps = [p for p in self._root.split("/") if p] + parts[:-1]
        leaf = parts[-1]
        all_fds: list[int] = []
        ancestors: list[tuple[str, int, int, os.stat_result]] = []  # (nombre, parent_fd, dir_fd, fstat SELLADO)
        primary: str | None = None
        entry: GovernedEntry | None = None
        try:
            try:
                root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)  # la raíz del fs no es symlink
            except OSError as exc:
                return None, f"{rel}: '/' no abrible como directorio ({exc}) (B281)"
            all_fds.append(root_fd)
            cur = root_fd
            for comp in dir_comps:
                try:
                    nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cur)
                except OSError as exc:
                    primary = f"{rel}: componente {comp!r} no es directorio no-symlink abrible ({exc}) (B281)"
                    break
                all_fds.append(nfd)
                st_dir = os.fstat(nfd)  # B293: UN SOLO fstat — se valida y sella el MISMO objeto
                dprob = _dir_problem(comp, st_dir, self._uid)
                if dprob is not None:
                    primary = f"{rel}: {dprob} (B282)"
                    break
                ancestors.append((comp, cur, nfd, st_dir))
                cur = nfd
            if primary is None:
                try:
                    lfd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=cur)
                except OSError as exc:
                    primary = f"{rel}: leaf {leaf!r} no abrible sin seguir symlink ({exc}) (B274)"
                else:
                    all_fds.append(lfd)
                    primary, entry = self._govern_leaf(rel, leaf, lfd, cur, exact_mode, max_bytes, ancestors)
        finally:
            close_errors: list[str] = []
            for fd in reversed(all_fds):
                try:
                    os.close(fd)
                except OSError as exc:
                    close_errors.append(str(exc))
        if close_errors:  # B282: un cierre fallido invalida un éxito potencial y se ADJUNTA al error primario
            joined = "; ".join(close_errors)
            return None, ((primary + " | " if primary else "") + f"{rel}: fallo al cerrar fd(s): {joined} (B282)")
        if primary is not None:
            return None, primary
        return entry, None

    def _govern_leaf(self, rel, leaf, lfd, parent_fd, exact_mode, max_bytes, ancestors):
        st0 = os.fstat(lfd)  # B293: el MISMO objeto valida y (más abajo) se compara con la re-lectura
        if not stat.S_ISREG(st0.st_mode):
            return f"{rel}: no es un fichero regular (B274)", None
        if st0.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            return f"{rel}: bits especiales setuid/setgid/sticky (B274)", None
        if stat.S_IMODE(st0.st_mode) != exact_mode:
            return f"{rel}: modo {oct(stat.S_IMODE(st0.st_mode))} != {oct(exact_mode)} exacto (B274)", None
        if st0.st_uid != self._uid:
            return f"{rel}: uid {st0.st_uid} != {self._uid} actual (B274)", None
        if st0.st_nlink != 1:
            return f"{rel}: nlink {st0.st_nlink} != 1 (hardlink) (B274)", None
        if st0.st_size > max_bytes:
            return f"{rel}: tamaño {st0.st_size} > máximo {max_bytes} (B282)", None
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(lfd, 1 << 16)
            except OSError as exc:
                return f"{rel}: error de lectura ({exc}) (B274)", None
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return f"{rel}: excede el máximo {max_bytes} durante la lectura (B282)", None
            chunks.append(chunk)
        data = b"".join(chunks)
        st1 = StatSnapshot.of(os.fstat(lfd))
        snap0 = StatSnapshot.of(st0)
        if (snap0.dev, snap0.ino, snap0.size, snap0.mtime_ns, snap0.ctime_ns, snap0.mode, snap0.uid, snap0.nlink) != (
            st1.dev, st1.ino, st1.size, st1.mtime_ns, st1.ctime_ns, st1.mode, st1.uid, st1.nlink,
        ):  # fmt: skip
            return f"{rel}: el inode del leaf cambió durante la lectura (B274)", None
        if len(data) != snap0.size:
            return f"{rel}: tamaño leído {len(data)} != fstat {snap0.size} (B274)", None
        for name, pfd, dfd, fst0 in ancestors:  # B288: identidad COMPLETA del ancestro (re-fstat + re-stat por nombre)
            try:
                dnow = StatSnapshot.of(os.fstat(dfd))
            except OSError as exc:
                return f"{rel}: ancestro {name!r} no re-fstat-able ({exc}) (B288)", None
            if dnow.identity() != StatSnapshot.of(fst0).identity():
                return f"{rel}: el ancestro {name!r} cambió de identidad durante la lectura (B288)", None
            try:
                byname = StatSnapshot.of(os.stat(name, dir_fd=pfd, follow_symlinks=False))
            except OSError as exc:
                return f"{rel}: ancestro {name!r} no re-stat-able ({exc}) (B274)", None
            if byname.identity() != StatSnapshot.of(fst0).identity():
                return f"{rel}: el ancestro {name!r} cambió de nombre↔identidad durante la lectura (B288)", None
        try:
            leafname = StatSnapshot.of(os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False))
        except OSError as exc:
            return f"{rel}: leaf {leaf!r} no re-stat-able ({exc}) (B274)", None
        if leafname.identity() != snap0.identity():
            return f"{rel}: el leaf {leaf!r} cambió de identidad durante la lectura (B288)", None
        return None, GovernedEntry(rel=rel, data=data, sha256=hashlib.sha256(data).hexdigest(), stat=snap0)

    # -- inventario y revalidación ----------------------------------------
    def tracked(self, pattern: str = "*") -> tuple[str, ...]:
        """B286: ficheros versionados que casan `pattern`, vía `git -C ROOT ls-files` (independiente del cwd)."""
        try:
            out = subprocess.run(
                ["git", "-C", self._root, "ls-files", "-z", "--", pattern], capture_output=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GovernanceSnapshotError(f"git ls-files falló ({exc}) (fail-closed B286)") from exc
        if out.returncode != 0:
            raise GovernanceSnapshotError(f"git ls-files rc={out.returncode} (fail-closed B286)")
        return tuple(x.decode("utf-8") for x in out.stdout.split(b"\x00") if x)

    def reverify(self) -> None:
        """B286: re-lee (gobernado) cada entrada cacheada y exige identidad+bytes idénticos a lo sellado. Fail-closed."""
        for rel, sealed in list(self._cache.items()):
            fresh, err = self._read_once(rel, exact_mode=stat.S_IMODE(sealed.stat.mode), max_bytes=_SOURCE_MAX_BYTES)
            if fresh is None:
                raise GovernanceSnapshotError(f"reverify {rel}: {err}")
            if fresh.sha256 != sealed.sha256 or fresh.stat.identity() != sealed.stat.identity():
                raise GovernanceSnapshotError(f"reverify {rel}: bytes/identidad cambiaron desde el sellado (B286)")
