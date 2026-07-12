"""Validación walk-forward sin fuga temporal (US-E1, US-F1, US-F2).

Replica el protocolo del Anteproyecto (§4.4): ventana inicial expansible de 60
meses (FAD) / 36 meses (DFF), paso de 1 mes, y los últimos 24 meses reservados como
hold-out independiente que NUNCA participa en la selección de modelo. El pronóstico
a 1 paso en cada origen usa solo el pasado (darts ``historical_forecasts`` con
ventana expansible), de modo que la comparación entre modelos está libre de leakage.

Decisión documentada (ponytail): los modelos estadísticos se reentrenan en cada
paso (barato, ventana expansible verdadera); las redes se reentrenan cada 12 meses
(anual) porque reentrenar una NN en cada uno de ~200 orígenes es inviable en CPU. Es
el compromiso estándar coste/validez para forecasting con NN.
"""

from __future__ import annotations

import warnings as _warnings
from collections import Counter
from dataclasses import dataclass, field
from typing import cast

from darts import TimeSeries

from vp_model import dataset, intervals, metrics, models
from vp_model.config import (
    HOLDOUT,
    LIKELIHOOD_MODELS,
    MIN_BACKTEST_BUFFER,
    MIN_TRAIN,
    NN_RETRAIN,
    NUM_SAMPLES_POINT,
    PROBABILISTIC,
    RETRAIN_EACH_STEP,
    get_logger,
)
from vp_model.feature_builder import FeatureBuilder

log = get_logger(__name__)


@dataclass(frozen=True)
class BacktestResult:
    model: str
    country: str
    category: str
    table: str
    selection: dict[str, float]  # métricas en la región de selección (pre-holdout)
    holdout: dict[str, float]  # métricas en los 24 meses reservados
    # E5: warnings de ajuste (p. ej. ConvergenceWarning de statsmodels en SARIMA)
    # CAPTURADOS durante el walk-forward de esta serie y contados por mensaje único
    # ("Categoria: mensaje" -> n folds que lo emitieron). Registrados aquí, no
    # silenciados globalmente: una serie que no converge deja rastro auditable.
    warnings: dict[str, int] = field(default_factory=dict)


def _median_point(fc: TimeSeries) -> TimeSeries:
    """Collapse a stochastic forecast to its per-step median (AJ2); no-op if deterministic."""
    if fc.n_samples <= 1:
        return fc
    return fc.median(axis=2)


def _block(category: str) -> str:
    """Tuning-group block from the category code (AK7): EB* -> employment."""
    return "employment" if category.upper().startswith("EB") else "family"


def run_forecasts(
    model_name: str, country: str, category: str, table: str, model: object | None = None
) -> tuple[TimeSeries, TimeSeries]:
    """Pronósticos walk-forward a 1 paso (toda la serie desde min_train) + la serie real.

    Devuelve (ts, forecasts) leakage-free: cada origen solo ve el pasado. Lo usan
    ``backtest`` (para métricas) y la persistencia de pronósticos (para ensambles).
    El FE (huecos, covariables, escalado) lo compone ``FeatureBuilder`` según la
    política por modelo de config — mismo comportamiento, linaje explícito (AD1).
    US-F1: la rejilla es CAUSAL (LOCF) — una sola serie transformada es válida para
    todos los orígenes — y las máscaras MNAR llegan a los GBM desde la serie F cruda.
    """
    fe = FeatureBuilder(model_name)
    raw = dataset.load_series(country, category, table)
    ts = fe.to_timeseries(raw)
    min_train = MIN_TRAIN[table]
    if len(ts) < min_train + HOLDOUT + MIN_BACKTEST_BUFFER:
        raise ValueError(f"serie demasiado corta ({len(ts)}) para min_train={min_train}+holdout={HOLDOUT}")

    # AP1: injected models (tuner templates, auto-arima) are typed `object` by their
    # producers; the cast documents that they must satisfy the Forecaster protocol.
    fc_model: models.Forecaster = (
        models.build_model(model_name, table=table, block=_block(category))
        if model is None
        else cast("models.Forecaster", model)
    )
    retrain: bool | int = True if model_name in RETRAIN_EACH_STEP else NN_RETRAIN
    # historical_forecasts recibe future_covariates UNA vez (lo usa en fit y predict).
    cov = fe.covariates(ts, raw)
    extra: dict[str, object] = {"future_covariates": cov} if cov is not None else {}
    if model_name in LIKELIHOOD_MODELS:
        # AJ2: with the darts default num_samples=1, a SINGLE stochastic draw was
        # serving as the point forecast; sample the predictive distribution instead
        # and use its median as the point (collapsed in _median_point below).
        extra["num_samples"] = NUM_SAMPLES_POINT

    # Escalado leakage-free para redes: Scaler ajustado solo en la ventana inicial.
    scaler = fe.fit_scaler(ts, min_train)
    ts_model = scaler.transform(ts) if scaler is not None else ts

    forecasts = fc_model.historical_forecasts(
        ts_model,
        start=min_train,
        forecast_horizon=1,
        stride=1,
        retrain=retrain,
        last_points_only=True,
        verbose=False,
        **extra,
    )
    if scaler is not None:
        forecasts = scaler.inverse_transform(forecasts)
    return ts, _median_point(forecasts)


def _summarize_warnings(caught: list[_warnings.WarningMessage]) -> dict[str, int]:
    """E5: colapsa los warnings capturados a conteos por mensaje único.

    Clave = "Categoria: mensaje" (p. ej. "ConvergenceWarning: Maximum Likelihood
    optimization failed to converge…"); valor = número de folds/ajustes que lo
    emitieron durante el walk-forward de la serie.
    """
    return dict(Counter(f"{w.category.__name__}: {w.message}" for w in caught))


def backtest(model_name: str, country: str, category: str, table: str, model: object | None = None) -> BacktestResult:
    """Corre el walk-forward de un modelo sobre una serie y devuelve sus métricas.

    Las métricas de *selección* se calculan sobre los pronósticos previos al hold-out
    (lo que se usa para comparar modelos); las de *hold-out* sobre los últimos 24
    meses (evaluación final del modelo elegido, US-F2). ``model`` permite inyectar un
    modelo ya configurado (el tuner pasa una plantilla con hiperparámetros de prueba);
    si es ``None`` se usa ``build_model(model_name)`` con los defaults de config.

    E5: los warnings de ajuste (convergencia de statsmodels en ARIMA/SARIMA/ETS, etc.)
    se CAPTURAN alrededor del walk-forward y quedan registrados por serie en
    ``BacktestResult.warnings`` (conteo por mensaje único) — no se silencian con un
    filtro global ni se pierden en el scroll de una campaña de horas.
    """
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        ts, forecasts = run_forecasts(model_name, country, category, table, model)
    fit_warnings = _summarize_warnings(caught)
    if fit_warnings:
        log.info(
            "backtest %s %s/%s/%s: %d warning(s) de ajuste capturados (%d únicos) — ver BacktestResult.warnings",
            model_name,
            country,
            category,
            table,
            sum(fit_warnings.values()),
            len(fit_warnings),
        )
    # B1: las métricas se evalúan SOLO sobre observaciones F reales. `ts` viene rellenado
    # por `to_timeseries` (continuidad para entrenar); puntuar los meses interpolados
    # deprimía el error y hacía la vía local incomparable con la global (que ya enmascara
    # en `eval_neuralforecast.eval_global_deep`). La escala del MASE usa la MISMA fuente
    # única que la vía global: naïve estacional sobre la serie F cruda pre-hold-out.
    raw = dataset.load_series(country, category, table).astype("float64")
    fdates = raw.index
    split = ts.time_index[-HOLDOUT]
    scale = metrics.naive_scale_before(raw, split)
    # AI2: naive-1 (random walk) scale in parallel — contextualizes the m=12 MASE
    # without touching the canonical figures (extra `mase1` key/column).
    scale1 = metrics.naive_scale_before(raw, split, m=1)
    sel_fc, hold_fc = forecasts.split_before(split)
    insample = ts.drop_after(split)

    # Dimensión PROBABILÍSTICA (US-L1): intervalo de predicción CONFORME universal —
    # calibrado con los residuales F de la región de selección, válido para CUALQUIER
    # modelo a partir de sus pronósticos puntuales, sin reentrenar. Mide la calidad del
    # intervalo al 95% en el hold-out con MSIS (métrica oficial del M5), interval score
    # y cobertura empírica. Así la comparación deja de ser solo puntual.
    hold_actual = ts.slice_intersect(hold_fc)
    sel_actual = ts.slice_intersect(sel_fc)
    prob: dict[str, float] = {}
    try:
        iv = intervals.conformal(hold_fc, sel_actual, sel_fc, calib_dates=fdates)
        prob = {
            "msis": metrics.msis(hold_actual, iv.lower, iv.upper, insample, dates=fdates, scale=scale),
            "interval_score": metrics.interval_score(hold_actual, iv.lower, iv.upper, dates=fdates),
            "coverage": metrics.pi_coverage(hold_actual, iv.lower, iv.upper, dates=fdates),
        }
    except (ValueError, IndexError) as e:
        # AP5: the NaN degradation was SILENT — without a trace one cannot tell
        # "series with too few F residuals to calibrate" from a conformal bug.
        log.warning(
            "conformal PI failed for %s/%s/%s/%s (%s: %s) — probabilistic metrics set to NaN",
            model_name,
            country,
            category,
            table,
            type(e).__name__,
            e,
        )
        prob = {"msis": float("nan"), "interval_score": float("nan"), "coverage": float("nan")}
    holdout = {**metrics.compute(hold_actual, hold_fc, insample, dates=fdates, scale=scale, scale1=scale1), **prob}
    return BacktestResult(
        model=model_name,
        country=country,
        category=category,
        table=table,
        selection=metrics.compute(sel_actual, sel_fc, insample, dates=fdates, scale=scale, scale1=scale1),
        holdout=holdout,
        warnings=fit_warnings,
    )


def crps_holdout(model_name: str, country: str, category: str, table: str, num_samples: int = 200) -> float:
    """CRPS sobre el hold-out para modelos DISTRIBUCIONALES (muestreo nativo).

    A diferencia del PI conforme (intervalo), el CRPS evalúa la distribución predictiva
    COMPLETA. Solo aplica a modelos probabilísticos (ARIMA/SARIMA/DeepAR): se corre un
    walk-forward de 1 paso sobre el hold-out con ``num_samples`` y se promedia el CRPS.
    """
    if model_name not in PROBABILISTIC:
        # un modelo determinista devolvería un "CRPS" = MAE en silencio (sin muestreo real).
        raise ValueError(
            f"crps_holdout solo aplica a modelos distribucionales {sorted(PROBABILISTIC)}, no '{model_name}'"
        )
    fe = FeatureBuilder(model_name)
    raw = dataset.load_series(country, category, table)
    ts = fe.to_timeseries(raw)
    split = ts.time_index[-HOLDOUT]
    model = models.build_model(model_name, table=table, block=_block(category))
    retrain: bool | int = True if model_name in RETRAIN_EACH_STEP else NN_RETRAIN
    scaler = fe.fit_scaler(ts, len(ts) - HOLDOUT)
    ts_model = scaler.transform(ts) if scaler is not None else ts
    samples = model.historical_forecasts(
        ts_model,
        start=split,
        forecast_horizon=1,
        stride=1,
        retrain=retrain,
        last_points_only=True,
        verbose=False,
        num_samples=num_samples,
    )
    if scaler is not None:
        samples = scaler.inverse_transform(samples)
    # B1: puntuar solo sobre fechas F reales (los meses rellenados no son objetivo)
    return metrics.crps(ts.slice_intersect(samples), samples, dates=raw.index)


def demo() -> None:
    """Self-check: walk-forward de naïve + ARIMA sobre MX/F3/FAD, MASE finito."""
    for name in ("naive", "arima"):
        r = backtest(name, "mexico", "F3", "FAD")
        assert r.selection["n"] > 100, r.selection
        assert r.holdout["n"] == HOLDOUT, r.holdout
        assert r.selection["mase"] > 0
        print(
            f"{name:8s} sel: MASE={r.selection['mase']:.3f} sMAPE={r.selection['smape']:.2f} "
            f"| holdout: MAE={r.holdout['mae']:.0f}d MASE={r.holdout['mase']:.3f}"
        )
    print("OK — walk-forward sin leakage, holdout de 24 meses separado")


if __name__ == "__main__":
    demo()
