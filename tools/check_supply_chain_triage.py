#!/usr/bin/env python
"""Correspondencia allowlist ↔ triage ↔ docs de supply chain (P0R, ronda 10).

El guardián de consistencia NO cubría SECURITY_TRIAGE.md ni THREAT_MODEL.md, así que la
allowlist del gate (`--ignore-vuln` en scheduled-quality.yml) pudo derivar de la tabla de
triage y de los conteos declarados en la prosa (quedó "9 avisos" cuando ya eran 2). Este
check ata las TRES representaciones fail-closed:

  * el conjunto de IDs `--ignore-vuln` del workflow == IDs de las filas de la tabla de triage
    (cada ignore aparece como ID primario o alias en su fila);
  * el conteo "N avisos" del encabezado de SECURITY_TRIAGE.md == nº de filas == nº de ignores;
  * el conteo "N avisos aceptados" de THREAT_MODEL.md == ese mismo N.

Cualquier bump vía `make lock` que retire/añada un aviso debe tocar las tres a la vez o esto
rompe. Stdlib puro; corre en el job `consistency` de CI (PR y push).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "scheduled-quality.yml"
TRIAGE = ROOT / "docs" / "SECURITY_TRIAGE.md"
THREAT = ROOT / "docs" / "THREAT_MODEL.md"
_ADV = re.compile(r"\b(?:CVE-\d{4}-\d+|PYSEC-\d{4}-\d+|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})\b")


def _ignores(text: str) -> list[str]:
    # solo args con FORMA de advisory (excluye menciones en prosa como "--ignore-vuln aquí")
    return [m for m in re.findall(r"--ignore-vuln\s+(\S+)", text) if _ADV.fullmatch(m)]


def _triage_rows(text: str) -> list[list[str]]:
    """Filas de datos de la tabla del perfil model: líneas `| pkg X.Y | AVISO ... |` con
    al menos un advisory ID. (Excluye encabezado y separador.)"""
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
    wf = WORKFLOW.read_text()
    tr = TRIAGE.read_text()
    th = THREAT.read_text()

    ignores = _ignores(wf)
    rows = _triage_rows(tr)
    n_ign = len(ignores)

    # (1) sin duplicados en la allowlist del workflow
    if len(set(ignores)) != n_ign:
        probs.append(f"allowlist: --ignore-vuln duplicados en el workflow ({ignores})")

    # (2) cada ignore aparece como ID primario o alias en ALGUNA fila del triage
    triage_all_ids = {i for row in rows for i in row}
    for ig in ignores:
        if ig not in triage_all_ids:
            probs.append(f"allowlist: {ig} ignorado en el workflow pero SIN fila en SECURITY_TRIAGE.md")

    # (3) nº de ignores == nº de filas de triage
    if n_ign != len(rows):
        probs.append(f"allowlist: {n_ign} ignores != {len(rows)} filas de triage")

    # (4) conteos de prosa coherentes en AMBOS docs
    for name, cnt in (("SECURITY_TRIAGE.md", _header_count(tr)), ("THREAT_MODEL.md", _header_count(th))):
        if cnt is None:
            probs.append(f"{name}: no se halló el conteo 'N avisos' en la prosa")
        elif cnt != n_ign:
            probs.append(f"{name}: prosa dice {cnt} avisos, pero la allowlist tiene {n_ign}")

    if probs:
        print(f"✗ SUPPLY-CHAIN TRIAGE incoherente ({len(probs)}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ Supply-chain triage coherente: {n_ign} avisos (workflow == triage == docs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
