#!/usr/bin/env python
"""Entornos Python content-addressed (P0R.5, R2/C2/C3). AÍSLA en TIEMPO DE EJECUCIÓN cada perfil de
dependencias en su propio intérprete, direccionado por el hash de su cierre COMPLETO: lock + lockset +
config de perfil + **cache_guarded** + **variante** + python + plataforma + toolchain + modo de
instalación + **el sha256 del propio `python_env.py`, `dvc_cache_guard.py` y `python_profiles.json`**.
Motivación: instalar el cierre de una herramienta en el intérprete de otro perfil degrada sus deps
(contaminación demostrada). La solución es RUNTIME-aislar cada perfil en `.vp_envs/<profile>[/<variant>]/`.

CLI:
  python -m tools.python_env env-id     --profile P [--variant V]
  python -m tools.python_env path       --profile P [--variant V]
  python -m tools.python_env build      --profile P [--variant V]
  python -m tools.python_env exec       --profile P [--variant V] -- dvc <args>   # console-script
  python -m tools.python_env run-python --profile P [--variant V] -- -m pytest    # el python del env
  python -m tools.python_env prune                                                 # borra SOLO staging

`build` (transaccional, SIN `--force`): abre la CADENA ROOT→.vp_envs→<perfil> con openat O_NOFOLLOW
(ningún ancestro puede ser symlink) → flock (openat, 0600, sin hardlink) → valida lockset+perfil → staging
= subdir con nonce creado RELATIVO al fd de `.staging` validado, 0700 → instala SOLO la receta del perfil →
`pip check` → esperado==observado y **sin extras** → purga bytecode (sin `.pyc` en el sello) → digest de
inventario + **hashes de ficheros** → READY.json 0600 AL FINAL → fsync → rename create-only. Reusa SOLO si
READY revalida: cadena de dirs 0700 sin symlink, READY.json abierto por fd (regular, 0600, nlink==1), env_id,
hashes de ficheros, `tree_digest`, `pip check` en vivo e inventario EXACTO. Un entorno sellado ALTERADO ⇒
FALLA sin reparar; un target existente inválido NO se borra. `env_id` reproducible (sin fechas/PID/rutas/tmp).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tools import lock_contracts as lc

ROOT = lc.ROOT
ENVS_ROOT = ROOT / ".vp_envs"
STAGING_ROOT = ENVS_ROOT / ".staging"
PROFILES_JSON = ROOT / "environments" / "python_profiles.json"
GUARD_PY = ROOT / "tools" / "dvc_cache_guard.py"
SELF_PY = ROOT / "tools" / "python_env.py"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", re.MULTILINE)
_VALID_RECIPES = {"hash-verified", "version-locked", "constraint-model", "constraint-model-cpu-index"}


def _recipe_values(install_mode) -> list[str]:
    """Valores de receta declarados (str o dict por plataforma), para validar el esquema."""
    if isinstance(install_mode, dict):
        return list(install_mode.values())
    return [install_mode]


def _resolved_recipe(cfg: dict, lock_rel: str) -> str:
    """Receta CONCRETA para esta plataforma. `auto` ⇒ hash-verified si el lock trae `--hash=`, si no
    version-locked (macOS de referencia sin hashes vs espejo Linux hasheado)."""
    m = cfg["install_mode"]
    if isinstance(m, dict):
        m = m.get(platform_key())
    if m is None:
        raise SystemExit(f"python_env: sin install_mode para plataforma {platform_key()!r}")
    if m == "auto":
        return "hash-verified" if "--hash=" in (ROOT / lock_rel).read_text() else "version-locked"
    return m


def _canon(name: str) -> str:
    """Nombre canónico PEP 503 (runs de -_. → un guion, minúsculas): flufl.lock == flufl-lock."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _sha256_path(p: Path) -> str:
    return _sha256_bytes(p.read_bytes())


# --------------------------------------------------------------------------- perfiles / descriptor


def _no_dup_keys(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise SystemExit(f"python_env: clave duplicada en python_profiles.json: {k!r}")
        d[k] = v
    return d


_PLATFORMS = {"Darwin-arm64", "Linux-x86_64"}
_PROJECT_SOURCE = {"editable", "extra-model", "none"}
# B15: esquema EXACTO por perfil (claves requeridas / opcionales / plataformas de cada lock-table).
# B29: `mode` es el install_mode EXACTO esperado (str o dict plataforma→receta) y `project_source` el
# valor EXACTO por perfil (no solo pertenencia a un set global).
_SCHEMA: dict[str, dict[str, Any]] = {
    "runtime": {
        "req": {"install_mode", "locks", "project_source"},
        "opt": {"note"},
        "platforms": _PLATFORMS,
        "mode": "auto",
        "project_source": "editable",
    },
    "dev": {
        "req": {"install_mode", "locks", "project_source"},
        "opt": {"note"},
        "platforms": _PLATFORMS,
        "mode": "auto",
        "project_source": "editable",
    },
    "model": {
        "req": {"install_mode", "locks", "project_source", "cpu_torch", "cpu_index"},
        "opt": {"note"},
        "platforms": _PLATFORMS,
        "mode": {"Darwin-arm64": "constraint-model", "Linux-x86_64": "constraint-model-cpu-index"},
        "project_source": "extra-model",
    },
    "deep": {
        "req": {"install_mode", "variants", "project_source"},
        "opt": {"note"},
        "variants": {"cpu": _PLATFORMS, "cu126": {"Linux-x86_64"}},
        "mode": "hash-verified",
        "project_source": "none",
    },
    "dvc-tool": {
        "req": {"install_mode", "locks", "console_scripts", "cache_guarded", "project_source"},
        "opt": {"note"},
        "platforms": _PLATFORMS,
        "mode": "hash-verified",
        "project_source": "none",
    },
}


def _all_lock_names() -> set[str]:
    return {f"locks/{n}" for n in lc.LOCK_NAMES}


def _validate_lock_table(name: str, table: dict, platforms: set[str], known: set[str]) -> None:
    if set(table) != platforms:
        raise SystemExit(f"python_env: perfil {name!r} con plataformas {sorted(table)} != {sorted(platforms)}")
    for plat, lock in table.items():
        if not isinstance(lock, str) or lock not in known:
            raise SystemExit(
                f"python_env: perfil {name!r} plataforma {plat} lock {lock!r} no registrado en el manifiesto"
            )


_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_CPU_TORCH_RE = re.compile(r"^\d+\.\d+\.\d+\+cpu$")
_VER_RE = re.compile(r"^\d+(\.\d+)+$")  # versión de toolchain: 26.1.2, 0.47.0, …


def load_profiles() -> dict:
    prof = json.loads(PROFILES_JSON.read_text(), object_pairs_hook=_no_dup_keys)
    if set(prof) != {"schema_version", "toolchain", "profiles"}:
        raise SystemExit(f"python_env: claves superiores {sorted(prof)} != schema_version/toolchain/profiles")
    if type(prof["schema_version"]) is not int or prof["schema_version"] != 1:  # B36: True no es 1
        raise SystemExit("python_env: schema_version no es int == 1")
    tc = prof["toolchain"]
    if not isinstance(tc, dict) or not isinstance(prof["profiles"], dict):
        raise SystemExit("python_env: toolchain/profiles no son objetos")
    if set(tc) != {"pip", "setuptools", "wheel", "uv"} or not all(
        isinstance(v, str) and _VER_RE.match(v) for v in tc.values()
    ):
        raise SystemExit("python_env: toolchain incompleto o con versión inválida en python_profiles.json")
    profiles = prof["profiles"]
    if set(profiles) != set(_SCHEMA):
        raise SystemExit(f"python_env: perfiles {sorted(profiles)} != exactamente {sorted(_SCHEMA)}")
    known = _all_lock_names()
    allowed_recipes = _VALID_RECIPES | {"auto"}
    for name, cfg in profiles.items():
        sch = _SCHEMA[name]
        if not isinstance(cfg, dict):
            raise SystemExit(f"python_env: perfil {name!r} no es un objeto")
        keys = set(cfg)
        if not (sch["req"] <= keys <= sch["req"] | sch["opt"]):
            raise SystemExit(
                f"python_env: perfil {name!r} con claves {sorted(keys)} != req {sorted(sch['req'])} (+opt {sorted(sch['opt'])})"
            )
        # B21: TIPOS estrictos (un bool 1, un console_scripts string, o un cpu_index ajeno se rechazan)
        if "note" in cfg and not isinstance(cfg["note"], str):
            raise SystemExit(f"python_env: perfil {name!r} note no-string")
        if not isinstance(cfg.get("cache_guarded", False), bool):
            raise SystemExit(f"python_env: perfil {name!r} cache_guarded no-booleano ({cfg['cache_guarded']!r})")
        if name == "dvc-tool" and cfg["console_scripts"] != ["dvc"]:
            raise SystemExit(f"python_env: dvc-tool console_scripts != ['dvc'] ({cfg['console_scripts']!r})")
        if name == "model":
            if not isinstance(cfg["cpu_torch"], str) or not _CPU_TORCH_RE.match(cfg["cpu_torch"]):
                raise SystemExit(f"python_env: model cpu_torch inválido {cfg['cpu_torch']!r} (X.Y.Z+cpu)")
            if cfg["cpu_index"] != _CPU_INDEX:
                raise SystemExit(f"python_env: model cpu_index no autorizado {cfg['cpu_index']!r}")
        # B29: install_mode y project_source EXACTOS por perfil (matriz de plataformas incluida).
        if cfg["install_mode"] != sch["mode"]:
            raise SystemExit(
                f"python_env: perfil {name!r} install_mode {cfg['install_mode']!r} != esperado {sch['mode']!r}"
            )
        for r in _recipe_values(cfg["install_mode"]):
            if r not in allowed_recipes:
                raise SystemExit(f"python_env: perfil {name!r} install_mode inválido {r!r}")
        if cfg["project_source"] != sch["project_source"]:
            raise SystemExit(
                f"python_env: perfil {name!r} project_source {cfg['project_source']!r} != {sch['project_source']!r}"
            )
        if "variants" in sch:
            if set(cfg["variants"]) != set(sch["variants"]):
                raise SystemExit(
                    f"python_env: perfil {name!r} variantes {sorted(cfg['variants'])} != {sorted(sch['variants'])}"
                )
            for var, plats in sch["variants"].items():
                _validate_lock_table(f"{name}/{var}", cfg["variants"][var], plats, known)
        else:
            _validate_lock_table(name, cfg["locks"], sch["platforms"], known)
    return prof


def _profile_config(profiles: dict, profile: str) -> dict:
    cfg = profiles.get("profiles", {}).get(profile)
    if cfg is None:
        raise SystemExit(f"python_env: perfil desconocido {profile!r}")
    return cfg


def platform_key() -> str:
    """Clave estable de plataforma para elegir el lock: 'Darwin-arm64' / 'Linux-x86_64'."""
    return f"{platform.system()}-{platform.machine()}"


def _libc_or_macos() -> str:
    if platform.system() == "Darwin":
        return "macos-" + (platform.mac_ver()[0].split(".")[0] or "0")
    name, ver = platform.libc_ver()
    return f"{name or 'unknown'}-{'.'.join(ver.split('.')[:2]) if ver else '0'}"


def _locks_table(cfg: dict, variant: str | None) -> dict:
    """Devuelve el mapa plataforma→lock para el (perfil, variante). Perfiles con `variants` EXIGEN
    variante explícita (nunca elegir CUDA en silencio)."""
    if "variants" in cfg:
        if variant is None:
            raise SystemExit(f"python_env: este perfil exige --variant (opciones: {sorted(cfg['variants'])})")
        if variant not in cfg["variants"]:
            raise SystemExit(f"python_env: variante {variant!r} desconocida (opciones: {sorted(cfg['variants'])})")
        return cfg["variants"][variant]
    if variant is not None:
        raise SystemExit(f"python_env: el perfil no admite variantes (--variant {variant!r})")
    return cfg["locks"]


def lock_rel_for(profile: str, variant: str | None = None, profiles: dict | None = None) -> str:
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    key = platform_key()
    lock = _locks_table(cfg, variant).get(key)
    if lock is None:
        raise SystemExit(f"python_env: perfil {profile!r} variante {variant!r} sin lock para {key!r}")
    return lock


def descriptor(profile: str, variant: str | None = None, profiles: dict | None = None) -> dict:
    """Descriptor CANÓNICO (fuente del env_id). Sin rutas/fechas/PID/tmp; incluye la política de
    seguridad (cache_guarded) y los sha256 del propio tooling gobernante."""
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    lock_rel = lock_rel_for(profile, variant, profiles)
    # B12: TODA la config operativa del perfil entra al env_id (incl. cpu_torch/cpu_index/receta),
    # excluyendo solo el campo informativo `note`.
    pcfg = {k: v for k, v in cfg.items() if k != "note"}
    return {
        "schema_version": 1,
        "profile": profile,
        "variant": variant,
        "install_mode": _resolved_recipe(cfg, lock_rel),
        "cache_guarded": bool(cfg.get("cache_guarded", False)),
        "project_source": cfg.get("project_source", "none"),
        "lock_sha256": _sha256_path(ROOT / lock_rel),
        "lockset_sha256": _sha256_path(ROOT / lc.MANIFEST_REL),
        "profile_config_sha256": _sha256_bytes(json.dumps(pcfg, sort_keys=True).encode()),
        "governance": {
            "python_env_sha256": _sha256_path(SELF_PY),
            "dvc_cache_guard_sha256": _sha256_path(GUARD_PY),
            "profiles_json_sha256": _sha256_path(PROFILES_JSON),
        },
        "python": {
            "implementation": platform.python_implementation().lower(),
            "version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag or "",
            "abi": sysconfig.get_config_var("SOABI") or "",
        },
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "libc_or_macos": _libc_or_macos(),
        },
        "toolchain": dict(profiles["toolchain"]),
    }


def env_id(profile: str, variant: str | None = None, profiles: dict | None = None) -> str:
    d = descriptor(profile, variant, profiles)
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def env_dir(profile: str, variant: str | None = None, profiles: dict | None = None) -> Path:
    leaf = profile if variant is None else f"{profile}-{variant}"
    return ENVS_ROOT / leaf / env_id(profile, variant, profiles)


# --------------------------------------------------------------------------- inventario / hashes / READY


def _venv_python(env_path: Path) -> Path:
    sub, exe = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return env_path / sub / exe


def _env_no_pyc(base: dict[str, str] | None = None) -> dict[str, str]:
    """B43: env con PYTHONDONTWRITEBYTECODE=1 — ningún proceso del entorno escribe .pyc, así el árbol
    sellado permanece libre de bytecode y un .pyc plantado se detecta como tamper."""
    return {**(base if base is not None else os.environ), "PYTHONDONTWRITEBYTECODE": "1"}


# B55/B58: intérprete RELATIVO al cwd fd-anclado. Nunca se ejecuta por RUTA ABSOLUTA del staging/env
# (una ruta absoluta reabre la ventana de swap de un ancestro por symlink).
_REL_PY = "Scripts\\python.exe" if os.name == "nt" else "bin/python"


def _run_in_dir(
    dir_fd: int, argv: list[str], *, extra_umask: bool = False, capture: bool = False, env: dict | None = None
) -> subprocess.CompletedProcess:
    """B55: ejecuta `argv` con el cwd del hijo FIJADO por `fchdir(dir_fd)` — inmune al swap de un ancestro por
    symlink (el inode queda anclado, no la ruta). `argv[0]` puede ser relativo (`bin/python`) y se resuelve
    DESPUÉS del fchdir; `dir_fd` se pasa con `pass_fds` para sobrevivir a `close_fds`. Las rutas de
    lock/constraints/proyecto que se pasen en `argv` deben ser ABSOLUTAS (fuera del env, de confianza)."""

    def _pre() -> None:
        os.fchdir(dir_fd)
        if extra_umask:
            os.umask(0o077)

    return subprocess.run(
        argv, check=False, capture_output=capture, text=capture, env=env, preexec_fn=_pre, pass_fds=(dir_fd,)
    )


# ---- lectura/hasheo/borrado RELATIVOS a un descriptor de directorio (B58) ----


def _open_dir_at(parent_fd: int, name: str) -> int:
    """openat de un subdirectorio con `O_DIRECTORY|O_NOFOLLOW` (un symlink ⇒ OSError). `name` sin `/`."""
    if "/" in name:
        raise SystemExit(f"python_env: componente inválido {name!r}")
    return os.open(name, _ODIR, dir_fd=parent_fd)


def _open_optional_dir_at(parent_fd: int, name: str) -> int | None:
    try:
        return _open_dir_at(parent_fd, name)
    except FileNotFoundError:
        return None


def _sha256_fd_at(dir_fd: int, name: str, *, follow: bool = False) -> str:
    """sha256 del contenido de `name` RELATIVO a `dir_fd`. Por defecto `O_NOFOLLOW` (no sigue symlink);
    con `follow=True` resuelve el symlink (para el intérprete real). Lectura incremental (ficheros grandes)."""
    flags = os.O_RDONLY | (0 if follow else os.O_NOFOLLOW)
    ffd = os.open(name, flags, dir_fd=dir_fd)
    try:
        h = hashlib.sha256()
        while chunk := os.read(ffd, 1 << 20):
            h.update(chunk)
    finally:
        os.close(ffd)
    return "sha256:" + h.hexdigest()


def _pip_freeze_at(dir_fd: int, env: dict | None = None) -> list[str]:
    r = _run_in_dir(
        dir_fd,
        [_REL_PY, "-m", "pip", "freeze", "--all", "--disable-pip-version-check"],
        capture=True,
        env=env if env is not None else _env_no_pyc(),
    )
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, "pip freeze", output=r.stdout, stderr=r.stderr)
    return sorted(line.strip() for line in r.stdout.splitlines() if line.strip() and not line.startswith("-e "))


def _pip_check_at(dir_fd: int, env: dict | None = None) -> bool:
    return (
        _run_in_dir(
            dir_fd, [_REL_PY, "-m", "pip", "check"], capture=True, env=env if env is not None else _env_no_pyc()
        )
    ).returncode == 0


def _purge_bytecode_at(dir_fd: int) -> None:
    """B43: borra todo `__pycache__`/`*.pyc`/`*.pyo` bajo `dir_fd`, fd-relativo y sin seguir symlinks, y
    VERIFICA que no quede ninguno (sin `ignore_errors`). Si algo sobrevive, aborta ANTES de sellar READY.json."""

    def walk(fd: int) -> None:
        for entry in list(os.scandir(fd)):
            est = os.lstat(entry.name, dir_fd=fd)
            if stat.S_ISLNK(est.st_mode):
                continue
            if stat.S_ISDIR(est.st_mode):
                if entry.name == "__pycache__":
                    _rmtree_at(fd, entry.name)
                else:
                    child = os.open(entry.name, _ODIR, dir_fd=fd)
                    try:
                        walk(child)
                    finally:
                        os.close(child)
            elif stat.S_ISREG(est.st_mode) and entry.name.endswith((".pyc", ".pyo")):
                os.unlink(entry.name, dir_fd=fd)

    walk(dir_fd)
    residual = _bytecode_residual_at(dir_fd, "")
    if residual:
        raise SystemExit(f"python_env: bytecode residual tras purgar, no se sella: {residual[:5]}")


def _bytecode_residual_at(dir_fd: int, prefix: str) -> list[str]:
    left: list[str] = []
    for entry in os.scandir(dir_fd):
        est = os.lstat(entry.name, dir_fd=dir_fd)
        rel = prefix + entry.name
        if stat.S_ISLNK(est.st_mode):
            continue
        if stat.S_ISDIR(est.st_mode):
            if entry.name == "__pycache__":
                left.append(rel)
            else:
                child = os.open(entry.name, _ODIR, dir_fd=dir_fd)
                try:
                    left.extend(_bytecode_residual_at(child, rel + "/"))
                finally:
                    os.close(child)
        elif stat.S_ISREG(est.st_mode) and entry.name.endswith((".pyc", ".pyo")):
            left.append(rel)
    return left


def _purge_bytecode(env_path: Path) -> None:
    """Envoltorio POR RUTA de `_purge_bytecode_at` (solo para pruebas unitarias standalone)."""
    fd = os.open(str(env_path), _ODIR)
    try:
        _purge_bytecode_at(fd)
    finally:
        os.close(fd)


def _inventory_digest(freeze: list[str]) -> str:
    return _sha256_bytes("\n".join(sorted(x.lower() for x in freeze)).encode())


# B43: el ÚNICO fichero excluido del sello es el propio READY.json. El BYTECODE (.pyc/__pycache__) ya
# NO se excluye — se borra antes de sellar y se corre todo con PYTHONDONTWRITEBYTECODE=1, de modo que un
# .pyc manipulado (código malicioso sin cambiar la fuente) altera el tree_digest y se detecta como tamper.
_TREE_EXCLUDE_DIRS: set[str] = set()
_TREE_EXCLUDE_SUFFIX: tuple[str, ...] = ()
_TREE_EXCLUDE_NAMES = {"READY.json"}
_READY_KEYS = {
    "schema_version",
    "env_id",
    "descriptor",
    "inventory",
    "inventory_digest",
    "file_hashes",
    "tree_digest",
    "pip_check",
    "n_packages",
}


def _tree_digest_at(dir_fd: int) -> str:
    """B13/B43/B58/B62: sello MERKLE de TODOS los objetos del entorno, recorrido RELATIVO a `dir_fd` con
    `openat`/`O_NOFOLLOW` por componente (ningún directorio intermedio se sigue si es symlink). **FAIL-CLOSED
    por tipo (B62):** solo se admiten directorios, ficheros regulares y symlinks; un FIFO, socket, dispositivo
    de bloque/carácter o cualquier tipo desconocido ABORTA (un venv gobernado no necesita objetos especiales,
    y sellarlos como `?` colisionaba entre sí). Cada objeto se sella con su TIPO, modo, dueño y (ficheros/dirs)
    nº de enlaces + contenido/destino; todo objeto debe pertenecer al UID esperado. Los regulares se ABREN una
    sola vez con `O_NOFOLLOW`, se re-valida por `fstat` del descriptor abierto (mismo inode, regular, dueño) y
    se hashea DE ESE MISMO descriptor (sin ventana lstat→open). El ÚNICO excluido es READY.json."""
    uid = os.getuid()
    entries: list[str] = []

    def walk(fd: int, prefix: str) -> None:
        for name in sorted(e.name for e in os.scandir(fd)):
            rel = prefix + name
            est = os.lstat(name, dir_fd=fd)
            mode = est.st_mode
            if est.st_uid != uid:
                raise SystemExit(f"python_env: árbol del entorno: {rel!r} de otro dueño ({est.st_uid}) — rechazado")
            perm = oct(stat.S_IMODE(mode))
            if stat.S_ISLNK(mode):
                entries.append(f"{rel}\tL\t{perm}\t{os.readlink(name, dir_fd=fd)}")
            elif stat.S_ISDIR(mode):
                if name in _TREE_EXCLUDE_DIRS:
                    continue
                child = os.open(name, _ODIR, dir_fd=fd)
                try:
                    cst = os.fstat(child)  # re-valida el inode abierto (no un swap entre lstat y open)
                    if not stat.S_ISDIR(cst.st_mode) or cst.st_uid != uid or cst.st_ino != est.st_ino:
                        raise SystemExit(f"python_env: árbol del entorno: {rel!r} cambió entre lstat y open")
                    entries.append(f"{rel}\tD\t{oct(stat.S_IMODE(cst.st_mode))}\t{cst.st_uid}")
                    walk(child, rel + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                if name in _TREE_EXCLUDE_NAMES or name.endswith(_TREE_EXCLUDE_SUFFIX):
                    continue
                ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    fst = os.fstat(ffd)
                    if not stat.S_ISREG(fst.st_mode) or fst.st_uid != uid or fst.st_ino != est.st_ino:
                        raise SystemExit(f"python_env: árbol del entorno: {rel!r} cambió entre lstat y open")
                    h = hashlib.sha256()
                    while chunk := os.read(ffd, 1 << 20):
                        h.update(chunk)
                    entries.append(
                        f"{rel}\tF\t{oct(stat.S_IMODE(fst.st_mode))}\t{fst.st_uid}\t{fst.st_nlink}\tsha256:{h.hexdigest()}"
                    )
                finally:
                    os.close(ffd)
            else:
                # B62: FIFO / socket / dispositivo de bloque o carácter / tipo desconocido ⇒ FAIL-CLOSED.
                raise SystemExit(
                    f"python_env: árbol del entorno: {rel!r} es un objeto especial (modo {perm}) — rechazado"
                )

    walk(dir_fd, "")
    return _sha256_bytes("\n".join(sorted(entries)).encode())


def _tree_digest(env_path: Path) -> str:
    """Envoltorio POR RUTA de `_tree_digest_at` (para pruebas unitarias standalone)."""
    fd = os.open(str(env_path), _ODIR)
    try:
        return _tree_digest_at(fd)
    finally:
        os.close(fd)


def _file_hashes_at(dir_fd: int, cfg: dict) -> dict[str, str]:
    """B58: hashes explícitos de los ficheros ejecutables clave, RELATIVOS a `dir_fd` (pyvenv.cfg, el
    intérprete RESUELTO y cada console-script declarado). Los console-scripts se abren bajo `bin/` con
    `openat`/`O_NOFOLLOW`; el intérprete se sigue (`follow=True`) para hashear su contenido real. Conjunto
    EXACTO — ready_valid exige que coincida (no un dict arbitrario/vacío)."""
    sub = "Scripts" if os.name == "nt" else "bin"
    out: dict[str, str] = {}
    for name in ("pyvenv.cfg",):
        try:
            fst = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(fst.st_mode):  # lstat: un symlink NO es S_ISREG ⇒ se excluye
            out[name] = _sha256_fd_at(dir_fd, name)
    bin_fd = _open_optional_dir_at(dir_fd, sub)
    if bin_fd is not None:
        try:
            for cs in cfg.get("console_scripts", []):
                try:
                    cst = os.lstat(cs, dir_fd=bin_fd)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(cst.st_mode):
                    out[f"{sub}/{cs}"] = _sha256_fd_at(bin_fd, cs)
            py = "python.exe" if os.name == "nt" else "python"
            try:
                out[f"{sub}/python#resolved"] = _sha256_fd_at(bin_fd, py, follow=True)
            except FileNotFoundError:
                pass
        finally:
            os.close(bin_fd)
    return out


def _file_hashes(env_path: Path, cfg: dict) -> dict[str, str]:
    """Envoltorio POR RUTA de `_file_hashes_at` (para pruebas unitarias standalone)."""
    fd = os.open(str(env_path), _ODIR)
    try:
        return _file_hashes_at(fd, cfg)
    finally:
        os.close(fd)


def _read_ready_at(env_fd: int) -> tuple[str | None, str | None]:
    """B47/B58: abre READY.json RELATIVO a `env_fd` con O_NOFOLLOW, valida por fstat (regular, del UID, modo
    0600 EXACTO, nlink==1) y lee DEL MISMO descriptor (sin ventana lstat-luego-read). (raw, None) o
    (None, motivo). Fuente única — usada por `ready_valid` y por `read_ready`."""
    try:
        rfd = os.open("READY.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=env_fd)
    except FileNotFoundError:
        return None, "READY.json ausente"
    except OSError as exc:
        return None, f"READY.json no abrible sin seguir symlink ({exc})"
    try:
        rst = os.fstat(rfd)
        if not stat.S_ISREG(rst.st_mode):
            return None, "READY.json no es fichero regular"
        if rst.st_uid != os.getuid():
            return None, "READY.json de otro dueño"
        if stat.S_IMODE(rst.st_mode) != 0o600:
            return None, f"READY.json modo {oct(stat.S_IMODE(rst.st_mode))} != 0600"
        if rst.st_nlink != 1:
            return None, "READY.json con hardlink (nlink>1)"
        with os.fdopen(os.dup(rfd), "r") as fh:
            return fh.read(), None
    finally:
        os.close(rfd)


def _validate_open_env(
    env_fd: int, env_path: Path, profile: str, variant: str | None, profiles: dict, cfg: dict
) -> tuple[bool, str, dict | None]:
    """B58/B61: valida un entorno YA ABIERTO (`env_fd`) por completo — SIN reabrir la ruta. Devuelve
    (ok, motivo, meta_sellado). TODA lectura/hasheo/inventario es RELATIVA a `env_fd` (openat/fchdir); los
    `_ident_ok` bracketan como tripwire (B52). Fuente ÚNICA compartida por `ready_valid` (wrapper) y
    `open_valid_environment` (handle vivo)."""
    est = os.fstat(env_fd)
    ident = (est.st_dev, est.st_ino)  # inode gobernado del entorno
    raw, err = _read_ready_at(env_fd)
    if err or raw is None:
        return False, err or "READY.json ilegible", None
    # B14: no reusar bajo un lockset/contrato inválido.
    contract = lc.validate_all(ROOT)
    if contract:
        return False, "lockset/contrato inválido: " + "; ".join(contract), None
    try:
        meta = json.loads(raw, object_pairs_hook=_no_dup_keys)
    except (ValueError, OSError, SystemExit) as exc:
        return False, f"READY.json ilegible/duplicado: {exc}", None
    if set(meta) != _READY_KEYS:
        return False, f"READY.json con esquema inexacto (claves {sorted(meta)})", None
    # B20/B28: validación SEMÁNTICA con IDENTIDAD DE TIPO (True != 1: un bool NO cuenta como int).
    if type(meta["schema_version"]) is not int or meta["schema_version"] != 1:
        return False, "schema_version no es int == 1", None
    inv = meta["inventory"]
    if not isinstance(inv, list) or not all(isinstance(x, str) for x in inv):
        return False, "inventory no es lista de strings", None
    if not all(_PIN.fullmatch(x) for x in inv):  # B35: fullmatch, no trailing basura ("alpha==1 X")
        return False, "inventory con entrada sin sintaxis exacta nombre==versión", None
    if len({_canon(x.split("==")[0]) for x in inv}) != len(inv):
        return False, "inventory con nombre canónico duplicado", None
    if type(meta["n_packages"]) is not int or meta["n_packages"] != len(inv):
        return False, f"n_packages no es int == len(inventory) {len(inv)}", None
    if meta["inventory_digest"] != _inventory_digest(inv):
        return False, "inventory_digest sellado != recomputado del inventario", None
    if meta["pip_check"] != "ok":
        return False, "pip_check sellado != 'ok'", None
    if not isinstance(meta["file_hashes"], dict) or not meta["file_hashes"]:
        return False, "file_hashes vacío o no-dict", None
    if meta.get("env_id") != env_id(profile, variant, profiles):
        return False, f"env_id sellado {meta.get('env_id')!r} != esperado", None
    if meta.get("descriptor") != descriptor(profile, variant, profiles):
        return False, "descriptor sellado != actual", None
    # B52: tripwire — la ruta debe seguir apuntando al inode gobernado ANTES de hashear/inventariar.
    if not _ident_ok(env_path, ident):
        return False, "env_path ya no apunta al inode gobernado (swap de ancestro)", None
    # B58: el intérprete se comprueba y ejecuta RELATIVO a env_fd (nunca por ruta absoluta re-resuelta).
    try:
        os.stat("bin/python", dir_fd=env_fd)
    except FileNotFoundError:
        return False, "falta el intérprete del venv", None
    except OSError as exc:
        return False, f"intérprete del venv inaccesible ({exc})", None
    # B20/B13/B58: file_hashes y tree_digest recomputados POR EL DESCRIPTOR (openat/O_NOFOLLOW).
    if meta["file_hashes"] != _file_hashes_at(env_fd, cfg):
        return False, "TAMPER: file_hashes sellado != recomputado (script/pyvenv/intérprete alterado)", None
    if _tree_digest_at(env_fd) != meta.get("tree_digest"):
        return False, "TAMPER: el árbol del entorno difiere del sello (fichero de site-packages alterado)", None
    try:
        freeze = _pip_freeze_at(env_fd)  # bin/python vía fchdir(env_fd)
    except (subprocess.CalledProcessError, OSError) as exc:
        return False, f"no se pudo inventariar: {exc}", None
    if sorted(x.lower() for x in freeze) != sorted(x.lower() for x in inv):
        return False, "TAMPER: inventario vivo != sellado (versión o paquete extra)", None
    # B34: el inventario sellado debe ser EXACTAMENTE el cierre esperado (no basta con contener el lock).
    sealed_obs = {_canon(x.split("==")[0]): x.split("==")[1] for x in inv if "==" in x}
    iprobs = _inventory_problems(sealed_obs, profile, variant, profiles)
    if iprobs:
        return False, "inventario sellado != cierre esperado: " + "; ".join(iprobs), None
    if not _pip_check_at(env_fd):
        return False, "TAMPER: pip check falla en el entorno sellado", None
    # B52: reverifica el inode TRAS todas las operaciones (tripwire de defensa; el hasheo ya fue fd-relativo).
    if not _ident_ok(env_path, ident):
        return False, "env_path cambió de inode durante la validación (swap de ancestro)", None
    return True, "ok", meta


def ready_valid(
    env_path: Path, profile: str, variant: str | None = None, profiles: dict | None = None
) -> tuple[bool, str]:
    """(válido, motivo). Wrapper de `_validate_open_env` sobre la cadena gobernada de `env_path` (abre→valida→
    cierra). B40/B41/B46: valida la CADENA (ROOT→.vp_envs→perfil→env_id) por openat O_NOFOLLOW — ningún
    ancestro gobernado puede ser symlink, todos 0700 del UID — ANTES de leer nada."""
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    anchor, comps = _governed_chain(env_path)
    try:
        env_fd = _open_governed_chain(anchor, comps, create=False, require_mode=0o700)
    except SystemExit as exc:
        return False, f"cadena del entorno insegura: {exc}"
    try:
        ok, why, _meta = _validate_open_env(env_fd, env_path, profile, variant, profiles, cfg)
        return ok, why
    finally:
        os.close(env_fd)


class _ValidEnv:
    """Handle de un entorno ABIERTO y VALIDADO: mantiene `fd` vivo (todo hasheo/lectura/ejecución es relativa
    a él) y expone el `ready` (sello VALIDADO en esa misma pasada), `env_id`, `path` y `cfg`. Solo lo emite
    `open_valid_environment`, que cierra `fd` al salir del context manager (B60/B61)."""

    __slots__ = ("fd", "ready", "env_id", "path", "cfg")

    def __init__(self, fd: int, ready: dict, env_id: str, path: Path, cfg: dict) -> None:
        self.fd = fd
        self.ready = ready
        self.env_id = env_id
        self.path = path
        self.cfg = cfg


@contextmanager
def open_valid_environment(
    profile: str, variant: str | None = None, profiles: dict | None = None
) -> Iterator[_ValidEnv]:
    """B60/B61: API ÚNICA de entorno abierto+validado. Abre la cadena gobernada UNA sola vez, MANTIENE `env_fd`
    abierto, valida por completo SOBRE ese descriptor y entrega un `_ValidEnv` cuyo `ready` proviene de esa
    misma validación. El fd se cierra SOLO al salir del `with`. Los consumidores (lectura del sello, ejecución
    del intérprete) operan RELATIVOS a `env.fd` — inmunes a un swap/reemplazo del ancestro tras la validación.
    Aborta si el entorno no es válido."""
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    target = env_dir(profile, variant, profiles)
    anchor, comps = _governed_chain(target)
    try:
        env_fd = _open_governed_chain(anchor, comps, create=False, require_mode=0o700)
    except SystemExit as exc:
        raise SystemExit(f"python_env: entorno {profile!r} inaccesible: {exc}") from exc
    try:
        ok, why, meta = _validate_open_env(env_fd, target, profile, variant, profiles, cfg)
        if not ok or meta is None:
            raise SystemExit(f"python_env: entorno {profile!r} no válido: {why}")
        yield _ValidEnv(env_fd, meta, meta["env_id"], target, cfg)
    finally:
        os.close(env_fd)


def read_ready(profile: str, variant: str | None = None, profiles: dict | None = None) -> dict:
    """B61/§3.7: devuelve el sello VALIDADO del entorno usando la MISMA validación que lo abrió (sin cerrar y
    reabrir la ruta, que permitía leer un sello reemplazado). Fuente única para consumidores que necesitan el
    inventario/env_id/descriptor sin re-resolver rutas (p. ej. `dvc_tool_smoke`)."""
    with open_valid_environment(profile, variant, profiles) as env:
        return env.ready


# --------------------------------------------------------------------------- build (transaccional)


def _expected_pins(lock_rel: str) -> dict[str, str]:
    return {_canon(m.group(1)): m.group(2) for m in _PIN.finditer((ROOT / lock_rel).read_text())}


# B34: `packaging` es dep TRANSITIVA del toolchain de bootstrap; su versión no la controlamos ⇒ se
# permite por NOMBRE (única excepción sin versión). Todo lo demás debe casar exactamente.
_NAME_ONLY_TOOLCHAIN = {"packaging"}


def expected_inventory(profile: str, variant: str | None = None, profiles: dict | None = None) -> dict[str, str]:
    """Cierre EXACTO nombre_canónico→versión de un entorno: los pins del lock MÁS el toolchain de
    bootstrap ausente del lock (pip/setuptools/wheel con la versión EXACTA de python_profiles.json; si un
    toolchain YA está en el lock, manda el lock). Fuente ÚNICA para build/ready_valid/validate_receipt."""
    profiles = profiles or load_profiles()
    lock_rel = lock_rel_for(profile, variant, profiles)
    pins = _expected_pins(lock_rel)
    tc = profiles["toolchain"]
    for pkg in ("pip", "setuptools", "wheel"):
        pins.setdefault(_canon(pkg), tc[pkg])  # lock gana si presente; si no, versión del toolchain
    return pins


def _inventory_problems(observed: dict[str, str], profile: str, variant: str | None, profiles: dict) -> list[str]:
    """observed (canon→versión) debe ser EXACTAMENTE expected_inventory, salvo `packaging` (name-only)."""
    expected = expected_inventory(profile, variant, profiles)
    probs = []
    wrong = {n: (expected[n], observed.get(n)) for n in expected if observed.get(n) != expected[n]}
    if wrong:
        probs.append(f"pins faltantes/incorrectos: {sorted(wrong)[:5]}")
    extras = set(observed) - set(expected) - _NAME_ONLY_TOOLCHAIN
    if extras:
        probs.append(f"paquetes EXTRA no permitidos: {sorted(extras)[:5]}")
    return probs


def _pip_at(dir_fd: int, *args: str, env: dict) -> None:
    """B55: `bin/python -m pip …` con el cwd del hijo FIJADO por fchdir(dir_fd) — nunca por ruta absoluta del
    staging (una ruta reabre la ventana de swap de ancestro)."""
    r = _run_in_dir(dir_fd, [_REL_PY, "-m", "pip", *args], capture=True, env=env)
    if r.returncode != 0:
        raise SystemExit(f"python_env: pip {' '.join(args[:2])} falló (rc {r.returncode}): {(r.stderr or '')[-800:]}")


def _venv_create_at(dir_fd: int) -> None:
    """B55: crea el venv DENTRO del inode del staging con el cwd fijado por fchdir — SIN `clear=True` ni ruta
    absoluta (el dir se creó vacío por `mkdirat`). `--without-pip` + `ensurepip` para que TODA la creación
    pase por el descriptor. La fijación es por fchdir (portable Linux+macOS); `/dev/fd/N/…` NO resuelve a
    través de directorios en macOS."""
    env = _env_no_pyc()
    r = _run_in_dir(dir_fd, [sys.executable, "-m", "venv", "--without-pip", "."], capture=True, env=env)
    if r.returncode != 0:
        raise SystemExit(f"python_env: creación del venv falló (rc {r.returncode}): {(r.stderr or '')[-800:]}")
    r2 = _run_in_dir(dir_fd, [_REL_PY, "-m", "ensurepip", "--upgrade"], capture=True, env=env)
    if r2.returncode != 0:
        raise SystemExit(f"python_env: ensurepip falló (rc {r2.returncode}): {(r2.stderr or '')[-800:]}")


def _install_at(dir_fd: int, profile: str, cfg: dict, profiles: dict, lock_rel: str) -> None:
    """Instala el cierre del perfil RELATIVO al descriptor del staging (fchdir). Las rutas de lock/proyecto
    van ABSOLUTAS (fuera del env, de confianza); `-e .` se sustituye por `-e <ROOT ABS>` para instalar el
    PROYECTO, no el cwd fijado por fchdir."""
    env = _env_no_pyc()
    tc = profiles["toolchain"]
    _pip_at(
        dir_fd,
        "install",
        "--disable-pip-version-check",
        f"pip=={tc['pip']}",
        f"setuptools=={tc['setuptools']}",
        f"wheel=={tc['wheel']}",
        env=env,
    )
    recipe = _resolved_recipe(cfg, lock_rel)
    lock = str(ROOT / lock_rel)
    root = str(ROOT)
    if recipe == "hash-verified":
        _pip_at(dir_fd, "install", "--require-hashes", "-r", lock, env=env)
    elif recipe == "version-locked":
        _pip_at(dir_fd, "install", "-r", lock, env=env)
    elif recipe == "constraint-model":
        _pip_at(dir_fd, "install", "-e", f"{root}[dev,model]", "-c", lock, env=env)
    elif recipe == "constraint-model-cpu-index":
        _pip_at(dir_fd, "install", f"torch=={cfg['cpu_torch']}", "--index-url", cfg["cpu_index"], env=env)
        _pip_at(dir_fd, "install", "-e", f"{root}[dev,model]", "-c", lock, env=env)
    if cfg.get("project_source") == "editable" and recipe in ("hash-verified", "version-locked"):
        _pip_at(dir_fd, "install", "-e", root, "--no-deps", env=env)


# --------------------------------------------------------------------------- apertura segura de cadenas
# B46/B48: validar SOLO el objeto final por lstat es INSEGURO. Si un ANCESTRO gobernado (`.vp_envs`, el dir
# de perfil, `.staging`) es un symlink, la ruta resuelve a un árbol EXTERNO y el leaf real pasa el chequeo
# (ready_valid reusa fuera del repo; prune borra fuera del repo). La defensa correcta es abrir la cadena
# COMPONENTE A COMPONENTE con O_DIRECTORY|O_NOFOLLOW relativo al fd del padre (openat), de modo que NINGÚN
# componente pueda ser symlink, validando cada dir por fstat (dir real, UID actual, modo 0700 EXACTO).
_ODIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _fstat_dir_ok(fd: int, label: str, require_mode: int | None) -> None:
    import stat as _stat

    st = os.fstat(fd)
    if not _stat.S_ISDIR(st.st_mode):
        raise SystemExit(f"python_env: {label} no es directorio")
    if st.st_uid != os.getuid():
        raise SystemExit(f"python_env: {label} es de otro dueño ({st.st_uid})")
    if require_mode is not None and _stat.S_IMODE(st.st_mode) != require_mode:
        raise SystemExit(f"python_env: {label} modo {oct(_stat.S_IMODE(st.st_mode))} != {oct(require_mode)}")


def _openat_dir(parent_fd: int, name: str, *, create: bool, require_mode: int) -> int:
    """openat(parent_fd, name, O_DIRECTORY|O_NOFOLLOW). Si `create`, mkdirat 0700 (tolera EEXIST) y, SOLO si
    lo creamos nosotros, fchmod al modo exacto (pese al umask). `name` es un componente simple. Un symlink
    en `name` (incluido roto) ⇒ OSError ⇒ SystemExit (fail-closed, sin `exists()` previo)."""
    if "/" in name or name in ("", ".", ".."):
        raise SystemExit(f"python_env: componente de ruta inválido {name!r}")
    created = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
    try:
        fd = os.open(name, _ODIR, dir_fd=parent_fd)
    except OSError as exc:
        raise SystemExit(f"python_env: {name!r} no abrible sin seguir symlink ({exc}) — prohibido") from exc
    try:
        if created:
            os.fchmod(fd, require_mode)
        _fstat_dir_ok(fd, name, require_mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_governed_chain(anchor: Path, components: list[str], *, create: bool, require_mode: int) -> int:
    """Abre `anchor` (ancla de confianza; su modo NO se valida, solo que su ÚLTIMO componente no sea symlink)
    y desciende por `components` con openat O_NOFOLLOW, creando+validando cada nivel a 0700. Devuelve el fd
    del ÚLTIMO componente (el llamador lo cierra). Fail-closed: symlink (incl. roto), dueño o modo != 0700
    en cualquier nivel aborta con SystemExit."""
    try:
        fd = os.open(str(anchor), _ODIR)
    except OSError as exc:
        raise SystemExit(f"python_env: ancla {anchor} inaccesible sin seguir symlink ({exc})") from exc
    try:
        for name in components:
            child = _openat_dir(fd, name, create=create, require_mode=require_mode)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _governed_chain(path: Path) -> tuple[Path, list[str]]:
    """(ancla, componentes) para validar `path` por cadena. Bajo ROOT ⇒ ancla=ROOT y TODA la cadena de
    componentes; fuera de ROOT (tests) ⇒ ancla=padre y el último componente (su propio nombre y el padre
    no pueden ser symlink)."""
    try:
        rel = path.relative_to(ROOT)
        return ROOT, list(rel.parts)
    except ValueError:
        return path.parent, [path.name]


def _open_lock(dir_fd: int, name: str) -> int:
    """B25/B39: abre el lock-file `name` RELATIVO a `dir_fd` (openat) con O_NOFOLLOW y exige regular, del UID
    actual, modo 0600 y st_nlink==1 (sin hardlink). Nuevo ⇒ O_EXCL 0600; existente ⇒ O_RDWR. Nunca trunca.
    Abrir relativo al fd del dir de perfil YA validado evita que un `.vp_envs` symlink redirija el lock."""
    import stat as _stat

    if "/" in name:
        raise SystemExit(f"python_env: nombre de lock inválido {name!r}")
    try:
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    except FileExistsError:
        fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=dir_fd)
    st = os.fstat(fd)
    if (
        not _stat.S_ISREG(st.st_mode)
        or st.st_uid != os.getuid()
        or _stat.S_IMODE(st.st_mode) != 0o600
        or st.st_nlink != 1
    ):
        os.close(fd)
        raise SystemExit(f"python_env: lock-file {name} inseguro (no regular / dueño / modo!=0600 / hardlink)")
    return fd


def build(profile: str, variant: str | None = None, profiles: dict | None = None) -> Path:
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    target = env_dir(profile, variant, profiles)
    ok, why = ready_valid(target, profile, variant, profiles)
    if ok:
        return target
    if target.exists():
        raise SystemExit(
            f"python_env: entorno sellado inválido en {target} ({why}) — NO se repara; usa `prune`/borra manualmente"
        )
    # B46: crea/valida la cadena ROOT→.vp_envs→<perfil> con openat O_NOFOLLOW y MANTIENE abierto el fd del
    # dir de perfil TODA la transacción (B51: la promoción/limpieza serán RELATIVAS a él, inmunes a swaps).
    eid = env_id(profile, variant, profiles)
    p_anchor, p_comps = _governed_chain(target.parent)
    profile_fd = _open_governed_chain(p_anchor, p_comps, create=True, require_mode=0o700)
    try:
        lock_fd = _open_lock(profile_fd, f".lock-{eid}")  # openat relativo al perfil
        with os.fdopen(lock_fd, "r+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            ok, why = ready_valid(target, profile, variant, profiles)  # re-check bajo el lock
            if ok:
                return target
            # B25: si el target EXISTE tras el lock y es inválido, abortar — NUNCA reparar/reemplazar.
            if target.exists():
                raise SystemExit(f"python_env: target inválido bajo el lock en {target} ({why}) — no se repara")
            probs = lc.validate_all(ROOT)
            if probs:
                raise SystemExit("python_env: lockset/contrato inválido -> " + "; ".join(probs))
            lock_rel = lock_rel_for(profile, variant, profiles)
            # staging: subdir con nonce creado RELATIVO al fd de `.vp_envs/.staging`; MANTIENE abiertos el fd
            # del padre `.staging` y el del nonce toda la transacción (B51).
            s_anchor, s_comps = _governed_chain(STAGING_ROOT)
            staging_parent_fd = _open_governed_chain(s_anchor, s_comps, create=True, require_mode=0o700)
            try:
                nonce = f"{eid}.{os.urandom(8).hex()}"
                os.mkdir(nonce, 0o700, dir_fd=staging_parent_fd)
                staging_fd = os.open(nonce, _ODIR, dir_fd=staging_parent_fd)
                sealed = False
                try:
                    os.fchmod(staging_fd, 0o700)
                    sst = os.fstat(staging_fd)
                    ident = (sst.st_dev, sst.st_ino)  # inode gobernado del staging
                    # B55: el staging se creó VACÍO por `mkdirat`; se comprueba por el DESCRIPTOR (no por ruta).
                    if next(os.scandir(staging_fd), None) is not None:
                        raise SystemExit("python_env: staging no está vacío tras crearlo — abortado")
                    staging = (
                        STAGING_ROOT / nonce
                    )  # ruta abs SOLO como tripwire _check_ident (las ops son fd-relativas)
                    # B55: creación/instalación/freeze/check/purge/hash RELATIVOS a `staging_fd` (fchdir/openat) —
                    # sin `venv.create(clear=True)` por ruta absoluta (que borraba el árbol externo tras un swap).
                    # Los `_check_ident` bracketan como defensa en profundidad; la seguridad la da la fd-relatividad.
                    _check_ident(staging, ident, "pre-venv")
                    _venv_create_at(staging_fd)
                    _check_ident(staging, ident, "post-venv")
                    _install_at(staging_fd, profile, cfg, profiles, lock_rel)
                    _check_ident(staging, ident, "post-install")
                    if not _pip_check_at(staging_fd):
                        raise SystemExit("python_env: pip check falla tras instalar")
                    freeze = _pip_freeze_at(staging_fd)
                    _check_ident(staging, ident, "post-freeze")
                    observed = {_canon(ln.split("==")[0]): ln.split("==")[1] for ln in freeze if "==" in ln}
                    # B24/B34: el inventario observado debe ser EXACTAMENTE el cierre esperado (pins+toolchain);
                    # faltantes, versiones distintas y extras (salvo `packaging`) abortan ANTES de sellar.
                    iprobs = _inventory_problems(observed, profile, variant, profiles)
                    if iprobs:
                        raise SystemExit("python_env: inventario observado != cierre esperado -> " + "; ".join(iprobs))
                    _check_ident(staging, ident, "pre-purge")
                    _purge_bytecode_at(staging_fd)  # B43: sin .pyc en el árbol sellado
                    _check_ident(staging, ident, "pre-hash")
                    meta = {
                        "schema_version": 1,
                        "env_id": eid,
                        "descriptor": descriptor(profile, variant, profiles),
                        "inventory": freeze,
                        "inventory_digest": _inventory_digest(freeze),
                        "file_hashes": _file_hashes_at(staging_fd, cfg),
                        "tree_digest": _tree_digest_at(staging_fd),
                        "pip_check": "ok",
                        "n_packages": len(freeze),
                    }
                    assert set(meta) == _READY_KEYS  # esquema exacto (B4/B13)
                    _check_ident(staging, ident, "pre-seal")
                    # B51: sella READY.json RELATIVO a staging_fd (openat O_EXCL 0600), fsync por fd
                    data = (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode()
                    rfd = os.open(
                        "READY.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=staging_fd
                    )
                    try:
                        os.write(rfd, data)
                        os.fchmod(rfd, 0o600)  # B40: el sello es solo-usuario
                        os.fsync(rfd)
                    finally:
                        os.close(rfd)
                    _fsync_fd(staging_fd)
                    # B32/B51: promoción CREATE-ONLY ATÓMICA y RELATIVA a fds (staging_parent_fd → profile_fd);
                    # inmune a swap de ancestro (opera sobre los inodes ya validados, no rutas absolutas).
                    _rename_noreplace(staging_parent_fd, nonce, profile_fd, target.name)
                    _fsync_fd(profile_fd)  # el rename se persiste con fsync del PADRE
                    sealed = True
                    return target
                finally:
                    os.close(staging_fd)
                    if not sealed:
                        _safe_cleanup(staging_parent_fd, nonce)  # SOLO por fd; nunca por ruta absoluta
            finally:
                os.close(staging_parent_fd)
    finally:
        os.close(profile_fd)


def _rename_noreplace(src_dir_fd: int, src_name: str, dst_dir_fd: int, dst_name: str) -> None:
    """B51: promoción create-only RELATIVA a descriptores de directorio — renameat2(RENAME_NOREPLACE) en
    Linux, renameatx_np(RENAME_EXCL) en macOS, ambos con `olddirfd`/`newdirfd`. Al operar sobre los fds ya
    validados (no rutas absolutas), un swap de un ancestro por symlink NO puede redirigir la promoción a un
    árbol externo. Falla si el destino ya existe (EEXIST). Fail-closed si la primitiva no está disponible."""
    import ctypes
    import ctypes.util

    if "/" in src_name or "/" in dst_name:
        raise SystemExit("python_env: nombre con '/' en promoción — prohibido")
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    s, d = os.fsencode(src_name), os.fsencode(dst_name)
    system = platform.system()
    ctypes.set_errno(0)
    if system == "Darwin":
        if not hasattr(libc, "renameatx_np"):
            raise SystemExit("python_env: renameatx_np no disponible — fail-closed")
        rc = libc.renameatx_np(ctypes.c_int(src_dir_fd), s, ctypes.c_int(dst_dir_fd), d, ctypes.c_uint(0x00000004))
    elif system == "Linux":
        if not hasattr(libc, "renameat2"):
            raise SystemExit("python_env: renameat2 no disponible — fail-closed")
        rc = libc.renameat2(
            ctypes.c_int(src_dir_fd), s, ctypes.c_int(dst_dir_fd), d, ctypes.c_uint(1)
        )  # RENAME_NOREPLACE
    else:
        raise SystemExit(f"python_env: promoción create-only no soportada en {system}")
    if rc != 0:
        err = ctypes.get_errno()
        raise SystemExit(
            f"python_env: promoción create-only falló ({dst_name} ya existe?) errno {err} {os.strerror(err)}"
        )


def _check_ident(path: Path, ident: tuple[int, int], what: str) -> None:
    """B51/B52: verifica que la RUTA ABSOLUTA `path` siga resolviendo al inode gobernado `ident`
    (st_dev, st_ino). `os.stat` SIGUE la ruta, así que un swap de un ancestro por symlink a otro árbol
    cambia (dev, ino) y aborta ANTES de que venv/pip/hash/borrado toquen algo externo."""
    try:
        st = os.stat(str(path))
    except OSError as exc:
        raise SystemExit(f"python_env: {what}: {path} no accesible ({exc}) — abortado") from exc
    if (st.st_dev, st.st_ino) != ident:
        raise SystemExit(f"python_env: {what}: {path} ya no apunta al inode gobernado — abortado (swap de ancestro)")


def _ident_ok(path: Path, ident: tuple[int, int]) -> bool:
    """B52: True si la RUTA ABSOLUTA `path` sigue resolviendo al inode gobernado `ident`. Versión que NO
    lanza (para `ready_valid`, que devuelve (bool, motivo))."""
    try:
        st = os.stat(str(path))
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == ident


def _safe_cleanup(parent_fd: int, name: str) -> None:
    """B51: borra el staging SOLO relativo al fd del padre ya validado (nunca por ruta absoluta, que
    seguiría un ancestro symlink). Sin `ignore_errors`; tolera solo que ya no exista."""
    try:
        _rmtree_at(parent_fd, name)
    except FileNotFoundError:
        pass


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


_STAGING_NAME = re.compile(r"^[0-9a-f]{64}\.[A-Za-z0-9_]+$")  # <env_id>.<sufijo nonce>


def _rmtree_at(parent_fd: int, name: str) -> None:
    """Borra recursivamente `name` (dir) RELATIVO a parent_fd con openat/unlinkat/rmdir + O_NOFOLLOW —
    ningún symlink del árbol se sigue (un symlink hijo se `unlink`ea, no se desciende). `name` no puede
    contener `/`."""
    if "/" in name or name in ("", ".", ".."):
        raise SystemExit(f"python_env: componente inválido en borrado {name!r}")
    fd = os.open(name, _ODIR, dir_fd=parent_fd)  # `name` debe ser dir REAL (no symlink)
    try:
        for entry in os.scandir(fd):
            if entry.is_dir(follow_symlinks=False):
                _rmtree_at(fd, entry.name)
            else:
                os.unlink(entry.name, dir_fd=fd)  # fichero o symlink: unlink no sigue el destino
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def prune_staging() -> int:
    """B42/B48: borra SOLO `.vp_envs/.staging/`, de forma NO destructiva y resistente a symlink EN CUALQUIER
    ANCESTRO. Desciende la cadena de `STAGING_ROOT` por openat O_NOFOLLOW (un symlink —incluido roto— en
    cualquier ancestro BLOQUEA, no devuelve 0; ausencia REAL de un nivel ⇒ 0). PREVALIDA todos los hijos por
    fstat relativo al fd (dir real, del UID, sin symlink, nombre `<env_id>.<sufijo>` por fullmatch) ANTES de
    borrar el primero; ante cualquier anomalía borra CERO y falla. Enumera y borra RELATIVO al descriptor
    seguro (nunca por ruta, que seguiría un ancestro symlink). Sin `ignore_errors`."""
    import stat as _stat

    anchor, comps = _governed_chain(STAGING_ROOT)
    if not comps:
        return 0
    try:
        fd = os.open(str(anchor), _ODIR)
    except OSError as exc:
        raise SystemExit(f"python_env: ancla {anchor} inaccesible sin seguir symlink ({exc}) — prune abortado") from exc
    try:
        # desciende por la cadena; ENOENT en cualquier nivel ⇒ nada que podar; symlink/otro ⇒ BLOQUEA.
        for name in comps:
            try:
                child = os.open(name, _ODIR, dir_fd=fd)
            except FileNotFoundError:
                return 0
            except OSError as exc:  # symlink (incl. roto) u otro
                raise SystemExit(
                    f"python_env: {name!r} no abrible sin seguir symlink ({exc}) — prune abortado"
                ) from exc
            os.close(fd)
            fd = child
            _fstat_dir_ok(fd, name, 0o700)
        staging_fd = fd  # fd apunta al `.staging` validado
        victims: list[str] = []
        for entry in sorted(os.scandir(staging_fd), key=lambda e: e.name):
            est = entry.stat(follow_symlinks=False)  # lstat relativo al fd (no sigue symlink)
            if (
                _stat.S_ISLNK(est.st_mode)
                or not _stat.S_ISDIR(est.st_mode)
                or est.st_uid != os.getuid()
                or not _STAGING_NAME.fullmatch(entry.name)
            ):
                raise SystemExit(
                    f"python_env: entrada de staging inesperada {entry.name!r} — prune abortado (0 borrados)"
                )
            victims.append(entry.name)
        for name in victims:
            _rmtree_at(staging_fd, name)  # borrado relativo al fd, resistente a symlink
        return len(victims)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- exec / run-python
# B67: `env_owns`/`resolve_console_script` (resolvían por `.resolve()`/ruta absoluta el console-script y su
# contención) fueron RETIRADOS — reintroducían el patrón de resolución por ruta que B60 eliminó. `run`
# valida el console-script contra `console_scripts` del perfil y ejecuta por módulo fd-bound; los scripts se
# gobiernan con `_governed_script`.


def _governed_script(name: str) -> str:
    """B64: valida que `name` sea un script GOBERNADO — relativo a ROOT, sin `..`, dentro de ROOT, fichero
    regular, sin symlink en ningún componente y VERSIONADO en git — y devuelve su ruta relativa. Rechaza
    rutas absolutas / `../` / fuera de ROOT / no versionadas / con symlink (código no gobernado no entra al
    camino oficial)."""
    p = Path(name)
    if p.is_absolute():
        raise SystemExit(f"python_env: script con ruta absoluta no permitido: {name!r}")
    if ".." in p.parts or "" in p.parts:
        raise SystemExit(f"python_env: script con componente '..' no permitido: {name!r}")
    # sin symlink en NINGÚN componente bajo ROOT (un symlink intermedio resolvería fuera)
    cur = ROOT
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise SystemExit(f"python_env: componente symlink en el script no permitido: {name!r}")
    full = ROOT / p
    try:
        full.resolve(strict=True).relative_to(ROOT.resolve())
    except (ValueError, OSError) as exc:
        raise SystemExit(f"python_env: script fuera de ROOT o inexistente: {name!r} ({exc})") from exc
    if not full.is_file():
        raise SystemExit(f"python_env: el script no es un fichero regular: {name!r}")
    r = subprocess.run(["git", "ls-files", "--error-unmatch", "--", str(p)], cwd=str(ROOT), capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"python_env: el script no está versionado en git: {name!r}")
    return str(p)


def _guard_env(cfg: dict) -> tuple[dict[str, str] | None, Callable[[], object] | None]:
    """B22: para un perfil cache-guarded devuelve (env sanitizado para el hijo, preexec umask 077). NO
    muta el os.environ/umask del padre. Para el resto, (None, None)."""
    if cfg.get("cache_guarded"):
        from tools import dvc_cache_guard

        return _env_no_pyc(dvc_cache_guard.child_env(ROOT)), (lambda: os.umask(0o077))
    return _env_no_pyc(), None  # B43: PYTHONDONTWRITEBYTECODE incluso sin cache guard


# B60: bootstrap FIJO (no atacante-controlado) que corre en el intérprete del entorno ya arrancado fd-bound.
# El intérprete se lanza por `fchdir(env_fd)` + `bin/python` (relativo, inmune a swap de ancestro); en el
# arranque el redirector de venv fija `sys.prefix=<env>` (argv0=bin/python resuelto desde el cwd anclado).
# YA dentro del proceso, el bootstrap hace `os.chdir(root)` y despacha el modo permitido SIN un 2º exec por
# ruta (module vía runpy, `-c` code, o script gobernado dentro de ROOT). Para DVC = `runpy.run_module("dvc")`,
# nunca resolver `<env>/bin/dvc`.
_RUNTIME_BOOTSTRAP = (
    "import sys, os, json, runpy\n"
    "s = json.loads(sys.argv[1])\n"
    "os.chdir(s['root'])\n"
    "mode, rest = s['mode'], s['rest']\n"
    "if mode == 'module':\n"
    "    sys.argv = [s['name'], *rest]\n"
    "    runpy.run_module(s['name'], run_name='__main__', alter_sys=True)\n"
    "elif mode == 'code':\n"
    "    sys.argv = ['-c', *rest]\n"
    "    exec(compile(s['code'], '<string>', 'exec'), {'__name__': '__main__'})\n"
    "elif mode == 'script':\n"
    "    sys.argv = [s['name'], *rest]\n"
    "    runpy.run_path(s['name'], run_name='__main__')\n"
    "else:\n"
    "    raise SystemExit('python_env bootstrap: modo no soportado ' + repr(mode))\n"
)
_ALLOWED_MODES = {"module", "code", "script"}


def _parse_python_argv(argv: list[str]) -> dict:
    """Traduce un argv estilo `python …` a un spec explícito {mode, …, rest}. Solo `-m module`, `-c code` y
    `script`; cualquier otro flag de intérprete se rechaza (evita reabrir el intérprete por ruta con opciones
    arbitrarias, B60)."""
    if not argv:
        raise SystemExit("python_env: falta el comando")
    if argv[0] == "-m":
        if len(argv) < 2:
            raise SystemExit("python_env: -m requiere un módulo")
        return {"mode": "module", "name": argv[1], "rest": list(argv[2:])}
    if argv[0] == "-c":
        if len(argv) < 2:
            raise SystemExit("python_env: -c requiere código")
        return {"mode": "code", "code": argv[1], "rest": list(argv[2:])}
    if argv[0].startswith("-"):
        # B64: rechaza `python -` (stdin) y cualquier flag de intérprete arbitrario.
        raise SystemExit(f"python_env: flag de intérprete no soportado {argv[0]!r}")
    # B64: un script debe ser GOBERNADO (relativo a ROOT, versionado, sin symlink/`..`/absoluto).
    return {"mode": "script", "name": _governed_script(argv[0]), "rest": list(argv[1:])}


def _launch_fd_bound(env: _ValidEnv, spec: dict, capture: bool) -> subprocess.CompletedProcess:
    """B60: arranca `bin/python -c <bootstrap> <spec>` RELATIVO a `env.fd` (fchdir), aplicando el cache guard
    del perfil. `spec` lleva `root` (ROOT absoluto, de confianza) y el modo permitido; el intérprete se
    resuelve por el descriptor validado y vivo, no por una ruta re-resuelta. NO se prepende el bin del env al
    PATH ⇒ los subprocesos de stage (`python -m ...` de dvc repro) heredan el intérprete del PRODUCTO (R3.7)."""
    if spec.get("mode") not in _ALLOWED_MODES:
        raise SystemExit(f"python_env: modo de ejecución no permitido {spec.get('mode')!r}")
    guard_env, _ = _guard_env(env.cfg)
    extra_umask = bool(env.cfg.get("cache_guarded"))
    payload = {**spec, "root": str(ROOT)}
    return _run_in_dir(
        env.fd,
        [_REL_PY, "-c", _RUNTIME_BOOTSTRAP, json.dumps(payload)],
        extra_umask=extra_umask,
        capture=capture,
        env=guard_env,
    )


def run(
    profile: str, argv: list[str], *, variant: str | None = None, capture: bool = False, profiles: dict | None = None
) -> subprocess.CompletedProcess:
    """Ejecuta un console-script declarado del perfil en su entorno aislado (build+reuse). B60: el intérprete
    se lanza RELATIVO al `env.fd` validado y vivo (context manager) y el console-script se corre como MÓDULO
    gobernado (`runpy.run_module(name)`), nunca resolviendo `<env>/bin/<script>` por ruta."""
    if not argv:
        raise SystemExit("python_env: falta el comando")
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    name, rest = argv[0], list(argv[1:])
    if name not in cfg.get("console_scripts", []):
        raise SystemExit(f"python_env: {name!r} no es console-script declarado del perfil {profile!r}")
    build(profile, variant, profiles)  # asegura el entorno construido/sellado
    with open_valid_environment(profile, variant, profiles) as env:
        # el console-script se ejecuta como módulo homónimo (dvc → `python -m dvc`, con __main__).
        return _launch_fd_bound(env, {"mode": "module", "name": name, "rest": rest}, capture)


def run_python(
    profile: str, argv: list[str], *, variant: str | None = None, capture: bool = False, profiles: dict | None = None
) -> subprocess.CompletedProcess:
    """Ejecuta `python <argv>` del perfil (call graph del producto) RELATIVO al `env.fd` validado y vivo
    (B60). `argv` se traduce a un modo explícito (`-m`/`-c`/script); no se reabre el intérprete por ruta."""
    profiles = profiles or load_profiles()
    spec = _parse_python_argv(argv)
    build(profile, variant, profiles)  # asegura el entorno construido/sellado
    with open_valid_environment(profile, variant, profiles) as env:
        return _launch_fd_bound(env, spec, capture)


# --------------------------------------------------------------------------- provenance (recibos)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True, check=True).stdout.strip()


def provenance() -> dict:
    """Procedencia git para recibos: distingue el head declarado (fuente) del checkout real (que en un
    PR es el merge sintético refs/pull/N/merge) y la base."""
    checkout = _git("rev-parse", "HEAD")
    checkout_tree = _git("rev-parse", "HEAD^{tree}")
    dirty = bool(
        subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    )
    prov = {
        "checkout_sha": checkout,
        "checkout_tree_sha": checkout_tree,
        "git_dirty": dirty,
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    }
    # En un PR, GITHUB_SHA es el merge; el head fuente viene en el evento.
    prov["source_head_sha"] = os.environ.get("GITHUB_PR_HEAD_SHA") or os.environ.get("GITHUB_SHA") or checkout
    prov["base_sha"] = os.environ.get("GITHUB_BASE_SHA")
    return prov


# --------------------------------------------------------------------------- CLI


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("env-id", "path", "build"):
        p = sub.add_parser(c)
        p.add_argument("--profile", required=True)
        p.add_argument("--variant", default=None)
    for c in ("exec", "run-python"):
        p = sub.add_parser(c)
        p.add_argument("--profile", required=True)
        p.add_argument("--variant", default=None)
        p.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("prune")
    ns = ap.parse_args(argv[1:])

    if ns.cmd == "env-id":
        print(env_id(ns.profile, ns.variant))
        return 0
    if ns.cmd == "path":
        print(env_dir(ns.profile, ns.variant))
        return 0
    if ns.cmd == "build":
        print(f"✓ entorno {ns.profile}{'/' + ns.variant if ns.variant else ''} listo: {build(ns.profile, ns.variant)}")
        return 0
    if ns.cmd == "prune":
        print(f"✓ staging purgado: {prune_staging()} entradas")
        return 0
    rest = ns.rest[1:] if ns.rest and ns.rest[0] == "--" else ns.rest
    if ns.cmd == "exec":
        return run(ns.profile, rest, variant=ns.variant).returncode
    if ns.cmd == "run-python":
        return run_python(ns.profile, rest, variant=ns.variant).returncode
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
