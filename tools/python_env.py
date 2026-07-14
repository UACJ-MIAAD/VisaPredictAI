#!/usr/bin/env python
"""Entornos Python content-addressed (P0R.5, R2/R3). AÍSLA en TIEMPO DE EJECUCIÓN un perfil de
dependencias en su propio intérprete, direccionado por el hash de su cierre completo (lock + lockset
+ config de perfil + python + plataforma + toolchain + modo). Motivación: `dvc[s3]` fija
`requests`/`tqdm` MÁS NUEVOS que el producto; instalarlo en el intérprete de `model`/`freeze` degrada
las deps del producto (contaminación demostrada). La solución no es lock-aislar (ya lo estaba) sino
runtime-aislar: DVC corre desde `.vp_envs/dvc-tool/<env_id>/`, jamás desde el python del producto.

Uso:
  python -m tools.python_env env-id  --profile dvc-tool           # imprime el env_id determinista
  python -m tools.python_env path    --profile dvc-tool           # imprime .vp_envs/<profile>/<env_id>
  python -m tools.python_env build   --profile dvc-tool           # construye (transaccional) o reusa
  python -m tools.python_env exec    --profile dvc-tool -- dvc dag # ejecuta un console-script del env

`build` es transaccional: flock por env_id → valida lockset+perfil → staging con nonce → instala SOLO
del lock gobernado → `pip check` → compara esperado vs observado → digest de inventario → READY.json AL
FINAL → fsync → rename. Reusa SOLO un entorno cuyo READY.json revalide (recomputa el digest del
inventario vivo); si un entorno sellado fue ALTERADO, FALLA sin reparar. Sin fechas/PID/rutas/tmp en el
descriptor ⇒ env_id reproducible. Stdlib puro salvo el `pip` del propio venv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from tools import lock_contracts as lc

ROOT = lc.ROOT
ENVS_ROOT = ROOT / ".vp_envs"
STAGING_ROOT = ENVS_ROOT / ".staging"
PROFILES_JSON = ROOT / "environments" / "python_profiles.json"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", re.MULTILINE)


def _canon(name: str) -> str:
    """Nombre canónico PEP 503 (runs de -_. → un guion, minúsculas): flufl.lock == flufl-lock."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------- descriptor / env_id


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _sha256_path(p: Path) -> str:
    return _sha256_bytes(p.read_bytes())


def platform_key() -> str:
    """Clave estable de plataforma para elegir el lock: 'Darwin-arm64' / 'Linux-x86_64'."""
    return f"{platform.system()}-{platform.machine()}"


def _libc_or_macos() -> str:
    if platform.system() == "Darwin":
        return "macos-" + (platform.mac_ver()[0].split(".")[0] or "0")
    name, ver = platform.libc_ver()
    return f"{name or 'unknown'}-{'.'.join(ver.split('.')[:2]) if ver else '0'}"


def load_profiles() -> dict:
    return json.loads(PROFILES_JSON.read_text())


def _profile_config(profiles: dict, profile: str) -> dict:
    cfg = profiles.get("profiles", {}).get(profile)
    if cfg is None:
        raise SystemExit(f"python_env: perfil desconocido {profile!r}")
    return cfg


def lock_rel_for(profile: str, profiles: dict | None = None) -> str:
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    key = platform_key()
    lock = cfg.get("locks", {}).get(key)
    if lock is None:
        raise SystemExit(f"python_env: perfil {profile!r} sin lock para plataforma {key!r}")
    return lock


def descriptor(profile: str, profiles: dict | None = None) -> dict:
    """Descriptor CANÓNICO del entorno (fuente del env_id). Sin rutas/fechas/PID/tmp."""
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    lock_rel = lock_rel_for(profile, profiles)
    lock_path = ROOT / lock_rel
    manifest = ROOT / lc.MANIFEST_REL
    # El fragmento de config del perfil que afecta la resolución (sin `note`/campos informativos).
    pcfg = {
        "install_mode": cfg["install_mode"],
        "locks": cfg["locks"],
        "console_scripts": cfg.get("console_scripts", []),
    }
    return {
        "profile": profile,
        "lock_sha256": _sha256_path(lock_path),
        "lockset_sha256": _sha256_path(manifest),
        "profile_config_sha256": _sha256_bytes(json.dumps(pcfg, sort_keys=True).encode()),
        "python": {
            "implementation": platform.python_implementation().lower(),
            "version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag or "",
            "abi": (__import__("sysconfig").get_config_var("SOABI") or ""),
        },
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "libc_or_macos": _libc_or_macos(),
        },
        "toolchain": dict(profiles["toolchain"]),
        "install_mode": cfg["install_mode"],
    }


def env_id(profile: str, profiles: dict | None = None) -> str:
    d = descriptor(profile, profiles)
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def env_dir(profile: str, profiles: dict | None = None) -> Path:
    return ENVS_ROOT / profile / env_id(profile, profiles)


# --------------------------------------------------------------------------- inventario / READY


def _venv_python(env_path: Path) -> Path:
    return env_path / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")


def _pip_freeze(py: Path) -> list[str]:
    out = subprocess.run(
        [str(py), "-m", "pip", "freeze", "--all", "--disable-pip-version-check"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(line.strip() for line in out.splitlines() if line.strip() and not line.startswith("-e "))


def _inventory_digest(freeze: list[str]) -> str:
    return _sha256_bytes("\n".join(sorted(x.lower() for x in freeze)).encode())


def ready_valid(env_path: Path, profile: str, profiles: dict | None = None) -> tuple[bool, str]:
    """(válido, motivo). Reusa SOLO si READY.json existe, casa el env_id esperado y el digest del
    inventario VIVO coincide con el sellado (tamper ⇒ inválido; el llamador decide fallar sin reparar)."""
    ready = env_path / "READY.json"
    if not ready.exists():
        return False, "sin READY.json"
    try:
        meta = json.loads(ready.read_text())
    except (ValueError, OSError) as exc:
        return False, f"READY.json ilegible: {exc}"
    expected_id = env_id(profile, profiles)
    if meta.get("env_id") != expected_id:
        return False, f"env_id sellado {meta.get('env_id')!r} != esperado {expected_id!r}"
    py = _venv_python(env_path)
    if not py.exists():
        return False, "falta el intérprete del venv"
    try:
        live = _inventory_digest(_pip_freeze(py))
    except (subprocess.CalledProcessError, OSError) as exc:
        return False, f"no se pudo inventariar: {exc}"
    if live != meta.get("inventory_digest"):
        return False, "TAMPER: el inventario vivo difiere del sellado en READY.json"
    return True, "ok"


# --------------------------------------------------------------------------- build (transaccional)


def _install(py: Path, profile: str, profiles: dict, lock_rel: str) -> None:
    tc = profiles["toolchain"]
    subprocess.run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            f"pip=={tc['pip']}",
            f"setuptools=={tc['setuptools']}",
            f"wheel=={tc['wheel']}",
        ],
        check=True,
        cwd=str(ROOT),
    )
    mode = _profile_config(profiles, profile)["install_mode"]
    cmd = [str(py), "-m", "pip", "install", "--no-deps" if mode == "hash-verified" else "--disable-pip-version-check"]
    # hash-verified: cierre COMPLETO y hasheado ⇒ --require-hashes (implica el set exacto del lock).
    if mode == "hash-verified":
        cmd = [str(py), "-m", "pip", "install", "--require-hashes", "-r", str(ROOT / lock_rel)]
    else:
        cmd = [str(py), "-m", "pip", "install", "-r", str(ROOT / lock_rel)]
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def _expected_pins(lock_rel: str) -> dict[str, str]:
    text = (ROOT / lock_rel).read_text()
    return {_canon(m.group(1)): m.group(2) for m in _PIN.finditer(text)}


def build(profile: str, *, force: bool = False, profiles: dict | None = None) -> Path:
    profiles = profiles or load_profiles()
    target = env_dir(profile, profiles)
    if not force:
        ok, why = ready_valid(target, profile, profiles)
        if ok:
            return target
        if (target / "READY.json").exists() and why.startswith("TAMPER"):
            raise SystemExit(
                f"python_env: entorno sellado ALTERADO en {target} ({why}) — NO se repara; borra y reconstruye"
            )
    ENVS_ROOT.mkdir(parents=True, exist_ok=True)
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    (ENVS_ROOT / profile).mkdir(parents=True, exist_ok=True)
    lock_file = ENVS_ROOT / profile / f".lock-{env_id(profile, profiles)}"
    import fcntl

    with open(lock_file, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        # Re-chequeo bajo el lock (otro proceso pudo construirlo).
        ok, _ = ready_valid(target, profile, profiles)
        if ok and not force:
            return target
        probs = lc.validate_all(ROOT)
        if probs:
            raise SystemExit("python_env: lockset/contrato inválido -> " + "; ".join(probs))
        lock_rel = lock_rel_for(profile, profiles)
        fd, staging_name = tempfile.mkstemp(dir=str(STAGING_ROOT), prefix=f"{env_id(profile, profiles)}.")
        os.close(fd)
        os.unlink(staging_name)
        staging = Path(staging_name)
        try:
            venv.create(staging, with_pip=True, clear=True)
            py = _venv_python(staging)
            _install(py, profile, profiles, lock_rel)
            subprocess.run([str(py), "-m", "pip", "check"], check=True, cwd=str(ROOT))
            freeze = _pip_freeze(py)
            observed = {_canon(ln.split("==")[0]): ln.split("==")[1] for ln in freeze if "==" in ln}
            expected = _expected_pins(lock_rel)
            missing = {n: v for n, v in expected.items() if observed.get(n) != v}
            if missing:
                raise SystemExit(f"python_env: inventario observado != lock para {sorted(missing)}")
            meta = {
                "schema_version": 1,
                "env_id": env_id(profile, profiles),
                "descriptor": descriptor(profile, profiles),
                "inventory_digest": _inventory_digest(freeze),
                "pip_check": "ok",
                "n_packages": len(freeze),
            }
            (staging / "READY.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
            # fsync fichero + directorio antes de promover.
            with open(staging / "READY.json", "rb") as fh:
                os.fsync(fh.fileno())
            dfd = os.open(str(staging), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            if target.exists():
                import shutil

                shutil.rmtree(target)
            os.replace(staging, target)
            return target
        except BaseException:
            import shutil

            shutil.rmtree(staging, ignore_errors=True)
            raise


# --------------------------------------------------------------------------- exec


def resolve_console_script(profile: str, name: str, profiles: dict | None = None) -> Path:
    """Ruta ABSOLUTA del console-script `name` DENTRO del entorno certificado del perfil (build si falta)."""
    profiles = profiles or load_profiles()
    cfg = _profile_config(profiles, profile)
    if name not in cfg.get("console_scripts", []):
        raise SystemExit(f"python_env: {name!r} no es console-script declarado del perfil {profile!r}")
    target = build(profile, profiles=profiles)
    binp = target / ("Scripts" if os.name == "nt" else "bin") / name
    if not binp.exists():
        raise SystemExit(f"python_env: {name} ausente en {binp}")
    return binp.resolve()


def env_owns(profile: str, executable: Path, profiles: dict | None = None) -> bool:
    """True si `executable` (realpath) vive DENTRO del entorno certificado del perfil."""
    try:
        executable.resolve().relative_to(env_dir(profile, profiles).resolve())
        return True
    except ValueError, OSError:
        return False


def run(
    profile: str, argv: list[str], *, capture: bool = False, profiles: dict | None = None
) -> subprocess.CompletedProcess:
    """Ejecuta un console-script del perfil en su entorno aislado (build+reuse), aplicando el cache
    guard si el perfil es cache-guarded. `capture=True` devuelve stdout/stderr en la CompletedProcess."""
    if not argv:
        raise SystemExit("python_env: falta el comando")
    profiles = profiles or load_profiles()
    name, rest = argv[0], argv[1:]
    binp = resolve_console_script(profile, name, profiles)
    # R4.7: el ejecutable DEBE vivir dentro del entorno certificado del perfil.
    if not env_owns(profile, binp, profiles):
        raise SystemExit(f"python_env: {binp} fuera del entorno certificado de {profile!r}")
    # Perfiles cache-guarded (dvc-tool): prepara+valida la caché y fija umask 077 antes de ejecutar.
    if _profile_config(profiles, profile).get("cache_guarded"):
        from tools import dvc_cache_guard

        dvc_cache_guard.enforce(ROOT)
    py = _venv_python(env_dir(profile, profiles)).resolve()
    # Se ejecuta el console-script A TRAVÉS del python del env (no por su shebang): el venv promovido
    # trae shebangs con la ruta de STAGING (no relocatable) y además exceden el límite del kernel. Correr
    # `<env>/bin/python <env>/bin/dvc` fija las deps aisladas del env por sys.executable. NO se prepende
    # el bin del env al PATH ⇒ los subprocesos de stage (`python -m ...` de `dvc repro`) heredan el PATH
    # ambiente = intérprete del PRODUCTO, jamás el del CLI DVC (R3.7).
    return subprocess.run([str(py), str(binp), *rest], check=False, cwd=str(ROOT), capture_output=capture, text=capture)


def _exec(profile: str, argv: list[str], profiles: dict | None = None) -> int:
    if not argv:
        raise SystemExit("python_env exec: falta el comando tras `--`")
    return run(profile, argv, profiles=profiles).returncode


# --------------------------------------------------------------------------- CLI


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("env-id", "path", "build"):
        p = sub.add_parser(c)
        p.add_argument("--profile", required=True)
        if c == "build":
            p.add_argument("--force", action="store_true")
    pe = sub.add_parser("exec")
    pe.add_argument("--profile", required=True)
    pe.add_argument("rest", nargs=argparse.REMAINDER)
    ns = ap.parse_args(argv[1:])

    if ns.cmd == "env-id":
        print(env_id(ns.profile))
        return 0
    if ns.cmd == "path":
        print(env_dir(ns.profile))
        return 0
    if ns.cmd == "build":
        target = build(ns.profile, force=ns.force)
        print(f"✓ entorno {ns.profile} listo: {target}")
        return 0
    if ns.cmd == "exec":
        rest = ns.rest
        if rest and rest[0] == "--":
            rest = rest[1:]
        return _exec(ns.profile, rest)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
