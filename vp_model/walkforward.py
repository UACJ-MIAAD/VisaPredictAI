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

from dataclasses import dataclass

from darts import TimeSeries

from vp_model import dataset, intervals, metrics, models
from vp_model.config import (
    HOLDOUT,
    MIN_BACKTEST_BUFFER,
    MIN_TRAIN,
    NN_RETRAIN,
    PROBABILISTIC,
    RETRAIN_EACH_STEP,
)
from vp_model.feature_builder import FeatureBuilder


@dataclass(frozen=True)
class BacktestResult:
    model: str
    country: str
    category: str
    table: str
    selection: dict[str, float]  # métricas en la región de selección (pre-holdout)
    holdout: dict[str, float]  # métricas en los 24 meses reservados


def run_forecasts(
    model_name: str, country: str, category: str, table: str, model: object | None = None
) -> tuple[TimeSeries, TimeSeries]:
    """Pronósticos walk-forward a 1 paso (toda la serie desde min_train) + la serie real.

    Devuelve (ts, forecasts) leakage-free: cada origen solo ve el pasado. Lo usan
    ``backtest`` (para métricas) y la persistencia de pronósticos (para ensambles).
    El FE (huecos, covariables, escalado) lo compone ``FeatureBuilder`` según la
    política por modelo de config — mismo comportamiento, linaje explícito (AD1).
    """
    fe = FeatureBuilder(model_name)
    ts = fe.to_timeseries(dataset.load_series(country, category, table))
    min_train = MIN_TRAIN[table]
    if len(ts) < min_train + HOLDOUT + MIN_BACKTEST_BUFFER:
        raise ValueError(f"serie demasiado corta ({len(ts)}) para min_train={min_train}+holdout={HOLDOUT}")

    model = models.build_model(model_name) if model is None else model
    retrain: bool | int = True if model_name in RETRAIN_EACH_STEP else NN_RETRAIN
    # historical_forecasts recibe future_covariates UNA vez (lo usa en fit y predict).
    cov = fe.covariates(ts)
    extra = {"future_covariates": cov} if cov is not None else {}

    # Escalado leakage-free para redes: Scaler ajustado solo en la ventana inicial.
    scaler = fe.fit_scaler(ts, min_train)
    ts_model = scaler.transform(ts) if scaler is not None else ts

    forecasts = model.historical_forecasts(  # type: ignore[attr-defined]
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
    return ts, forecasts


def backtest(model_name: str, country: str, category: str, table: str, model: object | None = None) -> BacktestResult:
    """Corre el walk-forward de un modelo sobre una serie y devuelve sus métricas.

    Las métricas de *selección* se calculan sobre los pronósticos previos al hold-out
    (lo que se usa para comparar modelos); las de *hold-out* sobre los últimos 24
    meses (evaluación final del modelo elegido, US-F2). ``model`` permite inyectar un
    modelo ya configurado (el tuner pasa una plantilla con hiperparámetros de prueba);
    si es ``None`` se usa ``build_model(model_name)`` con los defaults de config.
    """
    ts, forecasts = run_forecasts(model_name, country, category, table, model)
    # B1: las métricas se evalúan SOLO sobre observaciones F reales. `ts` viene rellenado
    # por `to_timeseries` (continuidad para entrenar); puntuar los meses interpolados
    # deprimía el error y hacía la vía local incomparable con la global (que ya enmascara
    # en `eval_neuralforecast.eval_global_deep`). La escala del MASE usa la MISMA fuente
    # única que la vía global: naïve estacional sobre la serie F cruda pre-hold-out.
    raw = dataset.load_series(country, category, table).astype("float64")
    fdates = raw.index
    split = ts.time_index[-HOLDOUT]
    scale = metrics.naive_scale_before(raw, split)
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
    except ValueError, IndexError:
        prob = {"msis": float("nan"), "interval_score": float("nan"), "coverage": float("nan")}
    holdout = {**metrics.compute(hold_actual, hold_fc, insample, dates=fdates, scale=scale), **prob}
    return BacktestResult(
        model=model_name,
        country=country,
        category=category,
        table=table,
        selection=metrics.compute(sel_actual, sel_fc, insample, dates=fdates, scale=scale),
        holdout=holdout,
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
    ts = fe.to_timeseries(dataset.load_series(country, category, table))
    split = ts.time_index[-HOLDOUT]
    model = models.build_model(model_name)
    retrain: bool | int = True if model_name in RETRAIN_EACH_STEP else NN_RETRAIN
    scaler = fe.fit_scaler(ts, len(ts) - HOLDOUT)
    ts_model = scaler.transform(ts) if scaler is not None else ts
    samples = model.historical_forecasts(  # type: ignore[attr-defined]
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
    # B1: puntuar solo sobre fechas F reales (los meses interpolados no son objetivo)
    fdates = dataset.load_series(country, category, table).index
    return metrics.crps(ts.slice_intersect(samples), samples, dates=fdates)


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
