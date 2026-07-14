#!/usr/bin/env python
"""Coherencia docs ↔ fuente machine-readable de advisories (P0R.3, ronda 10).

La AUTORIDAD de supply chain es ``security/python_advisories.json`` reconciliado contra los
locks por ``tools/audit_python_supply_chain.py`` (biyección exacta por perfil). Este check es
el guard DOCUMENTAL complementario: la prosa de seguridad no debe derivar del JSON. Verifica:

  * el nº de advisories del JSON == filas de la tabla de ``SECURITY_TRIAGE.md``;
  * cada ID del JSON (o un alias) aparece en alguna fila de la tabla;
  * los conteos "N avisos" de SECURITY_TRIAGE.md y THREAT_MODEL.md == nº del JSON.

(Antes parseaba ``--ignore-vuln`` del workflow; ese ya NO es autoridad — el workflow llama al
runner. Stdlib puro; corre en el job ``consistency`` de CI.)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADVISORIES = ROOT / "security" / "python_advisories.json"
TRIAGE = ROOT / "docs" / "SECURITY_TRIAGE.md"
THREAT = ROOT / "docs" / "THREAT_MODEL.md"
_ADV = re.compile(r"\b(?:CVE-\d{4}-\d+|PYSEC-\d{4}-\d+|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})\b")


def _json_ids() -> list[set[str]]:
    """IDs/alias de cada advisory del JSON. Guard DOCUMENTAL: no valida el esquema completo
    (eso lo hace autoritativamente el runner tools/audit_python_supply_chain.py en el job
    supply-chain). Un JSON malformado lanza aquí y main() lo captura -> fallo. Se lee inline
    (sin importar del runner) para no depender del modo script/paquete (mypy no-redef)."""
    obj = json.loads(ADVISORIES.read_text())
    return [{e["id"], *(e.get("aliases") or [])} for e in obj["advisories"]]


def _triage_row_ids(text: str) -> list[list[str]]:
    # Solo la sección vigente: otros historiales/tablas de seguridad no son allowlist.
    m = re.search(r"^## Triage vigente[^\n]*\n(?P<body>.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    if not m:
        return []
    rows = []
    for line in m.group("body").splitlines():
        s = line.strip()
        if not s.startswith("|") or s.startswith("|---") or "Aviso" in s:
            continue
        ids = _ADV.findall(s)
        if ids:
            rows.append(ids)
    return rows


def _header_count(text: str) -> int | None:
    m = re.search(r"^## Triage vigente[^\n]*\((\d+)\s+avisos", text, flags=re.MULTILINE)
    return int(m.group(1)) if m else None


def _threat_count(text: str) -> int | None:
    m = re.search(r"MEDIO-BAJO:\s*(\d+)\s+avisos aceptados", text)
    return int(m.group(1)) if m else None


def main() -> int:
    probs: list[str] = []
    try:
        json_ids = _json_ids()
    except (OSError, ValueError) as exc:
        print(f"✗ DOCS ↔ advisories JSON abortado: {exc}")
        return 1
    n = len(json_ids)
    tr, th = TRIAGE.read_text(), THREAT.read_text()
    rows = _triage_row_ids(tr)

    if len(rows) != n:
        probs.append(f"SECURITY_TRIAGE.md: {len(rows)} filas de tabla != {n} advisories del JSON")
    matched: list[list[int]] = []
    for row_i, row in enumerate(rows):
        hits = [i for i, toks in enumerate(json_ids) if toks & set(row)]
        matched.append(hits)
        if len(hits) != 1:
            probs.append(f"SECURITY_TRIAGE.md fila #{row_i + 1}: debe casar con 1 advisory JSON, casa con {hits}")
    for i, toks in enumerate(json_ids):
        row_hits = [row_i for row_i, hits in enumerate(matched) if i in hits]
        if len(row_hits) != 1:
            probs.append(f"advisory {sorted(toks)} debe aparecer en 1 fila, aparece en {row_hits}")
    for name, cnt in (("SECURITY_TRIAGE.md", _header_count(tr)), ("THREAT_MODEL.md", _threat_count(th))):
        if cnt is None:
            probs.append(f"{name}: no se halló el conteo 'N avisos'")
        elif cnt != n:
            probs.append(f"{name}: prosa dice {cnt} avisos, el JSON tiene {n}")

    if probs:
        print(f"✗ DOCS ↔ advisories JSON incoherentes ({len(probs)}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ Docs coherentes con security/python_advisories.json: {n} avisos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
