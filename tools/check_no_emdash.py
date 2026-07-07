#!/usr/bin/env python3
"""Guardarraíl tipográfico del entregable: prohíbe la raya larga (em-dash, U+2014 '—')
usada como DELIMITADOR DE INCISO en ``reports/latex/ProyectoI_VisaPredictAI.tex``.

Decisión del autor (jul-2026), MATIZADA: la raya larga queda proscrita SOLO cuando encierra
un inciso (patrón ``texto —inciso— texto``). En su lugar: paréntesis o comas. Los usos como
SEPARADOR de rango o datos SÍ se permiten (``Agosto—Diciembre``, ``A — B``); para rangos, de
todos modos, la tipografía correcta es la raya corta ``--`` (en-dash).

Cómo se distingue: una raya-delimitadora de inciso tiene espacio en UN solo lado
(``\\s—\\S`` al abrir, ``\\S—\\s`` al cerrar). Un rango pegado (``\\S—\\S``) o un separador con
espacio a ambos lados (``\\s—\\s``) NO son incisos y se permiten. La raya de inciso en español
va pegada por dentro, así que esta regla la caza en cualquier posición de línea.

Corre en ``pre-commit`` y ``pre-push`` (ver .pre-commit-config.yaml) y también a mano:
``ante/bin/python tools/check_no_emdash.py``. Sale 1 y lista las líneas ofensoras si halla alguna.
Alcance: SOLO el .tex del ProyectoI (entregable firmado); MICAI y otros quedan fuera.
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "reports/latex/ProyectoI_VisaPredictAI.tex"
EM_DASH = "—"  # — (raya larga / em dash)


def aside_delimiter_hits(text: str) -> list[tuple[int, str]]:
    """Posiciones de em-dash usadas como delimitador de inciso (espacio en un solo lado)."""
    hits: list[tuple[int, str]] = []
    for i, ch in enumerate(text):
        if ch != EM_DASH:
            continue
        before = text[i - 1] if i > 0 else "\n"
        after = text[i + 1] if i + 1 < len(text) else "\n"
        # inciso = exactamente UN lado con espacio/frontera; rango/separador = ambos iguales
        if before.isspace() != after.isspace():
            line_no = text.count("\n", 0, i) + 1
            line = text.splitlines()[line_no - 1].strip()
            hits.append((line_no, line))
    return hits


def main() -> int:
    if not TARGET.exists():
        print(f"check_no_emdash: no se encontró {TARGET}", file=sys.stderr)
        return 0
    hits = aside_delimiter_hits(TARGET.read_text(encoding="utf-8"))
    if hits:
        # deduplicar por línea (una línea puede tener las dos rayas del inciso)
        seen: dict[int, str] = {}
        for ln, txt in hits:
            seen.setdefault(ln, txt)
        print(f"✗ {len(seen)} línea(s) con raya larga (—) usada como INCISO en {TARGET.name}:")
        for ln, txt in list(seen.items())[:40]:
            print(f"   L{ln}: {txt[:90]}")
        print("Los incisos van con paréntesis o comas. (Rangos como 'Agosto--Diciembre' sí se permiten.)")
        return 1
    print(f"✓ {TARGET.name}: sin raya larga (—) como inciso")
    return 0


if __name__ == "__main__":
    sys.exit(main())
