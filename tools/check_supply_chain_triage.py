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
    obj = json.loads(ADVISORIES.read_text())
    return [{e["id"], *(e.get("aliases") or [])} for e in obj["advisories"]]


def _triage_row_ids(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or s.startswith("|---") or "Aviso" in s:
            continue
        ids = _ADV.findall(s)
        if ids:
            rows.append(ids)
    return rows


def _header_count(text: str) -> int | None:
    m = re.search(r"(\d+)\s+avisos", text)
    return int(m.group(1)) if m else None


def main() -> int:
    probs: list[str] = []
    json_ids = _json_ids()
    n = len(json_ids)
    tr, th = TRIAGE.read_text(), THREAT.read_text()
    rows = _triage_row_ids(tr)

    if len(rows) != n:
        probs.append(f"SECURITY_TRIAGE.md: {len(rows)} filas de tabla != {n} advisories del JSON")
    triage_tokens = {i for row in rows for i in row}
    for toks in json_ids:
        if not (toks & triage_tokens):
            probs.append(f"advisory {sorted(toks)} en el JSON pero SIN fila en SECURITY_TRIAGE.md")
    for name, cnt in (("SECURITY_TRIAGE.md", _header_count(tr)), ("THREAT_MODEL.md", _header_count(th))):
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
