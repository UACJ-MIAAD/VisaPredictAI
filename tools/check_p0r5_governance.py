#!/usr/bin/env python
"""B263: lista POSITIVA de que los gates de gobernanza P0R.5 siguen cableados como PASOS NOMBRADOS en CI.

Antes, `check_commit_frontier`, `check_reflection`, `check_safe_opens`, `check_raw_fs_mutations` y
`validate_b233_receipt` sólo corrían por TRANSITIVIDAD de pytest — funcional, pero fácil de retirar sin darse cuenta.
Este gate exige que CADA uno aparezca en un comando `run:` del workflow `.github/workflows/ci.yml`. NO es un wrapper que
ejecute los gates ocultando cuál falla; es una verificación de CONFIGURACIÓN: si un paso se elimina del CI, este gate
falla nombrando el que falta. Fail-closed ante workflow ausente/ilegible.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKFLOW = ".github/workflows/ci.yml"
# Tokens (ruta de tool o módulo) que DEBEN aparecer en algún comando `run:` del workflow.
_REQUIRED_GATES = (
    "tools/check_commit_frontier.py",
    "tools/check_reflection.py",
    "tools/check_safe_opens.py",
    "tools/check_raw_fs_mutations.py",
    "tools.validate_b233_receipt",
    "tools/check_p0r5_governance.py",
)


def _run_commands(text: str) -> list[str]:
    """Extrae los comandos de todos los bloques `run:` del YAML (línea única y bloques `run: |`), sin dependencia YAML."""
    cmds: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if stripped == "run: |" or stripped.startswith("run: |"):
            i += 1
            while i < len(lines) and (not lines[i].strip() or (len(lines[i]) - len(lines[i].lstrip())) > indent):
                if lines[i].strip():
                    cmds.append(lines[i].strip())
                i += 1
            continue
        if stripped.startswith("run:"):
            cmds.append(stripped[len("run:") :].strip())
        i += 1
    return cmds


def problems() -> list[str]:
    path = os.path.join(ROOT, _WORKFLOW)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return [f"{_WORKFLOW}: ilegible ({exc}) (fail-closed B263)"]
    cmds = _run_commands(text)
    blob = "\n".join(cmds)
    return [
        f"gate de gobernanza NO CABLEADO en {_WORKFLOW}: falta un paso `run:` que invoque {gate!r} (B263)"
        for gate in _REQUIRED_GATES
        if gate not in blob
    ]


def main() -> int:
    probs = problems()
    if probs:
        print("✗ gobernanza P0R.5 no cableada en CI:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ los {len(_REQUIRED_GATES)} gates de gobernanza P0R.5 están cableados como pasos de {_WORKFLOW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
