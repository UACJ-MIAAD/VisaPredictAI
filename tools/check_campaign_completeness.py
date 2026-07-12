#!/usr/bin/env python
"""Gate de COMPLETITUD + FRESCURA de una campaña de rederivación (auditoría 12-jul-2026).

Impide que la significancia / champion-challenger / key_facts se computen sobre una
campaña INCOMPLETA (falta un bloque/semilla/HPO) o sobre artefactos VIEJOS reutilizados
de un corte anterior. Sin este gate, un runbook que fallara a medias podía "terminar en
verde" con inputs stale.

Lee ``reports/campaign/campaign_manifest.json`` (sellado por run_rederivation.sh con
``campaign_id`` + ``sha`` + ``started_at``) y verifica que CADA artefacto canónico:
  1. EXISTE,
  2. es FRESCO (mtime >= started_at de la campaña — no reutilizado de un corte previo),
  3. cumple el CONTEO esperado (4 pools, 4 comparaciones, 6 HPO, 2 finalists/holdout, …),
  4. cada pool trae ≥ un piso de modelos (no un CSV vacío que pase por "existe").

Uso:
  python -m tools.check_campaign_completeness            # gate estricto (exit 1 si falla)
  python -m tools.check_campaign_completeness --preflight  # solo reporta, no exige frescura

Es un GATE del runbook (run_req antes de significancia). Salida 0 = campaña completa+fresca.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "reports" / "campaign" / "campaign_manifest.json"

# Artefactos canónicos: (glob relativo a ROOT, conteo exacto esperado, piso de líneas útiles).
# El piso de líneas evita que un CSV vacío (solo header) pase por "existe".
EXPECTED: list[tuple[str, int, int]] = [
    ("reports/campaign/campaign_pool_FAD_family.csv", 1, 5),
    ("reports/campaign/campaign_pool_FAD_employment.csv", 1, 5),
    ("reports/campaign/campaign_pool_DFF_family.csv", 1, 5),
    ("reports/campaign/campaign_pool_DFF_employment.csv", 1, 5),
    ("reports/eval/model_comparison_FAD21.csv", 1, 5),
    ("reports/eval/model_comparison_EB_FAD21.csv", 1, 5),
    ("reports/eval/model_comparison_DFF21.csv", 1, 5),
    ("reports/eval/model_comparison_EB_DFF21.csv", 1, 5),
    ("reports/campaign/hpo_deep_best_FAD_Auto*.json", 3, 0),
    ("reports/campaign/hpo_deep_best_DFF_Auto*.json", 3, 0),
    ("reports/eval/finalist_forecasts_FAD.csv", 1, 2),
    ("reports/eval/finalist_forecasts_DFF.csv", 1, 2),
    ("reports/eval/holdout_forecasts_FAD.csv", 1, 2),
    ("reports/eval/holdout_forecasts_DFF.csv", 1, 2),
    ("reports/eval/significance_summary.json", 1, 0),
    ("reports/governance/champion_challenger.json", 1, 0),
]


def _started_at() -> dt.datetime | None:
    if not MANIFEST.exists():
        return None
    try:
        raw = json.loads(MANIFEST.read_text())["started_at"]
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except json.JSONDecodeError, KeyError, ValueError:
        return None


def _useful_lines(path: Path) -> int:
    """Líneas no vacías menos la de header (aprox. de 'filas útiles' en un CSV)."""
    try:
        n = sum(1 for line in path.read_text().splitlines() if line.strip())
        return max(0, n - 1)
    except OSError:
        return 0


def check(preflight: bool = False) -> list[str]:
    problems: list[str] = []
    started = _started_at()
    if started is None:
        problems.append(
            "reports/campaign/campaign_manifest.json ausente o inválido: sin él no se puede "
            "verificar frescura. Lanza la campaña con run_rederivation.sh (sella el manifiesto)."
        )
        if not preflight:
            return problems  # sin manifiesto no hay frescura que comprobar

    for pattern, want, floor in EXPECTED:
        matches = sorted(ROOT.glob(pattern))
        if len(matches) != want:
            problems.append(f"CONTEO {pattern}: esperados {want}, hallados {len(matches)}")
            continue
        for m in matches:
            if floor and _useful_lines(m) < floor:
                problems.append(f"VACÍO {m.relative_to(ROOT)}: <{floor} filas útiles (¿CSV solo-header?)")
            if not preflight and started is not None:
                mtime = dt.datetime.fromtimestamp(m.stat().st_mtime, tz=dt.UTC)
                if mtime < started:
                    problems.append(
                        f"STALE {m.relative_to(ROOT)}: mtime {mtime:%F %T} < inicio de campaña "
                        f"{started:%F %T} — artefacto reutilizado de un corte anterior"
                    )
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preflight", action="store_true", help="solo reporta; no exige frescura")
    ns = ap.parse_args(argv[1:])
    problems = check(preflight=ns.preflight)
    if problems:
        print(f"✗ Campaña INCOMPLETA/STALE: {len(problems)} problema(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("✓ Campaña completa y fresca: todos los artefactos canónicos presentes, no-vacíos y posteriores al inicio.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
