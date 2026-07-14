#!/usr/bin/env python
"""Guard de la caché DVC (P0R.5, R4). MITIGA la superficie de PYSEC-2026-2447 (diskcache usa pickle;
RCE si un atacante ESCRIBE en el dir de caché y la víctima lo LEE después). NO corrige el aviso —
reduce la superficie:

  - inspecciona `.dvc/config` **y** `.dvc/config.local` en busca de overrides que re-apunten la caché
    fuera de `<repo>/.dvc/cache`;
  - `lstat` de CADA componente de `.dvc`, `.dvc/cache` y `.dvc/tmp` (incluido el padre `.dvc`): rechaza
    symlink, propietario distinto al UID actual y `mode & 0o022 != 0` (escribible por grupo/otros);
  - si `cache/` o `tmp/` no existen, valida igualmente que el padre `.dvc` sea seguro;
  - `prepare()` crea los directorios ausentes con modo 0700 (no side-effect de `check`).

Es una BIBLIOTECA que invoca `tools.python_env exec` antes de correr DVC (umask 077 + revalidación
inmediata pre-exec). Ya NO expone `--run <cualquier comando>` (evita ejecutar binarios arbitrarios ni
un dvc fuera del entorno gobernado). El TOCTOU queda REDUCIDO, no eliminado.

  python -m tools.dvc_cache_guard            # solo verifica la caché del repo (exit 1 si insegura)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Claves que RE-apuntarían la caché fuera de <repo>/.dvc/cache (override prohibido), en cualquiera de
# las capas de config del repo.
_CACHE_OVERRIDE_KEYS = ("[cache]", "dir =", "dir=", "site_cache_dir", "cache =", "cache=")
_CONFIG_FILES = ("config", "config.local")


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


def check(root: Path = ROOT) -> list[str]:
    """Lista de problemas de seguridad de la caché DVC del repo (vacía = segura)."""
    probs: list[str] = []
    dvc_dir = root / ".dvc"
    # 1) overrides de caché en config y config.local
    for cfg_name in _CONFIG_FILES:
        cfg = dvc_dir / cfg_name
        if cfg.exists():
            text = cfg.read_text()
            for key in _CACHE_OVERRIDE_KEYS:
                if key in text:
                    probs.append(f".dvc/{cfg_name} declara override de caché ({key!r}) — no permitido")
                    break
    # 2) el padre .dvc debe ser seguro aunque cache/tmp no existan aún
    if dvc_dir.exists():
        probs += _unsafe_stat(dvc_dir, ".dvc")
        try:
            dvc_dir.resolve().relative_to(root.resolve())
        except ValueError, OSError:
            probs.append(".dvc resuelve fuera del repo")
    # 3) cada componente de cache/ y tmp/
    for name in ("cache", "tmp"):
        p = dvc_dir / name
        if not p.exists() and not p.is_symlink():
            continue  # prepare() lo creará con 0700; ausencia no es violación
        probs += _unsafe_stat(p, f".dvc/{name}")
        if p.exists() and not p.is_symlink():
            try:
                p.resolve().relative_to(root.resolve())
            except ValueError, OSError:
                probs.append(f".dvc/{name} resuelve fuera del repo")
    return probs


def prepare(root: Path = ROOT) -> None:
    """Crea `.dvc/cache` y `.dvc/tmp` ausentes con modo 0700 (solo-usuario)."""
    dvc_dir = root / ".dvc"
    if not dvc_dir.exists():
        return
    for name in ("cache", "tmp"):
        p = dvc_dir / name
        if not p.exists() and not p.is_symlink():
            p.mkdir(mode=0o700, parents=True, exist_ok=True)


def enforce(root: Path = ROOT) -> None:
    """prepare + check + umask 077, o SystemExit si la caché es insegura. Lo llama python_env antes
    de ejecutar DVC (revalidación inmediata pre-exec: reduce el TOCTOU)."""
    prepare(root)
    probs = check(root)
    if probs:
        raise SystemExit("✗ DVC CACHE GUARD bloqueó (superficie de PYSEC-2026-2447):\n  - " + "\n  - ".join(probs))
    os.umask(0o077)


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
