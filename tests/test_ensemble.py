"""Tests del ensamble curado (vp_model/ensemble.py) — la combinación mediana-de-fuertes
que produce el resultado reportado ~0.1115 en FAD. Mockea datos y rutas para aislar la
lógica de combinación + escala naïve por fecha (sin depender de los CSV de resultados reales).
"""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("darts")  # capa de modelado: se salta sin el extra `model`

from vp_model import dataset, ensemble  # noqa: E402


def _write_forecasts(path, model_to_fc, actual=11.0):
    """CSV de hold-out sintético: 1 serie, 2 fechas, modelos con pronóstico constante."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.to_datetime(["2024-01-01", "2024-02-01"])
    rows = [
        {"country": "mexico", "category": "F1", "date": d, "model": m, "actual": actual, "forecast": fc}
        for d in dates
        for m, fc in model_to_fc.items()
    ]
    pd.DataFrame(rows).to_csv(path, index=False)


def test_curated_combination_median(tmp_path, monkeypatch):
    """La combinación toma la MEDIANA por fecha y escala por el naïve estacional previo."""
    monkeypatch.setattr(ensemble, "REPORTS", tmp_path)
    _write_forecasts(tmp_path / "eval" / "holdout_forecasts_FAD.csv", {"theta": 10.0, "ets": 12.0, "sarima": 20.0})
    # serie con escala naïve conocida: incrementos de 1 -> seasonal_naive_mae sobre tramo previo
    s = pd.Series(np.arange(120.0), index=pd.date_range("2014-01-01", periods=120, freq="MS"))
    monkeypatch.setattr(dataset, "load_series", lambda *a, **k: s)

    strat = ensemble.curated_combination("FAD")
    # mediana(10,12,20)=12; |11-12|=1 -> MAE=1.0; MASE>0 con escala por fecha (no NaN, no posicional)
    assert strat.hold_mae == pytest.approx(1.0)
    assert strat.hold_mase > 0 and np.isfinite(strat.hold_mase)
    assert "median" in strat.name


def test_combinations_returns_empty_without_csv(tmp_path, monkeypatch):
    """Sin CSV persistido, combinations() devuelve [] (no explota)."""
    monkeypatch.setattr(ensemble, "REPORTS", tmp_path)
    assert ensemble.combinations("FAD") == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
