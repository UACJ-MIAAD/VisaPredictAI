#!/usr/bin/env python
"""Gate P0R.5 (D10): CERO instalaciones de DVC fuera del lock dvc-tool HASHEADO.

DVC es una herramienta CLI AISLADA del producto: arrastra `diskcache 5.6.3` con PYSEC-2026-2447
(RCE vía pickle, sin fix), aceptado ACOTADO al perfil dvc-tool en `security/python_advisories.json`.
Cualquier `pip install` de dvc que NO venga del lock hasheado reintroduciría una versión de diskcache
sin gobernar. Este guard escanea los workflows, los scripts shell y el Makefile y exige que toda
instalación de dvc use EXACTAMENTE la forma aprobada:

    pip install --require-hashes -r locks/dvc-tool-linux-x86_64.txt [--quiet]

Además exige que toda invocación de `dvc` en workflows/scripts pase por el cache guard
(`tools.dvc_cache_guard --run`), nunca el binario suelto (mitiga la superficie de la caché).

    python -m tools.check_no_stray_dvc      # exit 1 si hay una instalación/uso fuera de política
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPROVED_INSTALL = "pip install --require-hashes -r locks/dvc-tool-linux-x86_64.txt"
GUARD_TOKEN = "dvc_cache_guard"


def _targets(root: Path) -> list[Path]:
    """Ficheros donde dvc puede instalarse o invocarse."""
    return (
        sorted((root / ".github" / "workflows").glob("*.yml"))
        + sorted((root / "experiments").glob("*.sh"))
        + [root / "Makefile"]
    )


# `pip install ... dvc ...` (dvc como token, dvc==, dvc[s3]) — NO matchea `.[dev]`/`.[model]`.
_PIP_DVC = re.compile(r"pip\s+install\b.*(?<![\w.-])dvc(?:\[[^\]]*\])?(?:==|\b)")
# Una invocación real del binario dvc (arranque de comando o tras `python -m ... --run`),
# excluyendo `$DVC`, `$(DVC)`, `dvc.lock`, `.dvc`, y las referencias a rutas `locks/dvc-tool`.
_DVC_CMD = re.compile(
    r"(?:^\s*|[;&|]\s*|--run\s+\S*\s+)dvc(?:\.exe)?\s+(?:add|push|pull|commit|repro|dag|status|checkout|fetch|gc|remove)\b"
)


def _strip_comment(line: str) -> str:
    """Quita comentarios de shell/YAML (`#`) respetando que no haya `#` dentro de literales simples."""
    out, in_s, in_d = [], False, False
    for ch in line:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            break
        out.append(ch)
    return "".join(out)


def check(root: Path = ROOT) -> list[str]:
    probs: list[str] = []
    for path in _targets(root):
        if not path.exists():
            continue
        rel = path.relative_to(root)
        for i, raw in enumerate(path.read_text().splitlines(), 1):
            line = _strip_comment(raw)
            if _PIP_DVC.search(line):
                norm = " ".join(line.split())
                # aprobada = la forma exacta (con o sin --quiet al final), nada más pegado.
                allowed = norm.startswith(APPROVED_INSTALL) and set(norm[len(APPROVED_INSTALL) :].split()) <= {
                    "--quiet"
                }
                if not allowed:
                    probs.append(f"{rel}:{i}: instala dvc fuera del lock dvc-tool hasheado → {norm}")
            if _DVC_CMD.search(line) and GUARD_TOKEN not in line and "$DVC" not in line and "$(DVC)" not in line:
                probs.append(f"{rel}:{i}: invoca dvc sin el cache guard (dvc_cache_guard --run) → {line.strip()}")
    return probs


def main() -> int:
    probs = check()
    if probs:
        print("✗ CHECK NO-STRAY-DVC bloqueó (P0R.5 D10):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(
        f"✓ DVC gobernado: {len(_targets(ROOT))} ficheros; toda instalación usa el lock dvc-tool, todo uso pasa por el guard"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
