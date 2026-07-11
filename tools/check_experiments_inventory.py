"""Gate del inventario de entrypoints (I2, plan auditoría 2026-07-11).

Todo entrypoint de ``experiments/`` (``*.py``/``*.sh``) debe estar clasificado en
``docs/experiments_inventory.json`` (producto · deliverable · investigacion ·
diagnostico · archivo) con su consumidor. Un script nuevo sin clasificar rompe CI;
una entrada fantasma (script borrado) también — el inventario no acumula fósiles.
Stdlib puro.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "docs" / "experiments_inventory.json"
CLASSES = {"producto", "deliverable", "investigacion", "diagnostico", "archivo"}


def main() -> int:
    inv = json.loads(INVENTORY.read_text())
    entries = {k: v for k, v in inv.items() if not k.startswith("_")}
    on_disk = {p.name for p in (ROOT / "experiments").glob("*.py")} | {
        p.name for p in (ROOT / "experiments").glob("*.sh")
    }
    problems: list[str] = []
    for missing in sorted(on_disk - set(entries)):
        problems.append(f"SIN CLASIFICAR: experiments/{missing} — añádelo a docs/experiments_inventory.json")
    for ghost in sorted(set(entries) - on_disk):
        problems.append(f"FANTASMA: {ghost} está en el inventario pero no en experiments/")
    for name, e in sorted(entries.items()):
        if e.get("clase") not in CLASSES:
            problems.append(f"{name}: clase inválida {e.get('clase')!r} (validas: {sorted(CLASSES)})")
        if not e.get("consumidor"):
            problems.append(f"{name}: sin consumidor declarado")
    if problems:
        print(f"✗ INVENTARIO DE EXPERIMENTOS ROTO ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    by_class: dict[str, int] = {}
    for e in entries.values():
        by_class[e["clase"]] = by_class.get(e["clase"], 0) + 1
    print(f"✓ {len(entries)} entrypoints clasificados: {dict(sorted(by_class.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
