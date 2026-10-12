"""M74-F · #65 — la distribución predictiva que muestrean ARIMA/SARIMA es la de statsmodels, completa.

darts 0.44.1 (`ARIMA._predict`) simula con ``initial_state=self.model.states.predicted[-1, :]``: fija el
estado predicho y sólo deja correr los choques de los pasos siguientes. En un ARIMA de statsmodels la
medición no tiene ruido, así que el paso h=1 sale DETERMINISTA (ancho 0) y h=2, h=3 pierden un paso de
varianza. Eso dejó la cobertura del CRPS campeón de DFF en 0/600 (C5 fallida) y el CRPS de referencia de
ARIMA/SARIMA convertido en un error puntual.

Estas pruebas comparan las muestras con la varianza ANALÍTICA del mismo ajuste (``get_forecast``), que es
la referencia independiente; el pronóstico puntual (``num_samples=1``) tiene que quedar idéntico.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("darts")
pytest.importorskip("statsmodels")

from darts import TimeSeries
from darts.models import ARIMA as DartsARIMA

from vp_model import models
from vp_model.config import HYPERPARAMS

MUESTRAS = 4000


@pytest.fixture(scope="module")
def serie() -> TimeSeries:
    """Paseo aleatorio con deriva, mensual, 120 meses: el régimen de las fechas de prioridad."""
    rng = np.random.default_rng(1)
    valores = 9000.0 + np.cumsum(12.0 + rng.normal(0.0, 40.0, 120))
    return TimeSeries.from_series(pd.Series(valores, index=pd.date_range("2010-01-01", periods=120, freq="MS")))


def _ajustado(nombre: str, ts: TimeSeries):
    m = models.build_relaxed_sarima() if nombre == "sarima_relajada" else models.build_model(nombre)
    return m.fit(ts)


@pytest.mark.parametrize("nombre", ["arima", "sarima", "sarima_relajada"])
def test_las_muestras_reproducen_la_varianza_analitica_en_cada_horizonte(serie: TimeSeries, nombre: str) -> None:
    m = _ajustado(nombre, serie)
    analitica = np.asarray(m.model.get_forecast(steps=3).se_mean, dtype=float)
    muestras = m.predict(3, num_samples=MUESTRAS, random_state=11).all_values()[:, 0, :]
    dispersion = muestras.std(axis=1, ddof=1)
    assert dispersion[0] > 0, f"{nombre}: el paso h=1 sale determinista (ancho 0)"
    np.testing.assert_allclose(dispersion, analitica, rtol=0.08, err_msg=f"{nombre}: desviación muestral ≠ analítica")


@pytest.mark.parametrize("nombre", ["arima", "sarima"])
def test_el_walk_forward_a_un_paso_que_usan_los_productores_tiene_ancho(serie: TimeSeries, nombre: str) -> None:
    """El camino EXACTO de `run_champion_crps._member_quantiles` y `walkforward.crps_holdout`."""
    m = models.build_model(nombre)
    muestras = m.historical_forecasts(
        serie,
        start=serie.time_index[-4],
        forecast_horizon=1,
        stride=1,
        retrain=True,
        last_points_only=True,
        verbose=False,
        num_samples=200,
    )
    ancho = muestras.quantile(0.975).values()[:, 0] - muestras.quantile(0.025).values()[:, 0]
    assert len(ancho) == 4 and (ancho > 30.0).all(), f"{nombre}: banda 95 % de ancho {ancho}"


@pytest.mark.parametrize("nombre", ["arima", "sarima"])
def test_control_el_pronostico_puntual_no_cambia(serie: TimeSeries, nombre: str) -> None:
    """`num_samples=1` (MASE, campañas, web) es el de darts, byte a byte."""
    m = models.build_model(nombre)
    m.fit(serie)
    nuestro = m.predict(6).values()
    de_darts = DartsARIMA(**HYPERPARAMS[nombre]).fit(serie).predict(6).values()
    np.testing.assert_array_equal(nuestro, de_darts)


def test_control_la_semilla_reproduce_las_muestras(serie: TimeSeries) -> None:
    m = _ajustado("sarima", serie)
    a = m.predict(2, num_samples=50, random_state=7).all_values()
    b = m.predict(2, num_samples=50, random_state=7).all_values()
    c = m.predict(2, num_samples=50, random_state=8).all_values()
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(a, c)


def test_control_las_muestras_se_centran_en_el_puntual(serie: TimeSeries) -> None:
    m = _ajustado("arima", serie)
    puntual = m.predict(3).values()[:, 0]
    media = m.predict(3, num_samples=MUESTRAS, random_state=3).all_values()[:, 0, :].mean(axis=1)
    analitica = np.asarray(m.model.get_forecast(steps=3).se_mean, dtype=float)
    assert (np.abs(media - puntual) < 4 * analitica / np.sqrt(MUESTRAS)).all()


def test_muestrear_sobre_otra_serie_no_altera_el_ajuste(serie: TimeSeries) -> None:
    """`predict(series=...)` re-aplica el ajuste a otra historia; después, el modelo es el de antes."""
    m = _ajustado("arima", serie)
    antes = m.predict(3).values()
    otra = serie[:100]
    assert m.predict(2, series=otra, num_samples=50, random_state=1).n_samples == 50
    np.testing.assert_array_equal(m.predict(3).values(), antes)
    assert m.model.nobs == len(serie)
