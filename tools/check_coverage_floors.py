"""Pisos de cobertura POR MÓDULO crítico (G3, plan auditoría 2026-07-11).

El promedio no puede esconder módulos críticos: este gate lee el JSON de coverage del
job de modelado y aplica los pisos por archivo de ``docs/coverage_floors.json``
(ledger/promoción/bandas/métricas/…). Los pisos SOLO SUBEN editando ese archivo en un
PR (trinquete explícito, como debt_baseline).

    coverage json -o /tmp/cov.json && python tools/check_coverage_floors.py /tmp/cov.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOORS = ROOT / "docs" / "coverage_floors.json"


def main() -> int:
    cov_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/cov.json")
    cov = json.loads(cov_path.read_text())
    floors: dict[str, float] = json.loads(FLOORS.read_text())
    by_name = {f.split("/")[-1]: v["summary"]["percent_covered"] for f, v in cov["files"].items()}
    problems, report = [], []
    for name, floor in sorted(floors.items()):
        got = by_name.get(name)
        if got is None:
            problems.append(f"{name}: sin datos de cobertura (¿salió del cov target?)")
            continue
        mark = "✓" if got >= floor else "✗"
        report.append(f"  {mark} {name}: {got:.1f}% (piso {floor}%)")
        if got < floor:
            problems.append(f"{name}: {got:.1f}% < piso {floor}%")
    print("\n".join(report))
    if problems:
        print(f"✗ PISOS DE COBERTURA ROTOS ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ {len(floors)} módulos críticos sobre su piso")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
