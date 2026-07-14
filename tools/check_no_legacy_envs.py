#!/usr/bin/env python
"""Gate anti-entornos legacy (P0R.5, C4/B2). El call graph oficial debe MIGRAR de `ante/bin/*` y
`ante_nf/bin/*` a la interfaz gobernada `python -m tools.python_env run-python --profile <P> [--variant V]`.
Este gate es un TRINQUETE: cuenta las referencias a los intérpretes legacy por fichero y las compara con
`docs/legacy_env_baseline.json`. Cualquier fichero que SUPERE su baseline (uso legacy NUEVO) FALLA; bajar
el baseline (migrar) es el único cambio permitido. Los directorios físicos `ante/`/`ante_nf/` se conservan
hasta la autorización de cutover; aquí solo se retira su AUTORIDAD en el código, de forma medible.

    python -m tools.check_no_legacy_envs      # exit 1 si aparece uso legacy nuevo o el baseline quedó stale
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "legacy_env_baseline.json"
_LEGACY = re.compile(r"\bante(_nf)?/bin/")
_SCAN_EXT = (".sh", ".yml", ".yaml")
_SCAN_BASE = ("Makefile", "dvc.yaml")


def _tracked(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(root), capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SystemExit(f"check_no_legacy_envs: `git ls-files` falló ({exc}) — fail-closed") from exc
    return [f for f in out.split("\0") if f and (f.endswith(_SCAN_EXT) or Path(f).name in _SCAN_BASE)]


def current_counts(root: Path = ROOT) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in _tracked(root):
        try:
            n = len(_LEGACY.findall((root / rel).read_text()))
        except OSError, UnicodeDecodeError:
            continue
        if n:
            counts[rel] = n
    return counts


def check(root: Path = ROOT) -> list[str]:
    baseline = json.loads(BASELINE.read_text()).get("max_per_file", {})
    counts = current_counts(root)
    probs: list[str] = []
    for rel, n in counts.items():
        allowed = baseline.get(rel, 0)
        if n > allowed:
            probs.append(f"{rel}: {n} refs a ante/ante_nf (baseline {allowed}) — migra a `python_env run-python`")
    # baseline stale: un fichero que YA migró (bajó a 0) debe salir del baseline
    for rel, allowed in baseline.items():
        if allowed and counts.get(rel, 0) < allowed:
            probs.append(
                f"{rel}: bajó de {allowed} a {counts.get(rel, 0)} refs — ACTUALIZA docs/legacy_env_baseline.json (el trinquete no permite holgura)"
            )
    return probs


def main() -> int:
    probs = check()
    if probs:
        print("✗ CHECK NO-LEGACY-ENVS (trinquete C4/B2):")
        for p in probs:
            print(f"  - {p}")
        return 1
    total = sum(current_counts().values())
    print(
        f"✓ Sin uso legacy nuevo: {total} refs a ante/ante_nf en el call graph (trinquete; migración en curso a run-python)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
