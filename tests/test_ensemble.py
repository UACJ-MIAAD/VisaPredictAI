"""Tests del ensamble curado (vp_model/ensemble.py) — la combinación mediana-de-fuertes
que produce el resultado reportado ~0.1115 en FAD. Mockea datos y rutas para aislar la
lógica de combinación + escala naïve por fecha (sin depender de los CSV de resultados reales).
"""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("darts")  # capa de modelado: se salta sin el extra `model`

from vp_model import dataset, ensemble


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
    # ★ R2 · el marco entra por la costura: el lector recalcula el conjunto esperado contra el
    # panel real, y este fixture sintético no puede —ni debe— acreditarse. Aquí se prueba la
    # MEDIANA, no la acreditación, que tiene su propia batería.
    ruta = tmp_path / "eval" / "holdout_forecasts_FAD.csv"
    _write_forecasts(ruta, {"theta": 10.0, "ets": 12.0, "sarima": 20.0})
    marco = pd.read_csv(ruta, parse_dates=["date"])
    # serie con escala naïve conocida: incrementos de 1 -> seasonal_naive_mae sobre tramo previo.
    # AM4d: el scorer canónico (mase_by_series) aplica la máscara F-only por fecha, así que
    # la serie cruda debe CONTENER las fechas del hold-out sintético (2024-01/02).
    s = pd.Series(np.arange(124.0), index=pd.date_range("2014-01-01", periods=124, freq="MS"))
    monkeypatch.setattr(dataset, "load_series", lambda *a, **k: s)

    strat = ensemble.curated_combination("FAD", fc=marco)
    # mediana(10,12,20)=12; |11-12|=1 -> MAE=1.0; MASE>0 con escala por fecha (no NaN, no posicional)
    assert strat.hold_mae == pytest.approx(1.0)
    assert strat.hold_mase > 0 and np.isfinite(strat.hold_mase)
    assert "median" in strat.name


def test_combinations_aborta_sin_artefacto_acreditado(tmp_path, monkeypatch):
    """★ M74-E-R1 · el contrato CAMBIÓ, y a propósito.

    Antes devolvía `[]` «sin explotar». El problema es que `[]` se leía igual que «las
    combinaciones no aportan nada», y el runbook lo daba por un resultado. Un insumo ausente o sin
    acreditar no es un resultado: aborta nombrando la causa.
    """
    from vp_model import artifact_receipt as ar

    monkeypatch.setattr(ensemble, "REPORTS", tmp_path)
    monkeypatch.delenv("CAMPAIGN_ID", raising=False)
    with pytest.raises(ar.ReceiptError):
        ensemble.combinations("FAD")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
