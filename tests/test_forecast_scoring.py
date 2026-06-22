"""Contrato de la evaluación prospectiva (experiments/score_forecasts).

Prueba la lógica PURA de scoring (`_score_rows`) con datos sintéticos — sin BD ni
modelos — para garantizar que el conteo de pendientes, la cobertura 80/95 % y el
error escalado (MASE) se calculan bien. Es la base de toda la medición real, así que
no puede quedar sin test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # para que score_forecasts resuelva `import tracking` (módulo raíz)
_spec = importlib.util.spec_from_file_location("score_forecasts", ROOT / "experiments" / "score_forecasts.py")
assert _spec is not None and _spec.loader is not None  # narrow para mypy + falla claro si no carga
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)


def test_demo_selfcheck() -> None:
    sf.demo()  # asserts internos: pendiente, cobertura 80/95, MASE


def test_pending_when_target_not_realized() -> None:
    fc = pd.DataFrame(
        [
            {
                "origin": "2024-01",
                "h": 1,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2099-01-01",
                "days": 1000,
                "lo80": 950,
                "hi80": 1050,
                "lo95": 900,
                "hi95": 1100,
            }
        ]
    )
    scored, pending = sf._score_rows(fc, {}, lambda *_: 100.0)
    assert scored == [] and pending == 1


def test_coverage_and_scaled_error() -> None:
    fc = pd.DataFrame(
        [
            {
                "origin": "2024-01",
                "h": 1,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2024-02-01",
                "days": 1000,
                "lo80": 990,
                "hi80": 1010,
                "lo95": 900,
                "hi95": 1100,
            }
        ]
    )
    # real = 1060 → fuera de [990,1010] (in80=0) pero dentro de [900,1100] (in95=1); |error|=60, escala=120 → MASE 0.5
    scored, pending = sf._score_rows(fc, {("mexico", "F1", "FAD", "2024-02-01"): 1060.0}, lambda *_: 120.0)
    assert pending == 0 and len(scored) == 1
    s = scored[0]
    assert s["abs_err"] == 60 and s["in80"] == 0 and s["in95"] == 1
    assert abs(s["scaled_err"] - 0.5) < 1e-9


if __name__ == "__main__":
    test_demo_selfcheck()
    test_pending_when_target_not_realized()
    test_coverage_and_scaled_error()
    print("OK — test_forecast_scoring: 3/3")
