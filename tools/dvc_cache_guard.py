#!/usr/bin/env python
"""Guard de la caché DVC (P0R.5, R4/B11). MITIGA la superficie de PYSEC-2026-2447 (diskcache usa
pickle; RCE si un atacante ESCRIBE en el dir de caché y la víctima lo LEE después). NO corrige el
aviso — reduce la superficie CONFINANDO la caché DENTRO del repo:

  - ⚠️ B11: DiskCache vive sobre todo en `Repo.site_cache_dir` (por defecto `/Library/Caches/dvc/repo`
    en macOS o `/var/tmp/dvc/...` en Linux, con padres 0777). `enforce()` fija
    `DVC_SITE_CACHE_DIR=<repo>/.dvc/site-cache` (NO confía en el heredado) y lo crea 0700;
  - inspecciona `.dvc/config`/`.dvc/config.local` + capas GLOBAL/SYSTEM (y las apuntadas por
    `DVC_GLOBAL_CONFIG_DIR`/`DVC_SYSTEM_CONFIG_DIR`) por overrides de caché;
  - `lstat` de CADA componente de `.dvc`, `.dvc/cache`, `.dvc/tmp`, `.dvc/site-cache` y su `repo/`
    (incluido el padre `.dvc`): rechaza symlink, dueño ajeno y `mode & 0o022 != 0`;
  - `prepare()` crea los directorios ausentes 0700; `enforce()` verifica ANTES y el smoke verifica el
    `site_cache_dir` OBSERVADO DESPUÉS.

Es una BIBLIOTECA que `tools.python_env` invoca antes de correr DVC (fija DVC_SITE_CACHE_DIR + umask
077 + revalidación pre-exec). El TOCTOU queda REDUCIDO, no eliminado.

  python -m tools.dvc_cache_guard            # solo verifica la caché del repo (exit 1 si insegura)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_CACHE_REL = ".dvc/site-cache"  # B11: raíz ÚNICA y confinada del site_cache_dir de DVC
# Claves que RE-apuntarían la caché fuera del repo (override prohibido), en cualquier capa de config.
_CACHE_OVERRIDE_KEYS = ("[cache]", "dir =", "dir=", "site_cache_dir", "cache =", "cache=", "remote_config")
_CONFIG_FILES = ("config", "config.local")


def site_cache_dir(root: Path = ROOT) -> Path:
    """La raíz confinada del site_cache_dir de DVC (dentro del repo)."""
    return root / SITE_CACHE_REL


def _external_config_layers() -> list[Path]:
    """Ficheros de config GLOBAL (usuario)/SYSTEM que DVC honra además de los del repo, incluidas las
    apuntadas por DVC_GLOBAL_CONFIG_DIR/DVC_SYSTEM_CONFIG_DIR. Un `cache.dir`/`site_cache_dir` externo
    en cualquiera alcanzaría la ejecución (Linux/macOS + XDG)."""
    home = Path.home()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    layers = [
        home / ".config" / "dvc" / "config",
        home / "Library" / "Application Support" / "dvc" / "config",
        Path("/etc/xdg/dvc/config"),
        Path("/etc/dvc/config"),
        Path("/Library/Application Support/dvc/config"),
    ]
    if xdg:
        layers.append(Path(xdg) / "dvc" / "config")
    for env_var in ("DVC_GLOBAL_CONFIG_DIR", "DVC_SYSTEM_CONFIG_DIR"):
        d = os.environ.get(env_var)
        if d:
            layers.append(Path(d) / "config")
    return layers


def _unsafe_stat(p: Path, label: str) -> list[str]:
    """Problemas de un nodo por lstat: symlink, dueño != UID, escribible por grupo/otros."""
    probs: list[str] = []
    st = p.lstat()
    import stat as _stat

    if _stat.S_ISLNK(st.st_mode):
        probs.append(f"{label} es symlink — prohibido")
        return probs  # no seguir el enlace
    if st.st_uid != os.getuid():
        probs.append(f"{label} no pertenece al UID actual ({st.st_uid} != {os.getuid()})")
    if st.st_mode & 0o022:
        probs.append(f"{label} escribible por grupo/otros (mode {oct(_stat.S_IMODE(st.st_mode))})")
    return probs


def _scan_override(path: Path, label: str) -> list[str]:
    try:
        text = path.read_text()
    except OSError, UnicodeDecodeError:
        return [f"{label} ilegible — fail-closed"]
    for key in _CACHE_OVERRIDE_KEYS:
        if key in text:
            return [f"{label} declara override de caché ({key!r}) — no permitido"]
    return []


def check(root: Path = ROOT) -> list[str]:
    """Lista de problemas de seguridad de la caché DVC (vacía = segura). Cubre repo + config.local +
    capas GLOBAL/SYSTEM, `.dvc` symlink/tipo, y permisos de cada componente."""
    probs: list[str] = []
    dvc_dir = root / ".dvc"
    # 0) `.dvc` NO puede ser symlink: exists() lo seguiría y evadiría todo lo demás (B7)
    if dvc_dir.is_symlink():
        return [".dvc es symlink — prohibido"]
    # 1) overrides de caché en config y config.local del repo
    for cfg_name in _CONFIG_FILES:
        cfg = dvc_dir / cfg_name
        if cfg.exists():
            probs += _scan_override(cfg, f".dvc/{cfg_name}")
    # 1b) capas GLOBAL (usuario) y SYSTEM: un cache.dir externo ahí también alcanza la ejecución (B7)
    for layer in _external_config_layers():
        if layer.exists():
            probs += _scan_override(layer, f"config externa {layer}")
    # 2) el padre .dvc debe existir, ser directorio y seguro aunque cache/tmp no existan aún
    if dvc_dir.exists():
        if not dvc_dir.is_dir():
            return [".dvc existe pero no es un directorio — prohibido"]
        probs += _unsafe_stat(dvc_dir, ".dvc")
        try:
            dvc_dir.resolve().relative_to(root.resolve())
        except ValueError, OSError:
            probs.append(".dvc resuelve fuera del repo")
    # 3) cada componente de cache/, tmp/, site-cache/ y site-cache/repo (B11: el site_cache_dir de
    #    DiskCache es la superficie REAL del aviso).
    for rel in ("cache", "tmp", "site-cache", "site-cache/repo"):
        p = dvc_dir / rel
        if not p.exists() and not p.is_symlink():
            continue  # prepare() lo creará con 0700; ausencia no es violación
        probs += _unsafe_stat(p, f".dvc/{rel}")
        if p.exists() and not p.is_symlink():
            if not p.is_dir():
                probs.append(f".dvc/{rel} existe pero no es un directorio — prohibido")
            try:
                p.resolve().relative_to(root.resolve())
            except ValueError, OSError:
                probs.append(f".dvc/{rel} resuelve fuera del repo")
    # 3b) B22: TODO descendiente del site-cache (los `repo/<token>/…` que crea DVC) por lstat.
    sc = dvc_dir / "site-cache"
    if sc.is_dir() and not sc.is_symlink():
        for d in sorted(sc.rglob("*")):
            probs += _unsafe_stat(d, str(d.relative_to(root)))
    # 4) B11: si DVC_SITE_CACHE_DIR está fijado, DEBE resolver dentro de <repo>/.dvc/site-cache
    env_scd = os.environ.get("DVC_SITE_CACHE_DIR")
    if env_scd:
        try:
            Path(env_scd).resolve().relative_to(site_cache_dir(root).resolve())
        except ValueError, OSError:
            probs.append(f"DVC_SITE_CACHE_DIR={env_scd} fuera de {SITE_CACHE_REL} — no permitido")
    return probs


def prepare(root: Path = ROOT) -> None:
    """Crea `.dvc/cache`, `.dvc/tmp`, `.dvc/site-cache` y `.dvc/site-cache/repo` ausentes, modo 0700."""
    dvc_dir = root / ".dvc"
    if not dvc_dir.exists():
        return
    for rel in ("cache", "tmp", "site-cache", "site-cache/repo"):
        p = dvc_dir / rel
        if not p.exists() and not p.is_symlink():
            p.mkdir(mode=0o700, parents=True, exist_ok=True)


def child_env(root: Path = ROOT) -> dict[str, str]:
    """B11/B22: prepara los dirs 0700, valida, y devuelve un ENV para el SUBPROCESO de DVC con
    `DVC_SITE_CACHE_DIR` confinado — SIN mutar el os.environ ni el umask del proceso PADRE. El umask 077
    se aplica solo en el hijo (preexec). SystemExit si la caché es insegura."""
    prepare(root)
    env = {**os.environ, "DVC_SITE_CACHE_DIR": str(site_cache_dir(root))}
    probs = check_with_env(root, env)
    if probs:
        raise SystemExit("✗ DVC CACHE GUARD bloqueó (superficie de PYSEC-2026-2447):\n  - " + "\n  - ".join(probs))
    return env


def check_with_env(root: Path, env: dict[str, str]) -> list[str]:
    """check() pero validando el `DVC_SITE_CACHE_DIR` del ENV dado (no del os.environ del padre)."""
    saved = os.environ.get("DVC_SITE_CACHE_DIR")
    os.environ["DVC_SITE_CACHE_DIR"] = env["DVC_SITE_CACHE_DIR"]
    try:
        return check(root)
    finally:
        if saved is None:
            os.environ.pop("DVC_SITE_CACHE_DIR", None)
        else:
            os.environ["DVC_SITE_CACHE_DIR"] = saved


def main() -> int:
    probs = check()
    if probs:
        print("✗ DVC CACHE GUARD bloqueó (superficie de PYSEC-2026-2447):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print("✓ DVC cache guard OK — caché solo-usuario, sin symlink ni override (config + config.local)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
