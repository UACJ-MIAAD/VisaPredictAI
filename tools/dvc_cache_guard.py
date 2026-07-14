#!/usr/bin/env python
"""Guard de la caché DVC (P0R.5, D5). MITIGA la superficie de PYSEC-2026-2447 (diskcache usa pickle;
RCE si un atacante ESCRIBE en el dir de caché y la víctima lo LEE después). NO corrige el aviso —
lo reduce: exige que `.dvc/cache` y `.dvc/tmp` resuelvan DENTRO del repo, no sean symlink, pertenezcan
al UID actual y no sean escribibles por grupo/otros (`mode & 0o022 == 0`), y prohíbe overrides de
caché externa en `.dvc/config`. Ejecuta DVC con umask 077.

  python -m tools.dvc_cache_guard                 # solo verifica (exit 1 si insegura)
  python -m tools.dvc_cache_guard --run dvc dag   # verifica + ejecuta `dvc dag` con umask 077

Toda invocación oficial de DVC DEBE pasar por este guard; nunca el binario suelto desde PATH.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Claves de .dvc/config que RE-apuntarían la caché fuera de <repo>/.dvc/cache (override prohibido).
_CACHE_OVERRIDE_KEYS = ("[cache]", "dir =", "dir=", "site_cache_dir", "cache =", "cache=")


def check(root: Path = ROOT) -> list[str]:
    """Devuelve la lista de problemas de seguridad de la caché DVC (vacía = segura)."""
    probs: list[str] = []
    dvc_dir = root / ".dvc"
    cfg = dvc_dir / "config"
    if cfg.exists():
        text = cfg.read_text()
        for key in _CACHE_OVERRIDE_KEYS:
            if key in text:
                probs.append(f".dvc/config declara override de caché ({key!r}) — no permitido")
                break
    for name in ("cache", "tmp"):
        p = dvc_dir / name
        if p.is_symlink():
            probs.append(f".dvc/{name} es symlink — prohibido")
            continue
        if not p.exists():
            continue  # se creará con umask 077; ausencia no es violación
        try:
            p.resolve().relative_to(root.resolve())
        except ValueError, OSError:
            probs.append(f".dvc/{name} resuelve fuera del repo")
        st = p.stat()
        if st.st_uid != os.getuid():
            probs.append(f".dvc/{name} no pertenece al UID actual ({st.st_uid} != {os.getuid()})")
        if st.st_mode & 0o022:
            probs.append(f".dvc/{name} escribible por grupo/otros (mode {oct(st.st_mode & 0o777)})")
    return probs


def main(argv: list[str]) -> int:
    probs = check()
    if probs:
        print("✗ DVC CACHE GUARD bloqueó (superficie de PYSEC-2026-2447):")
        for p in probs:
            print(f"  - {p}")
        return 1
    if len(argv) > 1 and argv[1] == "--run":
        cmd = argv[2:]
        if not cmd:
            print("✗ --run sin comando")
            return 1
        os.umask(0o077)  # la caché/artefactos DVC se crean solo-usuario
        return subprocess.run(cmd, check=False).returncode
    print("✓ DVC cache guard OK — caché solo-usuario, sin symlink ni override")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
