#!/usr/bin/env python3
"""Guardarraíl tipográfico del entregable: PROHÍBE la raya larga (em-dash, U+2014 '—')
en ``reports/latex/ProyectoI_VisaPredictAI.tex``.

Decisión del autor (jul-2026): el entregable ProyectoI no usa raya larga. El estilo es
paréntesis para incisos, coma o dos puntos para cortes, y guion simple para compuestos.
Este hook corre en ``pre-commit`` y ``pre-push`` (ver .pre-commit-config.yaml) y también
puede invocarse a mano: ``ante/bin/python tools/check_no_emdash.py``. Sale con código 1 y
lista las líneas ofensoras si encuentra alguna raya larga.

Alcance deliberado: SOLO el .tex del ProyectoI (el entregable firmado). Los artículos de
MICAI y otros .tex quedan fuera; ahí la raya larga sigue permitida.
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "reports/latex/ProyectoI_VisaPredictAI.tex"
EM_DASH = "—"  # — (raya larga / em dash)


def main() -> int:
    if not TARGET.exists():
        print(f"check_no_emdash: no se encontró {TARGET}", file=sys.stderr)
        return 0  # no bloquear si el archivo no está (p. ej. checkout parcial)
    hits = [
        (i, line.strip())
        for i, line in enumerate(TARGET.read_text(encoding="utf-8").splitlines(), 1)
        if EM_DASH in line
    ]
    if hits:
        print(f"✗ {len(hits)} línea(s) con raya larga (—) prohibida en {TARGET.name}:")
        for i, line in hits[:40]:
            print(f"   L{i}: {line[:90]}")
        if len(hits) > 40:
            print(f"   … y {len(hits) - 40} más")
        print("Reemplaza: paréntesis (inciso) · coma o dos puntos (corte) · guion simple (compuesto).")
        return 1
    print(f"✓ {TARGET.name}: sin raya larga (—)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
