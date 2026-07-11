"""Gate del catálogo de modelos (D2, plan auditoría 2026-07-11). Stdlib puro.

Hace cumplir el catálogo gobernado ``docs/model_catalog.json``:
1. Todo modelo de las tablas de comparación vigentes está clasificado (y sin fantasmas).
2. Las baselines obligatorias (naive1, drift, naïve estacional) están en TODA tabla de
   comparación — ninguna campaña puede omitir el piso.
3. El manifiesto campeón solo usa modelos ``active`` — nada research/retired se
   despliega en silencio (reclasificar = PR explícito al catálogo).
4. Ningún ``retired`` aparece en recetas del manifiesto ni del verdict.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "docs" / "model_catalog.json"
COMPARISONS = [ROOT / "reports" / "eval" / f"model_comparison_{t}21.csv" for t in ("FAD", "DFF")]
MANIFEST = ROOT / "reports" / "governance" / "champion_manifest.json"


def _models_in(csv_path: Path) -> set[str]:
    with csv_path.open() as fh:
        return {row["model"] for row in csv.DictReader(fh)}


def main() -> int:
    cat = {k: v for k, v in json.loads(CATALOG.read_text()).items() if not k.startswith("_")}
    baselines = {k for k, v in cat.items() if v.get("baseline_obligatoria")}
    active = {k for k, v in cat.items() if v["clase"] == "active"}
    retired = {k for k, v in cat.items() if v["clase"] == "retired"}
    problems: list[str] = []

    seen: set[str] = set()
    for path in COMPARISONS:
        if not path.exists():
            problems.append(f"{path.name}: tabla de comparación ausente")
            continue
        models = _models_in(path)
        seen |= models
        for m in sorted(models - set(cat)):
            problems.append(f"{path.name}: modelo SIN CLASIFICAR en el catálogo: {m}")
        for b in sorted(baselines - models):
            problems.append(f"{path.name}: baseline obligatoria AUSENTE: {b}")
    for ghost in sorted(set(cat) - seen):
        problems.append(f"catálogo: {ghost} no aparece en ninguna tabla de comparación vigente (¿fantasma?)")

    manifest = json.loads(MANIFEST.read_text())
    for table, recipe in manifest.items():
        for m in recipe.get("models", []):
            if m in retired:
                problems.append(f"manifiesto[{table}]: usa un modelo RETIRADO: {m}")
            elif m not in active:
                problems.append(
                    f"manifiesto[{table}]: {m} no es clase 'active' — reclasificar en docs/model_catalog.json ANTES de desplegar"
                )

    if problems:
        print(f"✗ CATÁLOGO DE MODELOS ROTO ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    by_class: dict[str, int] = {}
    for v in cat.values():
        by_class[v["clase"]] = by_class.get(v["clase"], 0) + 1
    print(
        f"✓ Catálogo OK — {len(cat)} modelos {dict(sorted(by_class.items()))} · baselines {sorted(baselines)} presentes · manifiesto ⊆ active"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
