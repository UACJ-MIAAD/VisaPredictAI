"""Trinquete de deuda técnica (E3, plan auditoría 2026-07-11).

Cuenta los marcadores de deuda del código de producto y los compara contra la
baseline versionada ``docs/debt_baseline.json``: **ningún conteo puede SUBIR**
(el trinquete solo aprieta). Si un conteo BAJA, el script lo celebra y pide
actualizar la baseline en el mismo PR (decisión visible, nunca automática).

    python tools/check_debt.py            # gate (CI + make check)
    python tools/check_debt.py --update   # reescribe la baseline con los conteos actuales

Métricas: ``except Exception`` totales y SIN razón declarada (la política del repo:
todo except amplio lleva ``noqa: BLE001`` + comentario de continuidad), ``type: ignore``,
``noqa`` y marcadores TODO/FIXME/HACK/XXX. Solo capas de producto (vp_data, pipeline,
vp_model, tools, experiments). Stdlib puro.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "debt_baseline.json"
LAYERS = ("vp_data", "pipeline", "vp_model", "tools", "experiments")
JUSTIFIED = re.compile(r"noqa|—|--")
TODOISH = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")


def counts() -> dict[str, int]:
    c = {"except_exception": 0, "except_exception_unjustified": 0, "type_ignore": 0, "noqa": 0, "todo_class": 0}
    for layer in LAYERS:
        for f in (ROOT / layer).glob("*.py"):
            for line in f.read_text(errors="ignore").splitlines():
                if "except Exception" in line:
                    c["except_exception"] += 1
                    if not JUSTIFIED.search(line):
                        c["except_exception_unjustified"] += 1
                if "type: ignore" in line:
                    c["type_ignore"] += 1
                if "# noqa" in line:
                    c["noqa"] += 1
                if TODOISH.search(line):
                    c["todo_class"] += 1
    return c


def main() -> int:
    now = counts()
    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps(now, indent=2) + "\n")
        print(f"baseline actualizada → {BASELINE}: {now}")
        return 0
    base = json.loads(BASELINE.read_text())
    worse = {k: (base[k], v) for k, v in now.items() if v > base.get(k, 0)}
    better = {k: (base[k], v) for k, v in now.items() if v < base.get(k, 10**9)}
    if worse:
        print(f"✗ DEUDA CRECIÓ (el trinquete solo aprieta): {worse}")
        print("  añade la justificación/limpia el marcador, o actualiza la baseline en el PR con razón explícita")
        return 1
    msg = f"✓ Deuda dentro de baseline: {now}"
    if better:
        msg += f" · MEJORÓ {list(better)} — corre `python tools/check_debt.py --update` y commitea"
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
