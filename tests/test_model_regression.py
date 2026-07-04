"""Golden-master de las métricas del modelado: impide que un refactor degrade los
forecasts EN SILENCIO.

El walk-forward leakage-free es la pieza más cara de auditar a ojo: un cambio sutil en
``vp_model`` (escala naïve, ventana, indexación de folds, fuga de futuro) puede mover el
MASE sin romper ningún test de tipo. Este test fija el MASE de hold-out de una rejilla
pequeña, barata y DETERMINISTA (naïve/ETS/Theta/ARIMA/SARIMA sobre 2 series, FAD+DFF) y
falla si cualquiera se desvía del baseline committeado más allá de una tolerancia que
absorbe ruido numérico de BLAS entre plataformas (macOS↔linux) pero no una regresión real.

Regenerar el baseline a propósito (tras un cambio de metodología legítimo):
    ante/bin/python tests/test_model_regression.py --update
y commitear ``tests/model_regression_baseline.json`` con una nota del porqué.

Historial de regeneraciones intencionales:
  * 2026-07-04 (plan MODELOS, épica AJ4): ets/theta pasaron a AutoETS/AutoTheta
    (selección AICc / select_best_model sobre la ventana inicial) — sus celdas
    cambian por diseño. De paso el baseline absorbe el micro-drift FAD (<0.6%,
    dentro de tolerancia) que dejó la resurrección I1 de los 5 meses pre-2015
    (solo tocó series FAD; el baseline previo era pre-resurrección).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("darts")  # la rejilla corre sobre la capa de modelado (extra `model`)

from vp_model import config, walkforward  # noqa: E402  (tras el importorskip a propósito)

BASELINE_PATH = Path(__file__).with_name("model_regression_baseline.json")

# Rejilla determinista y barata. Cubre: el denominador del MASE (naïve), los campeones
# parsimoniosos (ETS/Theta), el camino clásico pesado (ARIMA/SARIMA), y ambas tablas.
SERIES = [("mexico", "F3", "FAD"), ("mexico", "F1", "DFF")]
MODELS = ["naive", "ets", "theta", "arima", "sarima"]

# Una regresión real mueve el MASE mucho (o lo hace explotar); el ruido de BLAS entre
# plataformas es ~1e-3. Tolerancia = el mayor de un piso absoluto y un relativo.
ABS_TOL = 0.01
REL_TOL = 0.05


def _key(country: str, category: str, table: str, model: str) -> str:
    return f"{country}/{category}/{table}/{model}"


def compute() -> dict[str, float]:
    """MASE de hold-out de toda la rejilla, con semilla fija."""
    config.seed_everything()
    out: dict[str, float] = {}
    for country, category, table in SERIES:
        for model in MODELS:
            r = walkforward.backtest(model, country, category, table)
            out[_key(country, category, table, model)] = round(float(r.holdout["mase"]), 4)
    return out


def test_model_regression() -> None:
    assert BASELINE_PATH.exists(), (
        f"falta {BASELINE_PATH.name}; genéralo con `ante/bin/python tests/test_model_regression.py --update`"
    )
    baseline = json.loads(BASELINE_PATH.read_text())
    got = compute()

    assert set(got) == set(baseline), (
        f"la rejilla cambió respecto al baseline: "
        f"nuevas={sorted(set(got) - set(baseline))} faltan={sorted(set(baseline) - set(got))}"
    )
    drifted = []
    for k, exp in baseline.items():
        tol = max(ABS_TOL, REL_TOL * abs(exp))
        if abs(got[k] - exp) > tol:
            drifted.append(f"{k}: baseline={exp} ahora={got[k]} (tol={tol:.4f})")
    assert not drifted, "MASE de hold-out regresó:\n  " + "\n  ".join(drifted)


if __name__ == "__main__":
    import sys

    facts = compute()
    if "--update" in sys.argv:
        BASELINE_PATH.write_text(json.dumps(facts, indent=2) + "\n")
        print(f"baseline escrito en {BASELINE_PATH} ({len(facts)} celdas)")
    else:
        print(json.dumps(facts, indent=2))
