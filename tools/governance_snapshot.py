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

import contextlib
import enum
import hashlib
import os
import selectors
import signal
import stat
import subprocess
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass

_CONTRACT_MAX_BYTES = 1 << 20  # 1 MiB
_AUTHORITY_MAX_BYTES = 4 << 20  # 4 MiB
_SOURCE_MAX_BYTES = 4 << 20  # 4 MiB
_SNAPSHOT_TOTAL_MAX_BYTES = 64 << 20  # 64 MiB (suma de todo lo leído por la instancia)
_DIR_GO_WRITE = 0o022  # bits de escritura grupo/otros — prohibidos en directorios gobernados
_NEW, _OPEN, _CLOSED = "new", "open", "closed"  # B298: estados del ciclo de vida de un solo uso
# B303: ejecutable git ABSOLUTO y gobernado — NUNCA "git" por PATH ni `shutil.which` (fake git falsificaría el inventario).
_GIT_ABS = "/usr/bin/git"  # ruta certificada en Linux y macOS
# B303: entorno hijo por ALLOWLIST (no filtrado subtractivo) — sólo lo mínimo determinista, sin heredar GIT_*/XDG/PYTHON*.
_GIT_CHILD_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",  # B306: sin locks opcionales
    "PATH": "/usr/bin:/bin",  # sólo para helpers internos inevitables de git, no para localizarlo
}
# B306: prefijo de config por línea de comando — PRECEDE a system/global/local/includes y neutraliza la config que
# ejecuta programas durante `rev-parse`/`ls-files` (core.fsmonitor RCE, untrackedCache, preloadIndex).
_GIT_CONFIG_ARGS = (
    "--no-optional-locks",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.preloadIndex=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
)  # fmt: skip
_GIT_STDERR_MAX_BYTES = 1 << 16  # 64 KiB de stderr basta para diagnóstico; más aborta
_GIT_TIMEOUT_S = 30.0
# B306: operaciones CERRADAS — el caller NUNCA suministra argv/subcomando/pathspec.
_GIT_OPS = {
    "TOPLEVEL": ("rev-parse", "--show-toplevel"),
    "TRACKED_INVENTORY": ("ls-files", "-z", "--"),
}
_ALLOWED_MODES = frozenset({0o644, 0o600})  # B296: conjunto cerrado de modos exactos aprobados
_CATEGORY_CAPS = {  # B296: cada categoría fija su cota superior; una categoría estricta NUNCA se satisface con una laxa
    "contract": _CONTRACT_MAX_BYTES,
    "authority": _AUTHORITY_MAX_BYTES,
    "source": _SOURCE_MAX_BYTES,
}


class GovernanceSnapshotError(Exception):
    """Fallo fail-closed de una lectura gobernada (identidad/modo/tamaño/cierre/ruta/política/ciclo de vida)."""


class _GroupState(enum.Enum):
    """B318: estado TRI-VALUADO del grupo de proceso. `UNKNOWN` NUNCA equivale a limpio (fail-closed)."""

    ABSENT = "absent"  # el grupo ya no existe (ProcessLookupError)
    PRESENT = "present"  # queda al menos un proceso vivo en el grupo
    UNKNOWN = "unknown"  # no señalizable/error de sonda (PermissionError u otro OSError) → fail-closed


@dataclass(frozen=True, slots=True)
class _ProcessIssue:
    """B314/B315/B318: incidencia ESTRUCTURADA de una fase del ciclo de vida del proceso gobernado. Se ACUMULA
    (nunca reemplaza el error primario); el texto sólo se materializa al clasificar el resultado final."""

    phase: str  # adquisición | terminación | reap | reconciliación | cleanup
    operation: str  # p.ej. killpg-TERM, wait, selector-close, close-stdout
    detail: str

    def __str__(self) -> str:
        return f"{self.phase}/{self.operation}: {self.detail}"


@dataclass(frozen=True)
class ReadPolicy:
    """B296: política de lectura INMUTABLE y con tipos cerrados. La caché se liga a `(rel, policy)`: una relectura con
    política distinta FALLA en vez de devolver bytes sellados bajo otra política. Sin coerciones (`bool`/`float`/`NaN`
    rechazados por identidad de tipo)."""

    exact_mode: int
    max_bytes: int
    category: str

    def __post_init__(self) -> None:
        if type(self.exact_mode) is not int:  # bool es subclase de int → lo rechaza `is not int`
            raise GovernanceSnapshotError(f"exact_mode debe ser int exacto, no {type(self.exact_mode).__name__} (B296)")
        if self.exact_mode not in _ALLOWED_MODES:
            raise GovernanceSnapshotError(f"exact_mode {self.exact_mode!r} fuera del conjunto {sorted(_ALLOWED_MODES)} (B296)")  # fmt: skip
        if type(self.max_bytes) is not int:  # float/NaN/bool rechazados por tipo antes de comparar
            raise GovernanceSnapshotError(f"max_bytes debe ser int exacto, no {type(self.max_bytes).__name__} (B296)")
        if self.category not in _CATEGORY_CAPS:
            raise GovernanceSnapshotError(f"categoría {self.category!r} no está en {sorted(_CATEGORY_CAPS)} (B296)")
        cap = _CATEGORY_CAPS[self.category]
        if not (0 < self.max_bytes <= cap):
            raise GovernanceSnapshotError(f"max_bytes {self.max_bytes!r} fuera de (0, {cap}] para categoría {self.category!r} (B296)")  # fmt: skip


_TRACKED_KINDS = frozenset({"prefix", "suffix", "exact"})


def _tracked_query_problem(kind: object, value: object) -> str | None:
    """B304/B307: ÚNICA definición TOTAL de la gramática de query. La usan `TrackedQuery.__post_init__` Y `tracked()` (no
    una validación corta en un lado y otra larga en el otro). Toda entrada inválida → mensaje; None si es válida."""
    if type(kind) is not str or kind not in _TRACKED_KINDS:
        return f"kind inválido {kind!r} (B304)"
    if type(value) is not str or not value or "\x00" in value:
        return "value debe ser str no vacío sin NUL (B304)"
    if kind == "exact":
        if _rel_parts(value) is None:
            return f"exact {value!r} no es ruta relativa POSIX (B304)"
    elif kind == "prefix":  # B307: directorio POSIX EXPLÍCITO que termina en `/` — `.` no es selector de dotfiles
        if not value.endswith("/"):
            return f"prefix {value!r} debe terminar en '/' (directorio POSIX explícito) (B304)"
        if _rel_parts(value[:-1]) is None:
            return f"prefix {value!r} inválido (abs/traversal/vacío/`.`) (B304)"
    else:  # suffix: sin slash, no `.`/`..`, y una extensión EXPLÍCITA (contiene un punto)
        if "/" in value or value in (".", "..") or "." not in value:
            return f"suffix {value!r} inválido (sin slash, no `.`/`..`, extensión explícita) (B304)"
    return None


@dataclass(frozen=True, slots=True)
class TrackedQuery:
    """B302/B304/B307: consulta CERRADA del inventario versionado — DATOS EXACTOS `(kind, value)`, sin despacho virtual.
    El matching lo hace una función interna (`_tracked_match`); `tracked()` exige `type(query) is TrackedQuery` Y
    revalida la gramática COMPLETA con `_tracked_query_problem` (una instancia forjada por `object.__new__`+
    `object.__setattr__` NO cuela). Se filtra en memoria — jamás un pathspec del caller llega a git."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if type(self) is not TrackedQuery:  # B304: sin subclases
            raise GovernanceSnapshotError("TrackedQuery no admite subclases (B304)")
        prob = _tracked_query_problem(self.kind, self.value)
        if prob is not None:
            raise GovernanceSnapshotError(f"TrackedQuery: {prob}")


def _tracked_match(kind: str, value: str, path: str) -> bool:
    """B304: matching INTERNO por (kind, value) validados — nunca un método virtual del objeto recibido."""
    if kind == "exact":
        return path == value
    if kind == "prefix":
        return path.startswith(value)
    return path.endswith(value)


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
        if type(root) is not str:  # B304: SÓLO str exacto — sin bytes, PathLike, subclases ni coerción `__fspath__`
            raise GovernanceSnapshotError(f"root debe ser str exacto, no {type(root).__name__} (B304)")
        if not root or "\x00" in root:
            raise GovernanceSnapshotError("root vacío o con NUL (B304)")
        try:
            self._root = os.path.abspath(root)  # root es str puro → no ejecuta código de caller
        except (OSError, ValueError) as exc:
            raise GovernanceSnapshotError(f"root no normalizable ({exc}) (B304)") from exc
        if type(self._root) is not str or "\x00" in self._root:
            raise GovernanceSnapshotError("root cambió de tipo o tiene NUL tras normalizar (B304)")
        self._uid = os.getuid()
        self._cache: dict[str, tuple[ReadPolicy, GovernedEntry]] = {}  # B296: sellado por (rel → política, entrada)
        self._total = 0
        self._state = _NEW  # B298: NEW -> OPEN -> CLOSED, un solo uso
        self._inventory: tuple[str, ...] | None = None  # B301: inventario git SELLADO (una captura por snapshot)
        self._inventory_sha: str | None = None  # B303: sha de los bytes CRUDOS de ls-files
        self._git_ident: tuple | None = None  # B303: identidad gobernada del ejecutable ligada al sello
        self._captures = 0  # B301: contador de capturas git (una al sellar + una al revalidar; jamás una por consumer)

    def __enter__(self) -> GovernanceSnapshot:  # B298: sólo se entra desde NEW; una instancia CERRADA no renace
        if self._state is not _NEW:
            raise GovernanceSnapshotError(f"GovernanceSnapshot no reutilizable: estado {self._state} (esperado {_NEW}) (B298)")  # fmt: skip
        self._state = _OPEN
        return self

    def __exit__(self, *exc: object) -> None:
        self._state = _CLOSED  # B298: __exit__ SIEMPRE cierra, aunque el cuerpo eleve; caché e inventario se descartan
        self._cache.clear()
        self._inventory = None  # B301: el inventario sellado queda invalidado al cerrar el contexto
        self._inventory_sha = None
        self._git_ident = None  # B303

    def _require_open(self) -> None:
        if self._state is not _OPEN:
            raise GovernanceSnapshotError(f"operación fuera de un contexto OPEN (estado {self._state}) (B298)")

    # -- lectura gobernada -------------------------------------------------
    def read(
        self, rel: str, *, exact_mode: int = 0o644, max_bytes: int | None = None, category: str = "source"
    ) -> GovernedEntry:
        self._require_open()  # B298: sólo se lee dentro de un contexto OPEN
        # B302: cerrar el contrato de tipos ANTES de tocar caché/diccionarios (un `rel`/`category` no hashable daba
        # TypeError CRUDO en `_cache.get`/`_CATEGORY_CAPS.get`). Toda entrada inválida → GovernanceSnapshotError.
        if type(rel) is not str:
            raise GovernanceSnapshotError(f"rel debe ser str, no {type(rel).__name__} (B302)")
        if _rel_parts(rel) is None:
            raise GovernanceSnapshotError(f"{rel!r}: ruta relativa POSIX inválida (B302)")
        if type(category) is not str:
            raise GovernanceSnapshotError(f"category debe ser str, no {type(category).__name__} (B302)")
        policy = ReadPolicy(exact_mode, _CATEGORY_CAPS.get(category, _SOURCE_MAX_BYTES) if max_bytes is None else max_bytes, category)  # fmt: skip
        cached = self._cache.get(rel)
        if cached is not None:  # B296: la caché se liga a la política — una relectura con política distinta FALLA
            prev_policy, prev_entry = cached
            if prev_policy != policy:
                raise GovernanceSnapshotError(f"{rel}: relectura con política {policy} != la sellada {prev_policy} (B296)")  # fmt: skip
            return prev_entry
        entry, err = self._read_once(rel, policy)
        if entry is None:
            raise GovernanceSnapshotError(err or f"{rel}: lectura gobernada fallida")
        new_total = self._total + entry.stat.size  # B296: calcular ANTES de mutar; un rechazo no envenena el contador
        if new_total > _SNAPSHOT_TOTAL_MAX_BYTES:
            raise GovernanceSnapshotError(f"{rel}: el snapshot total {new_total} excede {_SNAPSHOT_TOTAL_MAX_BYTES} (B282)")  # fmt: skip
        self._total = new_total
        self._cache[rel] = (policy, entry)
        return entry

    def _read_once(self, rel: str, policy: ReadPolicy) -> tuple[GovernedEntry | None, str | None]:
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
                primary = f"{rel}: '/' no abrible como directorio ({exc}) (B281)"
            else:
                all_fds.append(root_fd)
                cur = root_fd
                for comp in dir_comps:
                    try:
                        nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cur)
                    except OSError as exc:
                        primary = f"{rel}: componente {comp!r} no es directorio no-symlink abrible ({exc}) (B281)"
                        break
                    all_fds.append(nfd)
                    try:
                        st_dir = os.fstat(nfd)  # B293: UN SOLO fstat; B299: guardado (un fstat fallido no escapa crudo)
                    except OSError as exc:
                        primary = f"{rel}: fstat del directorio {comp!r} falló ({exc}) (B299)"
                        break
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
                        primary, entry = self._govern_leaf(rel, leaf, lfd, cur, policy, ancestors)
        except OSError as exc:  # B299: red de seguridad — ninguna OSError operacional escapa cruda (KeyboardInterrupt/
            primary = f"{rel}: error de sistema inesperado ({exc}) (B299)"  # SystemExit NO son OSError → propagan
        finally:
            close_errors: list[str] = []
            for fd in reversed(all_fds):  # B299: se cierran TODOS los fds aunque uno falle; siempre se agregan
                try:
                    os.close(fd)
                except OSError as exc:
                    close_errors.append(str(exc))
        # B299: resultado TOTAL — se llega aquí SIEMPRE (la red OSError impide que una excepción salte la agregación).
        if primary is not None:  # error primario: NO se reemplaza; los cierres fallidos se ADJUNTAN
            if close_errors:
                return None, f"{primary} | {rel}: además fallo al cerrar fd(s): {'; '.join(close_errors)} (B282)"
            return None, primary
        if close_errors:  # el camino iba a tener éxito pero un cierre falló → el resultado es FALLO
            return None, f"{rel}: fallo al cerrar fd(s): {'; '.join(close_errors)} (B282)"
        return entry, None

    def _govern_leaf(self, rel, leaf, lfd, parent_fd, policy: ReadPolicy, ancestors):
        exact_mode, max_bytes = policy.exact_mode, policy.max_bytes
        try:
            st0 = os.fstat(lfd)  # B293: el MISMO objeto valida y (más abajo) se compara; B299: guardado
        except OSError as exc:
            return f"{rel}: fstat del leaf falló ({exc}) (B299)", None
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
        try:
            st1 = StatSnapshot.of(os.fstat(lfd))  # B299: el re-fstat final también guardado
        except OSError as exc:
            return f"{rel}: re-fstat del leaf falló ({exc}) (B299)", None
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
    def _governed_git_identity(self) -> tuple:
        """B303: valida `/usr/bin/git` por descenso `openat` (dirs root-owned sin escritura g/o, leaf regular root-owned
        ejecutable sin symlink ni bits especiales) y devuelve su identidad `(dev,ino,mode,uid,nlink,size,mtime_ns,
        ctime_ns)`. B309: resultado TOTAL — NINGÚN `return` dentro del `try` con fds vivos, cierre de TODOS los fds con
        agregación (un cierre fallido sobre un camino exitoso es fail-closed), sin `except OSError: pass`.
        KeyboardInterrupt/SystemExit propagan."""
        parts = [p for p in _GIT_ABS.split("/") if p]
        leaf, dir_comps = parts[-1], parts[:-1]
        fds: list[int] = []
        primary: str | None = None
        identity: tuple | None = None
        try:
            cur = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            fds.append(cur)
            for comp in dir_comps:
                nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cur)
                fds.append(nfd)
                st = os.fstat(nfd)
                if st.st_uid != 0 or (st.st_mode & _DIR_GO_WRITE) or (st.st_mode & (stat.S_ISUID | stat.S_ISGID)):
                    primary = f"git: directorio {comp!r} no gobernado (root/no-g-o-write) (B303)"
                    break
                cur = nfd
            if primary is None:
                lfd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cur)
                fds.append(lfd)
                st = os.fstat(lfd)
                if not stat.S_ISREG(st.st_mode):
                    primary = f"git {_GIT_ABS!r} no es fichero regular (B303)"
                elif st.st_uid != 0 or (st.st_mode & _DIR_GO_WRITE) or (st.st_mode & (stat.S_ISUID | stat.S_ISGID)):
                    primary = f"git {_GIT_ABS!r} no root-owned / escribible g-o / setuid (B303)"
                elif not (st.st_mode & 0o100):  # ejecutable por el dueño
                    primary = f"git {_GIT_ABS!r} no ejecutable (B303)"
                else:
                    byname = os.stat(leaf, dir_fd=cur, follow_symlinks=False)  # nombre↔identidad (git es multi-call)
                    if (byname.st_dev, byname.st_ino) != (st.st_dev, st.st_ino):
                        primary = f"git {_GIT_ABS!r} cambió de nombre↔identidad (B303)"
                    else:
                        identity = (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)  # fmt: skip
        except OSError as exc:  # B309: red de seguridad; KeyboardInterrupt/SystemExit NO son OSError → propagan
            primary = f"git {_GIT_ABS!r} no gobernable ({exc}) (B303)"
        finally:
            close_errors: list[str] = []
            for fd in reversed(fds):  # B309: cierra TODOS aunque uno falle; agrega — nunca `except OSError: pass`
                try:
                    os.close(fd)
                except OSError as exc:
                    close_errors.append(str(exc))
        if primary is not None:  # B309: error primario preservado, cierres ADJUNTOS
            raise GovernanceSnapshotError(primary + (f" | cierres: {'; '.join(close_errors)} (B309)" if close_errors else ""))  # fmt: skip
        if close_errors:  # B309: camino exitoso pero un cierre falló → fail-closed
            raise GovernanceSnapshotError(f"git: fallo al cerrar fd(s): {'; '.join(close_errors)} (B309)")
        if identity is None:  # defensivo: no debería ocurrir sin primary
            raise GovernanceSnapshotError("git: identidad no derivada (B309)")
        return identity

    def _run_git(self, op: str, out_limit: int) -> bytes:
        """B303/B306: ejecuta una OPERACIÓN CERRADA (`op` ∈ `_GIT_OPS`; el caller nunca da argv/pathspec) del git ABSOLUTO
        gobernado con prefijo de config que desactiva `core.fsmonitor`/hooks/pager, entorno allowlist, stdin DEVNULL,
        close_fds y lectura ACOTADA con deadline. Revalida la identidad del ejecutable ANTES y DESPUÉS."""
        if op not in _GIT_OPS:
            raise GovernanceSnapshotError(f"operación git no permitida {op!r} (B306)")
        ident_before = self._governed_git_identity()
        argv = [_GIT_ABS, *_GIT_CONFIG_ARGS, "-C", self._root, *_GIT_OPS[op]]
        stdout = self._run_bounded(argv, out_limit)
        if self._governed_git_identity() != ident_before:  # rebind del ejecutable durante la ejecución
            raise GovernanceSnapshotError("git cambió de identidad durante la ejecución (B303)")
        return stdout

    def _run_bounded(self, argv: list[str], out_limit: int) -> bytes:
        """B306/B311/B312/B313/B314/B315/B318: runner TOTAL, PORTABLE y de ADQUISICIÓN/LIMPIEZA COMPLETA. El selector se
        construye ANTES de `Popen` (B314: si falla, no hay hijo que limpiar); el ciclo entero se envuelve en una captura
        `BaseException` cuyo ÚNICO fin es ejecutar cleanup y reelevar (B315: `KeyboardInterrupt`/`SystemExit` propagan tras
        limpiar). `Popen` en SESIÓN/GRUPO privado (`start_new_session=True`), stdin DEVNULL, close_fds; espera con
        `selectors.DefaultSelector` (epoll/kqueue, sin techo FD_SETSIZE) sobre fds NO bloqueantes; deadline monotónico;
        límites de stdout/stderr. El cleanup es TOTAL (`_cleanup_process`): cada paso propio, ninguna instrucción sin guard
        puede cortar las siguientes; toda salida anormal exige terminación del grupo; el grupo se verifica INCLUSO tras
        `rc=0` (B315: un nieto residual se termina y falla). El error PRIMARIO se preserva y las incidencias se ADJUNTAN."""
        sel: selectors.BaseSelector | None = None
        proc: subprocess.Popen[bytes] | None = None
        out_chunks: list[bytes] = []
        counts = {"out": 0, "err": 0}
        problem: str | None = None
        primary: BaseException | None = None
        event_loop_complete = False
        try:
            try:  # B314: adquirir el selector ANTES del proceso; su fallo NO deja hijo
                sel = selectors.DefaultSelector()
            except (OSError, ValueError, OverflowError) as exc:
                raise GovernanceSnapshotError(f"selector no construible ({exc}) (fail-closed B314)") from exc
            try:
                proc = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=dict(_GIT_CHILD_ENV), close_fds=True, start_new_session=True,  # B312: grupo/sesión privados
                )  # fmt: skip
            except OSError as exc:
                raise GovernanceSnapshotError(f"git no ejecutable ({exc}) (fail-closed B306)") from exc
            assert proc.stdout is not None and proc.stderr is not None
            for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
                os.set_blocking(stream.fileno(), False)  # B311: no bloqueante; BlockingIOError = sin datos ahora
                sel.register(stream.fileno(), selectors.EVENT_READ, tag)
            deadline = time.monotonic() + _GIT_TIMEOUT_S
            open_fds = 2
            while open_fds and problem is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    problem = "timeout"
                    break
                events = sel.select(remaining)
                if not events:
                    problem = "timeout"
                    break
                for key, _mask in events:
                    try:
                        data = os.read(key.fd, 1 << 16)
                    except BlockingIOError:
                        continue  # B311: sin datos disponibles, no es error terminal
                    if not data:
                        sel.unregister(key.fd)
                        open_fds -= 1
                        continue
                    counts[key.data] += len(data)
                    if key.data == "out":
                        if counts["out"] > out_limit:
                            problem = "stdout excede el límite"
                            break
                        out_chunks.append(data)
                    elif counts["err"] > _GIT_STDERR_MAX_BYTES:
                        problem = "stderr excede el límite"
                        break
            event_loop_complete = problem is None
        except GovernanceSnapshotError as exc:  # taxonomía propia (selector/Popen) → limpiar y reelevar tal cual
            primary = exc
        except (
            OSError,
            ValueError,
            OverflowError,
        ) as exc:  # B311: red de seguridad total del bucle → problem (adjuntable)
            problem = f"runner falló ({exc})"
        except BaseException as exc:  # B315: KI/SE/inesperado → limpiar y reelevar el MISMO objeto
            primary = exc
        finally:
            # B314/B315/B318: cleanup TOTAL. `must_terminate` NUNCA se deriva sólo de `problem`: cualquier primario,
            # cualquier error, o un bucle incompleto exigen terminar el grupo (§5.4 del plan).
            must_terminate = primary is not None or problem is not None or not event_loop_complete
            rc, issues = self._cleanup_process(sel, proc, must_terminate=must_terminate)
        suffix = f" | incidencias: {'; '.join(str(i) for i in issues)}" if issues else ""
        if primary is not None:  # B315: el primario manda; KI/SE se reelevan intactos (con notas de incidencias)
            for issue in issues:
                primary.add_note(str(issue))
            raise primary
        if problem is not None:  # B313: primario del bucle preservado, incidencias ADJUNTAS (nunca lo reemplazan)
            raise GovernanceSnapshotError(f"git {argv[-1]}: {problem}{suffix} (fail-closed B306)")
        if issues:  # camino feliz pero cleanup incompleto → fail-closed
            raise GovernanceSnapshotError(f"git {argv[-1]}: cleanup incompleto{suffix} (fail-closed B313)")
        if rc != 0:
            raise GovernanceSnapshotError(f"git {argv[-1]} rc={rc} (fail-closed B306)")
        return b"".join(out_chunks)

    def _cleanup_process(
        self, sel: selectors.BaseSelector | None, proc: subprocess.Popen[bytes] | None, *, must_terminate: bool
    ) -> tuple[int | None, list[_ProcessIssue]]:
        """B314/B315/B318: cleanup TOTAL del proceso gobernado. Ejecuta TODOS los pasos aunque cualquiera falle: (1)
        terminar/reconciliar el grupo + reap del hijo; (2) desregistrar fds restantes; (3) cerrar el selector; (4) cerrar
        stdout; (5) cerrar stderr. Cada paso tiene su propio guard y ACUMULA una `_ProcessIssue`; NINGÚN cleanup desnudo
        puede cortar los siguientes, y NO existe `except ...: pass`. Devuelve `(rc, incidencias)`."""
        issues: list[_ProcessIssue] = []
        rc: int | None = None
        if proc is not None:  # pasos 1-3 del plan §5.3: terminar grupo, reconciliar, reap del hijo directo
            rc, _final_state, group_issues = self._finish_process_group(proc, proc.pid, terminate=must_terminate)
            issues.extend(group_issues)
        if sel is not None:  # paso 4: desregistrar fds vivos antes de cerrar el selector
            try:
                for key in list(sel.get_map().values()):
                    try:
                        sel.unregister(key.fd)
                    except (KeyError, ValueError, OSError) as exc:
                        issues.append(_ProcessIssue("cleanup", "unregister", str(exc)))
            except (KeyError, ValueError, OSError, RuntimeError) as exc:
                issues.append(_ProcessIssue("cleanup", "get_map", str(exc)))
            try:  # paso 5: cerrar el selector — su fallo NO impide cerrar los pipes
                sel.close()
            except (OSError, ValueError) as exc:
                issues.append(_ProcessIssue("cleanup", "selector-close", str(exc)))
        if proc is not None:  # pasos 6-7: cerrar cada pipe de forma INDEPENDIENTE (uno no bloquea al otro)
            for stream, tag in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError) as exc:
                        issues.append(_ProcessIssue("cleanup", f"close-{tag}", str(exc)))
        return rc, issues

    @staticmethod
    def _group_state(pgid: int) -> tuple[_GroupState, _ProcessIssue | None]:
        """B318: estado TRI-VALUADO del grupo. `killpg(pgid, 0)` no señala, sólo consulta. `ProcessLookupError`→ABSENT;
        éxito→PRESENT; `PermissionError` u otro `OSError`→UNKNOWN + incidencia (jamás se descarta; UNKNOWN ≠ limpio)."""
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return _GroupState.ABSENT, None
        except (PermissionError, OSError) as exc:
            return _GroupState.UNKNOWN, _ProcessIssue("sonda", "killpg0", str(exc))
        return _GroupState.PRESENT, None

    def _finish_process_group(
        self, proc: subprocess.Popen[bytes], pgid: int, *, terminate: bool
    ) -> tuple[int | None, _GroupState, list[_ProcessIssue]]:
        """B312/B315/B318: termina el GRUPO privado (TERM→grace→KILL) y recolecta al hijo con taxonomía TOTAL. Nunca
        señala el grupo del auditor (`pgid == proc.pid != os.getpgrp()`). Verifica el grupo INCLUSO tras `rc=0`: un
        descendiente residual se termina y se reporta como `descendientes-inesperados`. JAMÁS eleva ni usa `except: pass`;
        toda `killpg` alimenta una incidencia/estado. Devuelve `(rc, estado_final, incidencias)`."""
        issues: list[_ProcessIssue] = []
        isolated = pgid == proc.pid and pgid != os.getpgrp()

        def _signal(sig: int, op: str) -> None:  # killpg gobernado: ABSENT se ignora, cualquier otro error se ACUMULA
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return  # el grupo ya no existe → terminación efectiva, no es un error de cleanup
            except OSError as exc:
                issues.append(_ProcessIssue("terminación", op, str(exc)))

        def _grace_probe(deadline_s: float) -> _GroupState:  # espera activa: reap del padre + sonda del grupo
            end = time.monotonic() + deadline_s
            state = _GroupState.UNKNOWN
            while time.monotonic() < end:
                with contextlib.suppress(OSError, ValueError):
                    proc.poll()  # reap best-effort del padre (zombie) para que el grupo pueda vaciarse; la sonda decide
                state, issue = self._group_state(pgid)
                if issue is not None:
                    issues.append(issue)
                if state == _GroupState.ABSENT:
                    return state
                time.sleep(0.02)
            return state

        if terminate:  # terminación PROACTIVA del grupo ante cualquier salida anormal
            if not isolated:
                issues.append(_ProcessIssue("terminación", "aislamiento", f"pgid {pgid} no aislado (== auditor)"))
            else:
                _signal(signal.SIGTERM, "killpg-TERM")
                if _grace_probe(2.0) != _GroupState.ABSENT:
                    _signal(signal.SIGKILL, "killpg-KILL")
        try:  # reap del hijo directo con deadline; sin excepción cruda
            rc: int | None = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal(signal.SIGKILL, "killpg-KILL")
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rc = None
                issues.append(_ProcessIssue("reap", "wait", "el hijo no terminó tras SIGKILL"))
            except OSError as exc:
                rc = None
                issues.append(_ProcessIssue("reap", "wait", str(exc)))
        except OSError as exc:
            rc = None
            issues.append(_ProcessIssue("reap", "wait", str(exc)))
        # B315: verificación/reconciliación INCLUSO en éxito — un nieto que daemoniza en el grupo no deja trabajo
        # residual. Cualquier estado != ABSENT (PRESENT o UNKNOWN — la sonda pudo fallar) exige intento de terminación
        # de mejor esfuerzo Y fail-closed: UNKNOWN nunca equivale a limpio.
        final_state, issue = self._group_state(pgid)
        if issue is not None:
            issues.append(issue)
        if isolated and final_state != _GroupState.ABSENT:
            if not terminate and final_state == _GroupState.PRESENT:  # descendientes tras rc en el camino feliz
                issues.append(
                    _ProcessIssue("reconciliación", "descendientes-inesperados", "grupo con descendientes tras rc")
                )
            _signal(signal.SIGTERM, "killpg-TERM")
            final_state = _grace_probe(1.0)
            if final_state != _GroupState.ABSENT:
                _signal(signal.SIGKILL, "killpg-KILL")
                final_state = _grace_probe(1.0)
            if final_state != _GroupState.ABSENT:  # tras TERM→KILL sigue sin desaparecer (o la sonda no resuelve)
                issues.append(_ProcessIssue("reconciliación", "grupo", f"grupo no ausente: {final_state.value}"))
        elif not isolated and final_state == _GroupState.UNKNOWN:  # sin aislamiento no señalamos; sólo reportamos
            issues.append(_ProcessIssue("reconciliación", "grupo", "estado de grupo desconocido"))
        return rc, final_state, issues

    def _capture_inventory(self) -> tuple[tuple[str, ...], str, tuple]:
        """B301/B303: UNA captura del inventario versionado COMPLETO (sin pathspec del caller) vía el git ABSOLUTO
        gobernado, tras verificar toplevel == ROOT EXACTO (sin `realpath`, que equipararía un root symlink). El sello se
        liga a `(paths, sha256(bytes CRUDOS de ls-files), identidad gobernada del ejecutable)`. Decodificada fail-closed,
        cada ruta validada, única y en orden canónico. Incrementa el contador de capturas."""
        self._captures += 1
        git_ident = self._governed_git_identity()  # identidad del ejecutable ligada al sello
        try:
            toplevel = self._run_git("TOPLEVEL", 1 << 12).decode("utf-8").strip()  # B306: rev-parse acotado a 4 KiB
        except UnicodeDecodeError as exc:
            raise GovernanceSnapshotError(f"git toplevel no-UTF-8 ({exc}) (fail-closed B301)") from exc
        if toplevel != self._root:  # B303: EXACTO — un root symlink (toplevel resuelto) se rechaza
            raise GovernanceSnapshotError(f"git toplevel {toplevel!r} != ROOT {self._root!r} exacto (fail-closed B303)")
        stdout = self._run_git("TRACKED_INVENTORY", _SNAPSHOT_TOTAL_MAX_BYTES + 1)  # B306: ls-files acotado
        if len(stdout) > _SNAPSHOT_TOTAL_MAX_BYTES:  # B301: no materializar un inventario ilimitado
            raise GovernanceSnapshotError(f"git ls-files devolvió más de {_SNAPSHOT_TOTAL_MAX_BYTES} bytes (B301)")
        raw_sha = hashlib.sha256(stdout).hexdigest()  # B303: sha de los BYTES CRUDOS, no del join reconstruido
        try:
            paths = [x.decode("utf-8") for x in stdout.split(b"\x00") if x]
        except UnicodeDecodeError as exc:  # B299: un nombre no-UTF-8 no escapa como UnicodeDecodeError cruda
            raise GovernanceSnapshotError(
                f"git ls-files devolvió un nombre no-UTF-8 ({exc}) (fail-closed B299)"
            ) from exc
        for p in paths:  # B301: cada ruta debe pasar la MISMA gramática relativa que `read`
            if _rel_parts(p) is None:
                raise GovernanceSnapshotError(f"inventario git con ruta inválida {p!r} (fail-closed B301)")
        if len(set(paths)) != len(paths):
            raise GovernanceSnapshotError("inventario git con rutas DUPLICADAS (fail-closed B301)")
        if list(paths) != sorted(paths):
            raise GovernanceSnapshotError("inventario git sin orden canónico (fail-closed B301)")
        return tuple(paths), raw_sha, git_ident

    def _sealed_inventory(self) -> tuple[str, ...]:
        """B301: SELLA el inventario en la PRIMERA consulta y lo reutiliza — toda consulta posterior deriva de la MISMA
        tuple (ningún segundo `git` durante el consumo)."""
        if self._inventory is None:
            inv, raw_sha, git_ident = self._capture_inventory()
            self._inventory = inv
            self._inventory_sha = raw_sha
            self._git_ident = git_ident
        return self._inventory

    def tracked(self, query: TrackedQuery) -> tuple[str, ...]:
        """B286/B301/B302: ficheros versionados que casan la `TrackedQuery` (gramática CERRADA), filtrados EN MEMORIA
        sobre el inventario SELLADO de la instancia (una sola captura git por snapshot; toda consulta deriva de ella)."""
        self._require_open()  # B298
        if type(query) is not TrackedQuery:  # B304: `type is`, no `isinstance` — una subclase no cuela su matching
            raise GovernanceSnapshotError(f"tracked() exige un TrackedQuery exacto, no {type(query).__name__} (B304)")
        try:  # B307: copiar los campos UNA vez a locales inmutables
            kind, value = query.kind, query.value
        except AttributeError as exc:
            raise GovernanceSnapshotError("TrackedQuery sin inicializar (B307)") from exc
        prob = _tracked_query_problem(kind, value)  # B307: la gramática COMPLETA en la frontera, ANTES de tocar git
        if prob is not None:
            raise GovernanceSnapshotError(f"tracked: query forjada rechazada — {prob} (B307)")
        return tuple(p for p in self._sealed_inventory() if _tracked_match(kind, value, p))

    def reverify(self) -> None:
        """B286: re-lee (gobernado) cada entrada cacheada y exige identidad+bytes idénticos a lo sellado. Fail-closed.
        B296: re-lee con EXACTAMENTE la política sellada de cada entrada (no `_SOURCE_MAX_BYTES` genérico).
        B301: si hubo inventario sellado, lo re-captura UNA vez y exige el MISMO sha (índice/toplevel sin cambio)."""
        self._require_open()  # B298
        for rel, (policy, sealed) in list(self._cache.items()):
            fresh, err = self._read_once(rel, policy)
            if fresh is None:
                raise GovernanceSnapshotError(f"reverify {rel}: {err}")
            if fresh.sha256 != sealed.sha256 or fresh.stat.identity() != sealed.stat.identity():
                raise GovernanceSnapshotError(f"reverify {rel}: bytes/identidad cambiaron desde el sellado (B286)")
        if self._inventory is not None:  # B301/B303: revalidación (una captura más) — bytes CRUDOS + identidad del git
            fresh_inv, fresh_sha, fresh_ident = self._capture_inventory()
            if fresh_sha != self._inventory_sha or fresh_inv != self._inventory:
                raise GovernanceSnapshotError("reverify: el inventario git cambió desde el sellado (B301)")
            if fresh_ident != self._git_ident:
                raise GovernanceSnapshotError(
                    "reverify: la identidad del ejecutable git cambió desde el sellado (B303)"
                )
