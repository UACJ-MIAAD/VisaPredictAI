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
import shutil
import subprocess
import sys
import sysconfig
import venv
from collections.abc import Callable
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


def _purge_bytecode(env_path: Path) -> None:
    """Borra todo __pycache__/*.pyc/*.pyo del entorno antes de sellarlo y VERIFICA que no quede ninguno
    (B43; sin `ignore_errors` — un fallo de borrado debe ser visible). Si algo sobrevive, aborta ANTES de
    escribir READY.json (nunca se sella un árbol con bytecode)."""
    for p in env_path.rglob("*"):
        if p.is_dir() and p.name == "__pycache__" and not p.is_symlink():
            shutil.rmtree(p)
    for p in env_path.rglob("*"):
        if p.is_file() and p.suffix in (".pyc", ".pyo") and not p.is_symlink():
            p.unlink()
    left = [
        p.relative_to(env_path).as_posix()
        for p in env_path.rglob("*")
        if (p.is_file() and p.suffix in (".pyc", ".pyo")) or (p.is_dir() and p.name == "__pycache__")
    ]
    if left:
        raise SystemExit(f"python_env: bytecode residual tras purgar, no se sella: {left[:5]}")


def _pip_freeze(py: Path) -> list[str]:
    out = subprocess.run(
        [str(py), "-m", "pip", "freeze", "--all", "--disable-pip-version-check"],
        check=True,
        capture_output=True,
        text=True,
        env=_env_no_pyc(),
    ).stdout
    return sorted(line.strip() for line in out.splitlines() if line.strip() and not line.startswith("-e "))


def _pip_check(py: Path) -> bool:
    return (
        subprocess.run(
            [str(py), "-m", "pip", "check"], cwd=str(ROOT), capture_output=True, env=_env_no_pyc()
        ).returncode
        == 0
    )


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


def _tree_digest(env_path: Path) -> str:
    """B13/B43: sello MERKLE de TODOS los ficheros inmutables del entorno (site-packages, extensiones
    nativas, .dist-info/RECORD, console-scripts, pyvenv.cfg, intérprete). El ÚNICO excluido es READY.json
    (`_TREE_EXCLUDE_NAMES`); el bytecode YA NO se excluye — se purga antes de sellar y se corre todo con
    PYTHONDONTWRITEBYTECODE=1, de modo que un `.pyc` plantado altera el digest. Un symlink dentro del árbol
    se sella por su destino textual (no se sigue). Detecta manipulación de una librería aunque la versión
    no cambie."""
    entries: list[str] = []
    for p in sorted(env_path.rglob("*")):
        rel = p.relative_to(env_path).as_posix()
        if (
            any(part in _TREE_EXCLUDE_DIRS for part in p.parts)
            or p.name.endswith(_TREE_EXCLUDE_SUFFIX)
            or p.name in _TREE_EXCLUDE_NAMES
        ):
            continue
        if p.is_symlink():
            entries.append(f"{rel}\tL\t{os.readlink(p)}")
        elif p.is_file():
            entries.append(f"{rel}\tF\t{_sha256_path(p)}")
        elif p.is_dir():
            entries.append(f"{rel}\tD")
    return _sha256_bytes("\n".join(entries).encode())


def _file_hashes(env_path: Path, cfg: dict) -> dict[str, str]:
    """Hashes explícitos de los ficheros ejecutables clave (además del árbol completo): pyvenv.cfg,
    el intérprete RESUELTO (aunque bin/python sea symlink al python del sistema) y cada console-script
    declarado. Conjunto EXACTO — ready_valid exige que coincida (no un dict arbitrario/vacío)."""
    sub = "Scripts" if os.name == "nt" else "bin"
    out: dict[str, str] = {}
    for rel in ["pyvenv.cfg", *[f"{sub}/{cs}" for cs in cfg.get("console_scripts", [])]]:
        p = env_path / rel
        if p.exists() and not p.is_symlink():
            out[rel] = _sha256_path(p)
    # intérprete: hashea el CONTENIDO real (sigue el symlink); clave estable `bin/python#resolved`.
    py = env_path / sub / ("python.exe" if os.name == "nt" else "python")
    if py.exists():
        out[f"{sub}/python#resolved"] = _sha256_path(py.resolve())
    return out


def ready_valid(
    env_path: Path, profile: str, variant: str | None = None, profiles: dict | None = None
) -> tuple[bool, str]:
    """(válido, motivo). Reusa SOLO si: el lockset/contrato es válido AHORA (B14), READY.json existe con
    esquema exacto, casa el env_id, el DIGEST DE ÁRBOL completo coincide (B13: tamper de cualquier fichero
    de site-packages), los hashes de ficheros clave coinciden, `pip check` en vivo pasa y el inventario
    vivo es EXACTAMENTE el sellado."""
    import stat as _stat

    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    # B40/B41/B46: valida la CADENA de directorios (ROOT→.vp_envs→perfil→env_id) por openat O_NOFOLLOW —
    # ningún ancestro gobernado puede ser symlink, todos 0700 del UID — ANTES de leer nada. Un `.vp_envs`
    # symlink a un entorno externo, un dir 0777 o un ancestro de otro dueño abortan aquí.
    anchor, comps = _governed_chain(env_path)
    try:
        env_fd = _open_governed_chain(anchor, comps, create=False, require_mode=0o700)
    except SystemExit as exc:
        return False, f"cadena del entorno insegura: {exc}"
    # B52: MANTIENE env_fd abierto TODA la validación; los hashes/inventario/sello por ruta se hacen entre
    # dos chequeos de identidad (dev, ino) — si un ancestro se intercambia por symlink a mitad, se rechaza.
    try:
        est = os.fstat(env_fd)
        ident = (est.st_dev, est.st_ino)  # inode gobernado del entorno
        # B47/paso 3: abre READY.json RELATIVO al fd del entorno con O_NOFOLLOW y valida por fstat (regular,
        # UID, modo 0600 EXACTO, nlink==1); lee DEL MISMO descriptor — sin lstat-luego-read.
        try:
            rfd = os.open("READY.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=env_fd)
        except FileNotFoundError:
            return False, "READY.json ausente"
        except OSError as exc:
            return False, f"READY.json no abrible sin seguir symlink ({exc})"
        try:
            rst = os.fstat(rfd)
            if not _stat.S_ISREG(rst.st_mode):
                return False, "READY.json no es fichero regular"
            if rst.st_uid != os.getuid():
                return False, "READY.json de otro dueño"
            if _stat.S_IMODE(rst.st_mode) != 0o600:
                return False, f"READY.json modo {oct(_stat.S_IMODE(rst.st_mode))} != 0600"
            if rst.st_nlink != 1:
                return False, "READY.json con hardlink (nlink>1)"
            with os.fdopen(os.dup(rfd), "r") as fh:
                raw = fh.read()
        finally:
            os.close(rfd)
        # B14: no reusar bajo un lockset/contrato inválido.
        contract = lc.validate_all(ROOT)
        if contract:
            return False, "lockset/contrato inválido: " + "; ".join(contract)
        try:
            meta = json.loads(raw, object_pairs_hook=_no_dup_keys)
        except (ValueError, OSError, SystemExit) as exc:
            return False, f"READY.json ilegible/duplicado: {exc}"
        if set(meta) != _READY_KEYS:
            return False, f"READY.json con esquema inexacto (claves {sorted(meta)})"
        # B20/B28: validación SEMÁNTICA con IDENTIDAD DE TIPO (True != 1: un bool NO cuenta como int).
        if type(meta["schema_version"]) is not int or meta["schema_version"] != 1:
            return False, "schema_version no es int == 1"
        inv = meta["inventory"]
        if not isinstance(inv, list) or not all(isinstance(x, str) for x in inv):
            return False, "inventory no es lista de strings"
        if not all(_PIN.fullmatch(x) for x in inv):  # B35: fullmatch, no trailing basura ("alpha==1 X")
            return False, "inventory con entrada sin sintaxis exacta nombre==versión"
        if len({_canon(x.split("==")[0]) for x in inv}) != len(inv):
            return False, "inventory con nombre canónico duplicado"
        if type(meta["n_packages"]) is not int or meta["n_packages"] != len(inv):
            return False, f"n_packages no es int == len(inventory) {len(inv)}"
        if meta["inventory_digest"] != _inventory_digest(inv):
            return False, "inventory_digest sellado != recomputado del inventario"
        if meta["pip_check"] != "ok":
            return False, "pip_check sellado != 'ok'"
        if not isinstance(meta["file_hashes"], dict) or not meta["file_hashes"]:
            return False, "file_hashes vacío o no-dict"
        if meta.get("env_id") != env_id(profile, variant, profiles):
            return False, f"env_id sellado {meta.get('env_id')!r} != esperado"
        if meta.get("descriptor") != descriptor(profile, variant, profiles):
            return False, "descriptor sellado != actual"
        # B52: la ruta debe seguir apuntando al inode gobernado ANTES de hashear/inventariar por ruta.
        if not _ident_ok(env_path, ident):
            return False, "env_path ya no apunta al inode gobernado (swap de ancestro)"
        py = _venv_python(env_path)
        if not py.exists():
            return False, "falta el intérprete del venv"
        # B20/B13: file_hashes DEBE casar EXACTAMENTE el recomputado (conjunto de claves Y hashes:
        # pyvenv.cfg, console-scripts y el intérprete resuelto). Un dict arbitrario/vacío/alterado falla.
        if meta["file_hashes"] != _file_hashes(env_path, cfg):
            return False, "TAMPER: file_hashes sellado != recomputado (script/pyvenv/intérprete alterado)"
        if _tree_digest(env_path) != meta.get("tree_digest"):
            return False, "TAMPER: el árbol del entorno difiere del sello (fichero de site-packages alterado)"
        try:
            freeze = _pip_freeze(py)
        except (subprocess.CalledProcessError, OSError) as exc:
            return False, f"no se pudo inventariar: {exc}"
        if sorted(x.lower() for x in freeze) != sorted(x.lower() for x in inv):
            return False, "TAMPER: inventario vivo != sellado (versión o paquete extra)"
        # B34: el inventario sellado debe ser EXACTAMENTE el cierre esperado (no basta con contener el lock).
        sealed_obs = {_canon(x.split("==")[0]): x.split("==")[1] for x in inv if "==" in x}
        iprobs = _inventory_problems(sealed_obs, profile, variant, profiles)
        if iprobs:
            return False, "inventario sellado != cierre esperado: " + "; ".join(iprobs)
        if not _pip_check(py):
            return False, "TAMPER: pip check falla en el entorno sellado"
        # B52: reverifica el inode TRAS todas las operaciones por ruta — un swap a mitad de camino
        # (entre la lectura del sello y el hashing/inventario) invalida el resultado.
        if not _ident_ok(env_path, ident):
            return False, "env_path cambió de inode durante la validación (swap de ancestro)"
        return True, "ok"
    finally:
        os.close(env_fd)


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


def _pip(py: Path, *args: str) -> None:
    subprocess.run([str(py), "-m", "pip", *args], check=True, cwd=str(ROOT), env=_env_no_pyc())


def _install(py: Path, profile: str, cfg: dict, profiles: dict, lock_rel: str) -> None:
    tc = profiles["toolchain"]
    _pip(
        py,
        "install",
        "--disable-pip-version-check",
        f"pip=={tc['pip']}",
        f"setuptools=={tc['setuptools']}",
        f"wheel=={tc['wheel']}",
    )
    recipe = _resolved_recipe(cfg, lock_rel)
    lock = str(ROOT / lock_rel)
    if recipe == "hash-verified":
        _pip(py, "install", "--require-hashes", "-r", lock)
    elif recipe == "version-locked":
        _pip(py, "install", "-r", lock)
    elif recipe == "constraint-model":
        _pip(py, "install", "-e", ".[dev,model]", "-c", lock)
    elif recipe == "constraint-model-cpu-index":
        _pip(py, "install", f"torch=={cfg['cpu_torch']}", "--index-url", cfg["cpu_index"])
        _pip(py, "install", "-e", ".[dev,model]", "-c", lock)
    if cfg.get("project_source") == "editable" and recipe in ("hash-verified", "version-locked"):
        _pip(py, "install", "-e", ".", "--no-deps")


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
                    staging = STAGING_ROOT / nonce  # ruta abs SOLO para venv/pip/hashes, bracketed por _check_ident
                    _check_ident(staging, ident, "pre-venv")
                    venv.create(staging, with_pip=True, clear=True)
                    _check_ident(staging, ident, "post-venv")
                    py = _venv_python(staging)
                    _install(py, profile, cfg, profiles, lock_rel)
                    _check_ident(staging, ident, "post-install")
                    if not _pip_check(py):
                        raise SystemExit("python_env: pip check falla tras instalar")
                    freeze = _pip_freeze(py)
                    _check_ident(staging, ident, "post-freeze")
                    observed = {_canon(ln.split("==")[0]): ln.split("==")[1] for ln in freeze if "==" in ln}
                    # B24/B34: el inventario observado debe ser EXACTAMENTE el cierre esperado (pins+toolchain);
                    # faltantes, versiones distintas y extras (salvo `packaging`) abortan ANTES de sellar.
                    iprobs = _inventory_problems(observed, profile, variant, profiles)
                    if iprobs:
                        raise SystemExit("python_env: inventario observado != cierre esperado -> " + "; ".join(iprobs))
                    _check_ident(staging, ident, "pre-purge")
                    _purge_bytecode(staging)  # B43: sin .pyc en el árbol sellado
                    _check_ident(staging, ident, "pre-hash")
                    meta = {
                        "schema_version": 1,
                        "env_id": eid,
                        "descriptor": descriptor(profile, variant, profiles),
                        "inventory": freeze,
                        "inventory_digest": _inventory_digest(freeze),
                        "file_hashes": _file_hashes(staging, cfg),
                        "tree_digest": _tree_digest(staging),
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


def env_owns(profile: str, executable: Path, variant: str | None = None, profiles: dict | None = None) -> bool:
    try:
        executable.resolve().relative_to(env_dir(profile, variant, profiles).resolve())
        return True
    except ValueError, OSError:
        return False


def resolve_console_script(profile: str, name: str, variant: str | None = None, profiles: dict | None = None) -> Path:
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    if name not in cfg.get("console_scripts", []):
        raise SystemExit(f"python_env: {name!r} no es console-script declarado del perfil {profile!r}")
    target = build(profile, variant, profiles)
    binp = target / ("Scripts" if os.name == "nt" else "bin") / name
    if not binp.exists():
        raise SystemExit(f"python_env: {name} ausente en {binp}")
    return binp.resolve()


def _guard_env(cfg: dict) -> tuple[dict[str, str] | None, Callable[[], object] | None]:
    """B22: para un perfil cache-guarded devuelve (env sanitizado para el hijo, preexec umask 077). NO
    muta el os.environ/umask del padre. Para el resto, (None, None)."""
    if cfg.get("cache_guarded"):
        from tools import dvc_cache_guard

        return _env_no_pyc(dvc_cache_guard.child_env(ROOT)), (lambda: os.umask(0o077))
    return _env_no_pyc(), None  # B43: PYTHONDONTWRITEBYTECODE incluso sin cache guard


def run(
    profile: str, argv: list[str], *, variant: str | None = None, capture: bool = False, profiles: dict | None = None
) -> subprocess.CompletedProcess:
    """Ejecuta un console-script del perfil en su entorno aislado (build+reuse), aplicando el cache
    guard si el perfil es cache-guarded. El script se corre como `<env>/bin/python <env>/bin/<script>`."""
    if not argv:
        raise SystemExit("python_env: falta el comando")
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    name, rest = argv[0], argv[1:]
    binp = resolve_console_script(profile, name, variant, profiles)
    if not env_owns(profile, binp, variant, profiles):
        raise SystemExit(f"python_env: {binp} fuera del entorno certificado de {profile!r}")
    env, preexec = _guard_env(cfg)
    py = _venv_python(env_dir(profile, variant, profiles)).resolve()
    # NO se prepende el bin del env al PATH ⇒ los subprocesos de stage (`python -m ...` de dvc repro)
    # heredan el PATH ambiente = intérprete del PRODUCTO, jamás el del CLI DVC (R3.7).
    return subprocess.run(
        [str(py), str(binp), *rest],
        check=False,
        cwd=str(ROOT),
        capture_output=capture,
        text=capture,
        env=env,
        preexec_fn=preexec,
    )


def run_python(
    profile: str, argv: list[str], *, variant: str | None = None, capture: bool = False, profiles: dict | None = None
) -> subprocess.CompletedProcess:
    """Ejecuta `<env>/bin/python <argv>` del perfil (para el call graph del producto)."""
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    target = build(profile, variant, profiles)
    env, preexec = _guard_env(cfg)
    py = _venv_python(target).resolve()
    return subprocess.run(
        [str(py), *argv], check=False, cwd=str(ROOT), capture_output=capture, text=capture, env=env, preexec_fn=preexec
    )


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
