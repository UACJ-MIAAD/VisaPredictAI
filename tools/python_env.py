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

`build` (transaccional, SIN `--force`): flock → valida lockset+perfil → staging `mkdtemp` 0700 → instala
SOLO la receta del perfil → `pip check` → esperado==observado y **sin extras** → digest de inventario +
**hashes de ficheros** (bin scripts, pyvenv.cfg) → READY.json AL FINAL → fsync fichero+staging+**padre**
→ rename. Reusa SOLO si READY revalida: env_id, hashes de ficheros, `pip check` en vivo e inventario
EXACTO. Un entorno sellado ALTERADO (versiones, contenido de un script, o paquete extra) ⇒ FALLA sin
reparar; un target existente inválido NO se borra. `env_id` reproducible (sin fechas/PID/rutas/tmp).
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
import tempfile
import venv
from pathlib import Path

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


def load_profiles() -> dict:
    prof = json.loads(PROFILES_JSON.read_text(), object_pairs_hook=_no_dup_keys)
    if prof.get("schema_version") != 1:
        raise SystemExit("python_env: schema_version de python_profiles.json != 1")
    tc = prof.get("toolchain", {})
    if set(tc) != {"pip", "setuptools", "wheel", "uv"}:
        raise SystemExit("python_env: toolchain incompleto en python_profiles.json")
    allowed = _VALID_RECIPES | {"auto"}
    for name, cfg in prof.get("profiles", {}).items():
        for r in _recipe_values(cfg.get("install_mode")):
            if r not in allowed:
                raise SystemExit(f"python_env: perfil {name!r} con install_mode inválido {r!r}")
        if "locks" not in cfg and "variants" not in cfg:
            raise SystemExit(f"python_env: perfil {name!r} sin `locks` ni `variants`")
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
    # fragmento del perfil que afecta la resolución/ejecución (sin campos informativos como `note`).
    pcfg = {
        k: cfg[k]
        for k in ("install_mode", "locks", "variants", "console_scripts", "cache_guarded", "project_source")
        if k in cfg
    }
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


def _pip_freeze(py: Path) -> list[str]:
    out = subprocess.run(
        [str(py), "-m", "pip", "freeze", "--all", "--disable-pip-version-check"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(line.strip() for line in out.splitlines() if line.strip() and not line.startswith("-e "))


def _pip_check(py: Path) -> bool:
    return subprocess.run([str(py), "-m", "pip", "check"], cwd=str(ROOT), capture_output=True).returncode == 0


def _inventory_digest(freeze: list[str]) -> str:
    return _sha256_bytes("\n".join(sorted(x.lower() for x in freeze)).encode())


def _tracked_bin_files(env_path: Path, cfg: dict) -> list[str]:
    """Ficheros ejecutables sellados: pyvenv.cfg + cada console-script declarado del perfil."""
    names = ["pyvenv.cfg"]
    sub = "Scripts" if os.name == "nt" else "bin"
    for cs in cfg.get("console_scripts", []):
        names.append(f"{sub}/{cs}")
    return names


def _file_hashes(env_path: Path, cfg: dict) -> dict[str, str]:
    out = {}
    for rel in _tracked_bin_files(env_path, cfg):
        p = env_path / rel
        if p.exists() and not p.is_symlink():
            out[rel] = _sha256_path(p)
    return out


def ready_valid(
    env_path: Path, profile: str, variant: str | None = None, profiles: dict | None = None
) -> tuple[bool, str]:
    """(válido, motivo). Reusa SOLO si READY.json existe, casa el env_id, TODOS los hashes de ficheros
    sellados coinciden, `pip check` en vivo pasa y el inventario vivo es EXACTAMENTE el sellado."""
    profiles = profiles or load_profiles()
    ready = env_path / "READY.json"
    if not ready.exists():
        return False, "sin READY.json"
    try:
        meta = json.loads(ready.read_text())
    except (ValueError, OSError) as exc:
        return False, f"READY.json ilegible: {exc}"
    if meta.get("env_id") != env_id(profile, variant, profiles):
        return False, f"env_id sellado {meta.get('env_id')!r} != esperado"
    py = _venv_python(env_path)
    if not py.exists():
        return False, "falta el intérprete del venv"
    for rel, h in (meta.get("file_hashes") or {}).items():
        p = env_path / rel
        if not p.exists() or p.is_symlink() or _sha256_path(p) != h:
            return False, f"TAMPER: {rel} alterado o ausente"
    try:
        freeze = _pip_freeze(py)
    except (subprocess.CalledProcessError, OSError) as exc:
        return False, f"no se pudo inventariar: {exc}"
    if sorted(x.lower() for x in freeze) != sorted(x.lower() for x in (meta.get("inventory") or [])):
        return False, "TAMPER: inventario vivo != sellado (versión o paquete extra)"
    if not _pip_check(py):
        return False, "TAMPER: pip check falla en el entorno sellado"
    return True, "ok"


# --------------------------------------------------------------------------- build (transaccional)


def _expected_pins(lock_rel: str) -> dict[str, str]:
    return {_canon(m.group(1)): m.group(2) for m in _PIN.finditer((ROOT / lock_rel).read_text())}


def _pip(py: Path, *args: str) -> None:
    subprocess.run([str(py), "-m", "pip", *args], check=True, cwd=str(ROOT))


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
    ENVS_ROOT.mkdir(parents=True, exist_ok=True)
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(ENVS_ROOT, 0o700)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    lock_file = target.parent / f".lock-{env_id(profile, variant, profiles)}"
    with open(lock_file, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        ok, _ = ready_valid(target, profile, variant, profiles)  # re-check bajo el lock
        if ok:
            return target
        probs = lc.validate_all(ROOT)
        if probs:
            raise SystemExit("python_env: lockset/contrato inválido -> " + "; ".join(probs))
        lock_rel = lock_rel_for(profile, variant, profiles)
        staging = Path(tempfile.mkdtemp(dir=str(STAGING_ROOT), prefix=f"{env_id(profile, variant, profiles)}."))
        os.chmod(staging, 0o700)
        try:
            venv.create(staging, with_pip=True, clear=True)
            py = _venv_python(staging)
            _install(py, profile, cfg, profiles, lock_rel)
            if not _pip_check(py):
                raise SystemExit("python_env: pip check falla tras instalar")
            freeze = _pip_freeze(py)
            observed = {_canon(ln.split("==")[0]): ln.split("==")[1] for ln in freeze if "==" in ln}
            expected = _expected_pins(lock_rel)
            missing = {n: v for n, v in expected.items() if observed.get(n) != v}
            if missing:
                raise SystemExit(f"python_env: inventario observado != lock para {sorted(missing)}")
            meta = {
                "schema_version": 1,
                "env_id": env_id(profile, variant, profiles),
                "descriptor": descriptor(profile, variant, profiles),
                "inventory": freeze,
                "inventory_digest": _inventory_digest(freeze),
                "file_hashes": _file_hashes(staging, cfg),
                "pip_check": "ok",
                "n_packages": len(freeze),
            }
            (staging / "READY.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
            with open(staging / "READY.json", "rb") as fh:
                os.fsync(fh.fileno())
            _fsync_dir(staging)
            os.replace(staging, target)
            _fsync_dir(target.parent)  # el rename se persiste con fsync del PADRE
            return target
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prune_staging() -> int:
    """Borra SOLO `.vp_envs/.staging/` (nunca un entorno sellado)."""
    n = 0
    if STAGING_ROOT.exists():
        for child in STAGING_ROOT.iterdir():
            shutil.rmtree(child, ignore_errors=True)
            n += 1
    return n


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


def _enforce_cache_guard(cfg: dict) -> None:
    if cfg.get("cache_guarded"):
        from tools import dvc_cache_guard

        dvc_cache_guard.enforce(ROOT)


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
    _enforce_cache_guard(cfg)
    py = _venv_python(env_dir(profile, variant, profiles)).resolve()
    # NO se prepende el bin del env al PATH ⇒ los subprocesos de stage (`python -m ...` de dvc repro)
    # heredan el PATH ambiente = intérprete del PRODUCTO, jamás el del CLI DVC (R3.7).
    return subprocess.run([str(py), str(binp), *rest], check=False, cwd=str(ROOT), capture_output=capture, text=capture)


def run_python(
    profile: str, argv: list[str], *, variant: str | None = None, capture: bool = False, profiles: dict | None = None
) -> subprocess.CompletedProcess:
    """Ejecuta `<env>/bin/python <argv>` del perfil (para el call graph del producto)."""
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    target = build(profile, variant, profiles)
    _enforce_cache_guard(cfg)
    py = _venv_python(target).resolve()
    return subprocess.run([str(py), *argv], check=False, cwd=str(ROOT), capture_output=capture, text=capture)


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
