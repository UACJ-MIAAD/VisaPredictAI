"""Los 8 modelos de referencia tras una interfaz común (US-D1).

Todos se exponen como modelos darts (misma API ``fit``/``predict``/
``historical_forecasts``), lo que da comparación justa, walk-forward y bandas
probabilísticas sin reimplementar nada. El catálogo replica la Tabla de modelos de
referencia del Anteproyecto (§4.3):

    naive       — naïve estacional (baseline; denominador de MASE)
    arima       — ARIMA (lineal, no estacional)
    sarima      — SARIMA (estacionalidad anual)
    prophet     — Prophet (tendencia + estacionalidad con cambios de régimen)
    lstm        — LSTM determinista
    deepar      — LSTM probabilístico estilo DeepAR (intervalos nativos)
    arima_lstm  — cascada: ARIMA + LSTM sobre los residuales
    xgboost     — XGBoost sobre rezagos + regresores de calendario

Decisión (ponytail): darts ya trae 7 de los 8; solo la cascada ARIMA-LSTM es
código propio, y es un envoltorio delgado, no un modelo nuevo.
"""

from __future__ import annotations

# ponytail: el runtime OpenMP de xgboost DEBE cargarse antes que el de torch; el
# orden inverso segfaultea (doble libomp) en macOS. Por eso xgboost se importa
# primero y aislado del bloque ordenado por isort.
import xgboost  # noqa: F401

from collections.abc import Callable
from functools import partial
from typing import Protocol

import numpy as np
import pandas as pd
import torch
from darts import TimeSeries
from darts.dataprocessing.transformers import Scaler
from darts.models import (
    ARIMA,
    CatBoostModel,
    DLinearModel,
    ExponentialSmoothing,
    FourTheta,
    KalmanForecaster,
    LightGBMModel,
    LinearRegressionModel,
    NaiveSeasonal,
    NBEATSModel,
    NHiTSModel,
    NLinearModel,
    Prophet,
    RNNModel,
    TFTModel,
    TiDEModel,
    XGBModel,
)
from darts.utils.likelihood_models import GaussianLikelihood
from darts.utils.utils import ModelMode, SeasonalityMode

from vp_model import preprocess
from vp_model.config import (
    HYPERPARAMS,
    MODEL_NAMES,
    PROBABILISTIC,
    RANDOM_SEED,
    SEASONAL_PERIOD,
)

# ponytail: torch 2.12 / py3.14 en macOS segfaultea entrenando LSTM con múltiples
# hilos OpenMP; un solo hilo es estable. Subir si el throughput de entrenamiento
# llega a importar (no es el caso con series mensuales cortas).
torch.set_num_threads(1)

# Argumentos del trainer compartidos por todos los modelos torch: CPU determinista
# (devices=1 evita el segfault de MPS en Apple Silicon) y sin barra de progreso.
_TRAINER_KWARGS = {"enable_progress_bar": False, "accelerator": "cpu", "devices": 1}


class Forecaster(Protocol):
    """API mínima común a los 8 modelos (lo que usa el motor de walk-forward)."""

    def fit(self, series: TimeSeries, **kwargs: object) -> object: ...
    def predict(self, n: int, **kwargs: object) -> TimeSeries: ...


# Reexportados desde config para compatibilidad de la API pública del módulo
# (models.MODEL_NAMES / models.PROBABILISTIC siguen resolviendo).
__all__ = ["MODEL_NAMES", "PROBABILISTIC", "build_model", "registry", "to_timeseries", "ArimaLstm"]


def to_timeseries(series: pd.Series) -> TimeSeries:
    """pd.Series mensual -> darts TimeSeries con frecuencia regular y sin huecos.

    Reusa el criterio de huecos de ``preprocess`` (interpola cortos, deja largos);
    los NaN residuales se rellenan para que los modelos que no toleran huecos puedan
    entrenar. La conversión a float evita el dtype entero que rompe algunos modelos.
    """
    regular = preprocess.to_regular_monthly(series).astype("float64")
    ts = TimeSeries.from_series(regular)
    from darts.utils.missing_values import fill_missing_values

    return fill_missing_values(ts, fill="auto")


def _rnn(probabilistic: bool) -> RNNModel:
    # Hiperparámetros desde config; el tuning fino es trabajo futuro.
    return RNNModel(
        model="LSTM",
        **HYPERPARAMS["rnn"],
        random_state=RANDOM_SEED,
        likelihood=GaussianLikelihood() if probabilistic else None,
        pl_trainer_kwargs=_TRAINER_KWARGS,
    )


def _mlp(cls: type) -> Forecaster:
    """MLP/lineal torch (DLinear, NLinear, N-BEATS, N-HiTS, TiDE) con config común."""
    return cls(**HYPERPARAMS["mlp"], random_state=RANDOM_SEED, pl_trainer_kwargs=_TRAINER_KWARGS)


def build_model(name: str) -> Forecaster:
    """Fábrica: nombre -> modelo darts fresco (sin entrenar)."""
    # --- estadísticos parsimoniosos ---
    if name == "naive":
        return NaiveSeasonal(K=SEASONAL_PERIOD)
    if name == "arima":
        return ARIMA(**HYPERPARAMS["arima"])
    if name == "sarima":
        return ARIMA(**HYPERPARAMS["sarima"])
    if name == "prophet":
        return Prophet()
    if name == "ets":
        # ETS(A,Ad,N): tendencia aditiva amortiguada, sin estacionalidad (F_S~0).
        return ExponentialSmoothing(trend=ModelMode.ADDITIVE, damped=True, seasonal=None)
    if name == "theta":
        # Familia Theta optimizada (FourTheta), sin componente estacional.
        return FourTheta(season_mode=SeasonalityMode.NONE)
    if name == "kalman":
        return KalmanForecaster()
    # --- redes recurrentes ---
    if name == "lstm":
        return _rnn(probabilistic=False)
    if name == "deepar":
        return _rnn(probabilistic=True)
    if name == "arima_lstm":
        return ArimaLstm()
    # --- MLP / lineales modernos (torch) ---
    if name == "dlinear":
        return _mlp(DLinearModel)
    if name == "nlinear":
        return _mlp(NLinearModel)
    if name == "nbeats":
        return _mlp(NBEATSModel)
    if name == "nhits":
        return _mlp(NHiTSModel)
    if name == "tide":
        return _mlp(TiDEModel)
    if name == "tft":
        return TFTModel(**HYPERPARAMS["tft"], random_state=RANDOM_SEED, pl_trainer_kwargs=_TRAINER_KWARGS)
    if name == "chronos":
        return ChronosForecaster()
    # --- regresión lineal (ridge en forma cerrada vía sklearn) ---
    if name == "rlinear":
        return LinearRegressionModel(**HYPERPARAMS["rlinear"])
    # --- árboles: predicen el DELTA (Differenced) para poder extrapolar la tendencia ---
    if name == "xgboost":
        return Differenced(XGBModel(**HYPERPARAMS["trees"]))
    if name == "lightgbm":
        return Differenced(LightGBMModel(**HYPERPARAMS["trees"], verbose=-1))
    if name == "catboost":
        return Differenced(CatBoostModel(**HYPERPARAMS["trees"]))
    raise ValueError(f"modelo desconocido: {name!r}. Opciones: {MODEL_NAMES}")


class ArimaLstm:
    """Cascada Zhang (2003): ARIMA capta lo lineal, LSTM modela los residuales.

    No hereda de darts (su jerarquía interna es compleja); expone el subconjunto de
    la API que el motor de walk-forward usa: ``fit`` y ``predict``. Trabaja con
    ``TimeSeries`` para mantener la interfaz uniforme del resto del catálogo.
    """

    def __init__(self) -> None:
        self.arima = ARIMA(**HYPERPARAMS["arima"])
        # Ventana corta (rnn_hybrid): el LSTM ve solo los residuales que quedan tras
        # el warm-up de ARIMA, que son pocos en la ventana inicial.
        self.lstm = RNNModel(
            model="LSTM",
            **HYPERPARAMS["rnn_hybrid"],
            random_state=RANDOM_SEED,
            pl_trainer_kwargs=_TRAINER_KWARGS,
        )
        self._resid_scaler = Scaler()

    def fit(self, series: TimeSeries, **kwargs: object) -> ArimaLstm:
        self.arima.fit(series)
        # Residual = real - ajuste ARIMA one-step sobre el histórico (sin reentrenar).
        fitted = self.arima.historical_forecasts(
            series,
            forecast_horizon=1,
            stride=1,
            retrain=False,
            verbose=False,
            last_points_only=True,
        )
        actual = series.slice_intersect(fitted)
        resid = actual - fitted.slice_intersect(actual)
        self.lstm.fit(self._resid_scaler.fit_transform(resid))
        return self

    def predict(self, n: int, **kwargs: object) -> TimeSeries:
        linear = self.arima.predict(n)
        resid_fc = self._resid_scaler.inverse_transform(self.lstm.predict(n))
        return linear + resid_fc.slice_intersect(linear)

    def historical_forecasts(
        self,
        series: TimeSeries,
        *,
        start: int,
        forecast_horizon: int = 1,
        stride: int = 1,
        retrain: bool = False,
        last_points_only: bool = True,
        verbose: bool = False,
        **kwargs: object,
    ) -> TimeSeries:
        """Walk-forward del híbrido: ajusta una vez sobre la ventana inicial y rueda.

        Reentrenar ARIMA+LSTM en cada origen es inviable; se fija el modelo sobre los
        primeros ``start`` meses (leakage-free para la región de selección) y se
        produce el pronóstico lineal + el de residuales a 1 paso de forma rodante.
        """
        self.fit(series[:start])
        linear = self.arima.historical_forecasts(
            series,
            start=start,
            forecast_horizon=1,
            stride=1,
            retrain=False,
            last_points_only=True,
            verbose=False,
        )
        # Residual histórico (real - ARIMA fijo) escalado con el scaler ya ajustado.
        arima_full = self.arima.historical_forecasts(
            series,
            forecast_horizon=1,
            stride=1,
            retrain=False,
            last_points_only=True,
            verbose=False,
        )
        resid = series.slice_intersect(arima_full) - arima_full.slice_intersect(series)
        resid_scaled = self._resid_scaler.transform(resid)
        resid_fc = self._resid_scaler.inverse_transform(
            self.lstm.historical_forecasts(
                resid_scaled,
                start=resid_scaled.get_index_at_point(linear.start_time()),
                forecast_horizon=1,
                stride=1,
                retrain=False,
                last_points_only=True,
                verbose=False,
            )
        )
        return linear.slice_intersect(resid_fc) + resid_fc.slice_intersect(linear)


class Differenced:
    """Envuelve un modelo de regresión para que prediga la PRIMERA DIFERENCIA y reintegre.

    Los árboles de decisión solo predicen dentro del rango de valores visto en
    entrenamiento; sobre el nivel (días desde la época base) se saturan al máximo
    histórico y subestiman todo el horizonte futuro de una serie con tendencia. Modelar
    el delta mensual (que SÍ es estacionario) y reintegrar resuelve la extrapolación.
    Expone fit/predict/historical_forecasts con covariables, como el resto del catálogo.
    """

    def __init__(self, base: Forecaster) -> None:
        self.base = base
        self._last_level: float = 0.0

    def fit(self, series: TimeSeries, **kwargs: object) -> Differenced:
        self._last_level = float(series.values()[-1, 0])
        self.base.fit(series.diff(), **kwargs)
        return self

    def predict(self, n: int, **kwargs: object) -> TimeSeries:
        diff_fc = self.base.predict(n, **kwargs)
        vals = self._last_level + np.cumsum(diff_fc.values().flatten())
        return TimeSeries.from_times_and_values(diff_fc.time_index, vals)

    def historical_forecasts(
        self,
        series: TimeSeries,
        *,
        start: int,
        forecast_horizon: int = 1,
        stride: int = 1,
        retrain: bool = True,
        last_points_only: bool = True,
        verbose: bool = False,
        **kwargs: object,
    ) -> TimeSeries:
        # Backtest 1-paso sobre la serie diferenciada; cada delta pronosticado se reintegra
        # sumándolo al ÚLTIMO nivel observado (causal: conocido en el origen). Leakage-free.
        diff_fc = self.base.historical_forecasts(  # type: ignore[attr-defined]
            series.diff(),
            start=start,
            forecast_horizon=1,
            stride=1,
            retrain=retrain,
            last_points_only=True,
            verbose=False,
            **kwargs,
        )
        prev_level = series.shift(1).slice_intersect(diff_fc)
        return prev_level + diff_fc.slice_intersect(prev_level)


class ChronosForecaster:
    """Foundation model zero-shot (Amazon Chronos-Bolt): pronostica por TRANSFERENCIA.

    No se entrena en la serie: un modelo preentrenado en millones de series condiciona
    sobre el contexto histórico y emite cuantiles. Aborda de raíz el problema de n
    pequeño (n=125-290) que hunde a los modelos profundos entrenados localmente. La
    canalización se cachea a nivel de clase (se descarga una sola vez). Corre en CPU.
    """

    _pipe: object = None

    def __init__(self, model: str | None = None) -> None:
        from vp_model.config import CHRONOS_MODEL

        self.model = model or CHRONOS_MODEL
        self._series: TimeSeries | None = None

    @classmethod
    def _pipeline(cls, model: str) -> object:
        if cls._pipe is None:
            from chronos import BaseChronosPipeline

            cls._pipe = BaseChronosPipeline.from_pretrained(model, device_map="cpu")
        return cls._pipe

    def _q(self, context: np.ndarray, n: int) -> np.ndarray:
        _, mean = self._pipeline(self.model).predict_quantiles(  # type: ignore[attr-defined]
            torch.tensor(context, dtype=torch.float32), prediction_length=n, quantile_levels=[0.5]
        )
        return mean.numpy().flatten()

    def fit(self, series: TimeSeries, **kwargs: object) -> ChronosForecaster:
        self._series = series
        return self

    def predict(self, n: int, **kwargs: object) -> TimeSeries:
        assert self._series is not None
        idx = pd.date_range(self._series.end_time(), periods=n + 1, freq=self._series.freq_str)[1:]
        return TimeSeries.from_times_and_values(idx, self._q(self._series.values().flatten(), n))

    def historical_forecasts(
        self,
        series: TimeSeries,
        *,
        start: int,
        forecast_horizon: int = 1,
        stride: int = 1,
        retrain: bool = False,
        last_points_only: bool = True,
        verbose: bool = False,
        **kwargs: object,
    ) -> TimeSeries:
        # Pronóstico a 1 paso en cada origen condicionando SOLO sobre el pasado (zero-shot,
        # leakage-free por construcción): el contexto en el origen t son los datos [0:t].
        vals = series.values().flatten()
        preds = [float(self._q(vals[:t], 1)[0]) for t in range(start, len(series))]
        idx = series.time_index[start:]
        return TimeSeries.from_times_and_values(idx, np.asarray(preds))


def registry() -> dict[str, Callable[[], Forecaster]]:
    """Mapa nombre -> fábrica perezosa (un modelo nuevo por llamada)."""
    return {name: partial(build_model, name) for name in MODEL_NAMES}


def demo() -> None:
    """Self-check: cada modelo entrena y pronostica 12 meses sobre una serie piloto."""
    from vp_model import dataset

    s = dataset.load_series("mexico", "F3", "FAD")
    ts = to_timeseries(s)
    train = ts[:-12]
    # Solo los baratos en el self-check (las NN tardan); el resto se valida en Épica E.
    for name in ("naive", "arima", "sarima", "xgboost"):
        model = build_model(name)
        if name == "xgboost":
            cov = TimeSeries.from_dataframe(preprocess.calendar_features(ts.time_index))
            model.fit(train, future_covariates=cov)
            fc = model.predict(12, future_covariates=cov)
        else:
            model.fit(train)
            fc = model.predict(12)
        assert len(fc) == 12, name
        assert np.isfinite(fc.values()).all(), f"{name} produjo NaN"
    print(
        f"OK — 4 modelos rápidos entrenan y pronostican 12 meses sobre MX/F3/FAD "
        f"({len(train)} train); catálogo completo = {len(MODEL_NAMES)} modelos"
    )


if __name__ == "__main__":
    demo()
