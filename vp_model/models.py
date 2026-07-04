"""El catálogo de modelos tras una interfaz común (US-D1).

El catálogo completo son **23 modelos** (``config.MODEL_NAMES``: +ETS, Theta, Kalman,
DLinear, NLinear, RLinear, N-BEATS, N-HiTS, TiDE, LightGBM, CatBoost, TFT, Chronos, y
los pisos honestos naive1/drift de AI1). Los 8 de referencia originales del
Anteproyecto (§4.3) son el núcleo:

Todos se exponen como modelos darts (misma API ``fit``/``predict``/
``historical_forecasts``), lo que da comparación justa, walk-forward y bandas
probabilísticas sin reimplementar nada. Núcleo de referencia:

    naive       — naïve estacional (baseline; denominador de MASE)
    naive1      — naïve no estacional (random walk; piso AI1)
    drift       — random walk con deriva (piso AI1)
    arima       — ARIMA (lineal, no estacional)
    sarima      — SARIMA (estacionalidad anual)
    prophet     — Prophet (tendencia + estacionalidad con cambios de régimen)
    lstm        — LSTM determinista
    deepar      — LSTM probabilístico estilo DeepAR (intervalos nativos)
    arima_lstm  — cascada: ARIMA + LSTM sobre los residuales
    xgboost     — XGBoost sobre rezagos + regresores de calendario

AP3: ``_FACTORIES`` (dict nombre -> fábrica) es LA fuente del catálogo; ``build_model``
es un lookup y un assert de import mantiene ``config.MODEL_NAMES`` sincronizado.

Decisión (ponytail): darts cubre casi todo el catálogo; solo la cascada ARIMA-LSTM es
código propio, y es un envoltorio delgado, no un modelo nuevo.
"""

from __future__ import annotations

# ponytail: el runtime OpenMP de xgboost DEBE cargarse antes que el de torch; el
# orden inverso segfaultea (doble libomp) en macOS. Por eso xgboost se importa
# primero y aislado del bloque ordenado por isort.
import xgboost  # noqa: F401

import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd
import torch
from darts import TimeSeries, concatenate
from darts.dataprocessing.transformers import Scaler
from darts.models import (
    ARIMA,
    CatBoostModel,
    DLinearModel,
    ExponentialSmoothing,
    FourTheta,
    KalmanForecaster,
    LightGBMModel,
    NaiveDrift,
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
from darts.models import SKLearnModel  # RegressionModel is deprecated in darts 0.44
from darts.utils.likelihood_models import GaussianLikelihood
from darts.utils.utils import ModelMode
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from vp_model import preprocess
from vp_model.config import (
    DIFFERENCED,
    HYPERPARAMS,
    MODEL_NAMES,
    NN_RETRAIN,
    PROBABILISTIC,
    RANDOM_SEED,
    SEASONAL_PERIOD,
)

# ponytail: torch 2.12 / py3.14 en macOS segfaultea entrenando LSTM con múltiples
# hilos OpenMP; un solo hilo es estable. Subir si el throughput de entrenamiento
# llega a importar (no es el caso con series mensuales cortas).
torch.set_num_threads(1)

# Argumentos del trainer compartidos por todos los modelos torch: CPU determinista
# (devices=1 evita el segfault de MPS en Apple Silicon), sin barra de progreso y sin
# logger (T2: evita que cada fit deje un lightning_logs/version_N/ huérfano en la raíz).
_TRAINER_KWARGS = {"enable_progress_bar": False, "accelerator": "cpu", "devices": 1, "logger": False}


class Forecaster(Protocol):
    """API común al catálogo de modelos (lo que usa el motor de walk-forward).

    AP1: ``historical_forecasts`` entró al Protocol con firma laxa (``start`` +
    ``**kwargs``, espejo de la convención darts), así mypy vuelve a vigilar los
    call-sites del backtesting sin ``# type: ignore[attr-defined]``.
    """

    def fit(self, series: TimeSeries, **kwargs: object) -> object: ...
    def predict(self, n: int, **kwargs: object) -> TimeSeries: ...
    def historical_forecasts(
        self, series: TimeSeries, *, start: int | pd.Timestamp, **kwargs: object
    ) -> TimeSeries: ...


# Reexportados desde config para compatibilidad de la API pública del módulo
# (models.MODEL_NAMES / models.PROBABILISTIC siguen resolviendo).
__all__ = ["MODEL_NAMES", "PROBABILISTIC", "build_model", "registry", "to_timeseries", "ArimaLstm"]


def to_timeseries(series: pd.Series) -> TimeSeries:
    """pd.Series mensual -> darts TimeSeries con frecuencia regular y sin huecos.

    Reusa el criterio de huecos de ``preprocess`` (interpola cortos, deja largos);
    los NaN residuales se rellenan para que los modelos que no toleran huecos puedan
    entrenar. La conversión a float evita el dtype entero que rompe algunos modelos.

    ⚠️ Los valores rellenados existen SOLO para dar continuidad al entrenamiento:
    NO son objetivo predictivo y la evaluación los enmascara (B1 — `walkforward.backtest`
    puntúa únicamente sobre las fechas F reales de `dataset.load_series`).
    """
    regular = preprocess.to_regular_monthly(series).astype("float64")
    ts = TimeSeries.from_series(regular)
    from darts.utils.missing_values import fill_missing_values

    filled = fill_missing_values(ts, fill="auto")
    # AB4: el relleno de continuidad es una decisión consciente (docs/CLEANING.md,
    # gap_policy_training) — el invariante duro es que no quede NaN y que la
    # evaluación enmascare todo punto fabricado (B1).
    assert not np.isnan(filled.values(copy=False)).any(), "to_timeseries: quedaron NaN tras fill_missing_values"
    return filled


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


_TUNED_PARAMS_PATH = Path(__file__).resolve().parent.parent / "reports" / "eval" / "tuned_params.json"


def _tree_params(model: str, table: str | None) -> dict:
    """AJ5: HPO winners (reports/eval/tuned_params.json) become the GBM defaults.

    Without this the model-pool table compared an auto-selected ETS/Theta against
    library-default trees. When a winning tuned entry exists for the (model, table)
    cell it overrides the slow-learning bridge in ``HYPERPARAMS['trees']``
    (lr=0.02 / 200 trees — see config); with ``table=None`` (callers that build a
    model outside a table context) the bridge stands. ``table`` accepts either a
    bare table name ("FAD" -> family block, legacy callers) or a full tuning-group
    key ("FAD_employment") so EB series get their own accepted HPO winners (AK7).
    """
    params = dict(HYPERPARAMS["trees"])
    if table is not None and _TUNED_PARAMS_PATH.exists():
        key = table if "_" in table else f"{table}_family"
        entry = json.loads(_TUNED_PARAMS_PATH.read_text()).get(model, {}).get(key, {})
        if entry.get("improved"):
            params.update(entry.get("best_params", {}))
    return params


def _ridge() -> SKLearnModel:
    # AJ6: a REAL ridge — the docstring/deliverable say "ridge" but darts
    # LinearRegressionModel is plain OLS. Lags are standardized inside the
    # pipeline so the L2 penalty (alpha=1.0, sklearn default) is meaningful on
    # levels of ~1e4 days; without scaling the penalty would be vacuous.
    return SKLearnModel(model=make_pipeline(StandardScaler(), Ridge(alpha=1.0)), **HYPERPARAMS["rlinear"])


def _prophet() -> Prophet:
    # AJ7: Prophet defaults (25 changepoints over the first 80 % of the window)
    # put ~1 changepoint every 2 observations on the shortest 60-month training
    # windows — pure overfit. 8 potential changepoints keeps flexibility for the
    # documented retrogression regimes (~1 per 2 years on the longest windows)
    # while the default prior scale 0.05 (made explicit) shrinks unused ones.
    return Prophet(n_changepoints=8, changepoint_prior_scale=0.05)


# AP3: THE source of the catalog. Each factory receives the table (FAD/DFF) so
# table-specific tuned hyperparameters can be routed (only the GBMs use it today, AJ5).
# Adding a model = one entry here + its name in config.MODEL_NAMES (assert below).
_FACTORIES: dict[str, Callable[[str | None], Forecaster]] = {
    # --- parsimonious statistical models ---
    "naive": lambda table: NaiveSeasonal(K=SEASONAL_PERIOD),
    "naive1": lambda table: NaiveSeasonal(K=1),  # AI1: random-walk floor of the pool
    "drift": lambda table: NaiveDrift(),  # AI1: random walk with drift
    "arima": lambda table: ARIMA(**HYPERPARAMS["arima"]),
    "sarima": lambda table: ARIMA(**HYPERPARAMS["sarima"]),
    "prophet": lambda table: _prophet(),
    "ets": lambda table: AutoETS(),  # AJ4: small AICc search over {trend x damped}
    "theta": lambda table: AutoTheta(),  # AJ4: FourTheta.select_best_model
    # AI4: dim_x=2 = local linear trend (level + slope); dim_x=1 was a local level
    # unable to extrapolate the decades-long trend of these series.
    "kalman": lambda table: KalmanForecaster(dim_x=2),
    # --- recurrent nets (differenced, AJ1) ---
    "lstm": lambda table: Differenced(_rnn(probabilistic=False)),
    "deepar": lambda table: Differenced(_rnn(probabilistic=True)),
    "arima_lstm": lambda table: ArimaLstm(),
    # --- modern MLP / linear nets (torch; differenced, AJ1) ---
    "dlinear": lambda table: Differenced(_mlp(DLinearModel)),
    "nlinear": lambda table: Differenced(_mlp(NLinearModel)),
    "nbeats": lambda table: Differenced(_mlp(NBEATSModel)),
    "nhits": lambda table: Differenced(_mlp(NHiTSModel)),
    "tide": lambda table: Differenced(_mlp(TiDEModel)),
    "tft": lambda table: Differenced(
        TFTModel(**HYPERPARAMS["tft"], random_state=RANDOM_SEED, pl_trainer_kwargs=_TRAINER_KWARGS)
    ),
    "chronos": lambda table: ChronosForecaster(),
    # --- regularized linear regression (real ridge, AJ6) ---
    "rlinear": lambda table: _ridge(),
    # --- trees: predict the DELTA (Differenced) so they can extrapolate the trend ---
    "xgboost": lambda table: Differenced(XGBModel(**_tree_params("xgboost", table))),
    "lightgbm": lambda table: Differenced(LightGBMModel(**_tree_params("lightgbm", table), verbose=-1)),
    "catboost": lambda table: Differenced(CatBoostModel(**_tree_params("catboost", table))),
    # AL4: structural local-linear-trend state space (statsmodels MLE, analytic PIs).
    "llt": lambda table: LLTForecaster(),
}

# AL4: factories reachable through build_model but not part of the canonical campaign
# catalog. llt WAS promoted into config.MODEL_NAMES for the AQ re-campaign (4-jul-2026):
# analytic PIs + same parsimony class as ets/theta; the set stays as the mechanism for
# future candidates.
_EXTRA_MODELS: frozenset[str] = frozenset()

# Catalog drift guard: config.MODEL_NAMES (ordering, dependency-light) plus the declared
# extras and _FACTORIES (factories) must be the SAME set — fail at import time, not
# mid-campaign.
assert set(_FACTORIES) == set(MODEL_NAMES) | _EXTRA_MODELS, (
    f"catalog out of sync: _FACTORIES={sorted(_FACTORIES)} vs "
    f"config.MODEL_NAMES+extras={sorted(set(MODEL_NAMES) | _EXTRA_MODELS)}"
)


def build_model(name: str, table: str | None = None, block: str = "family") -> Forecaster:
    """Factory: name -> fresh (untrained) model. Lookup over ``_FACTORIES`` (AP3).

    ``table`` ("FAD"/"DFF") routes table-specific tuned hyperparameters to the
    GBMs (AJ5); ``None`` keeps the bridge defaults from config. ``block``
    ("family"/"employment") completes the tuning-group key so EB series use
    their own accepted HPO winners (AK7) — other models ignore it.
    """
    if name not in _FACTORIES:
        raise ValueError(f"modelo desconocido: {name!r}. Opciones: {MODEL_NAMES}")
    arg = f"{table}_{block}" if name in DIFFERENCED and table is not None else table
    return _FACTORIES[name](arg)


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
        # AJ3: fit is now called repeatedly (annual refit) — rebuild fresh
        # sub-models so each refit trains from scratch instead of resuming.
        self.arima = ARIMA(**HYPERPARAMS["arima"])
        self.lstm = RNNModel(
            model="LSTM",
            **HYPERPARAMS["rnn_hybrid"],
            random_state=RANDOM_SEED,
            pl_trainer_kwargs=_TRAINER_KWARGS,
        )
        self._resid_scaler = Scaler()
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

    def historical_forecasts(self, series: TimeSeries, *, start: int | pd.Timestamp, **kwargs: object) -> TimeSeries:
        """Walk-forward of the hybrid with ANNUAL REFIT (AJ3), fixed 1-step horizon.

        Previously the cascade was fit ONCE on the initial window and rolled
        frozen through ~15 years of origins. ARIMA+LSTM are now refit every
        ``NN_RETRAIN`` months (same cost/validity compromise as the torch nets):
        each block is forecast by a model trained ONLY on data before the block
        (leakage-free). The darts convenience kwargs (retrain/stride/...) are
        ignored: the cascade defines its own protocol.
        """
        start_idx = start if isinstance(start, int) else series.get_index_at_point(start)
        blocks = []
        for t0 in range(start_idx, len(series), NN_RETRAIN):
            t1 = min(t0 + NN_RETRAIN, len(series))
            blocks.append(self._block_forecasts(series, t0, t1))
        return concatenate(blocks, axis=0) if len(blocks) > 1 else blocks[0]

    def _block_forecasts(self, series: TimeSeries, t0: int, t1: int) -> TimeSeries:
        """One-step forecasts for origins [t0, t1) with the model fitted on [:t0]."""
        sub = series[:t1]
        self.fit(series[:t0])
        linear = self.arima.historical_forecasts(
            sub,
            start=t0,
            forecast_horizon=1,
            stride=1,
            retrain=False,
            last_points_only=True,
            verbose=False,
        )
        # Residual histórico (real - ARIMA fijo) escalado con el scaler ya ajustado.
        arima_full = self.arima.historical_forecasts(
            sub,
            forecast_horizon=1,
            stride=1,
            retrain=False,
            last_points_only=True,
            verbose=False,
        )
        resid = sub.slice_intersect(arima_full) - arima_full.slice_intersect(sub)
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
        # AJ2: per-SAMPLE reintegration (cumsum along the time axis) — a
        # probabilistic base (deepar/tft) emits (time, component, samples) and the
        # previous flatten collapsed the samples into one corrupt trajectory.
        vals = self._last_level + np.cumsum(diff_fc.all_values(copy=False), axis=0)
        return TimeSeries.from_times_and_values(diff_fc.time_index, vals)

    def historical_forecasts(self, series: TimeSeries, *, start: int | pd.Timestamp, **kwargs: object) -> TimeSeries:
        # One-step backtest on the differenced series; each forecast delta is
        # reintegrated onto the LAST observed level (causal: known at the origin).
        # Leakage-free. Defaults identical to the wrapper's historical behavior
        # (callers may override any of them).
        kwargs.setdefault("forecast_horizon", 1)
        kwargs.setdefault("stride", 1)
        kwargs.setdefault("retrain", True)
        kwargs.setdefault("last_points_only", True)
        kwargs.setdefault("verbose", False)
        diff_fc = self.base.historical_forecasts(series.diff(), start=start, **kwargs)
        prev_level = series.shift(1).slice_intersect(diff_fc)
        diff_al = diff_fc.slice_intersect(prev_level)
        # numpy addition with broadcasting (t,1,1)+(t,1,s): also valid when the base
        # emits samples (deepar/tft under AJ1+AJ2); s=1 reproduces the classic case.
        vals = prev_level.all_values(copy=False) + diff_al.all_values(copy=False)
        return TimeSeries.from_times_and_values(prev_level.time_index, vals)


class ChronosForecaster:
    """Foundation model zero-shot (Amazon Chronos-Bolt): pronostica por TRANSFERENCIA.

    No se entrena en la serie: un modelo preentrenado en millones de series condiciona
    sobre el contexto histórico y emite cuantiles. Aborda de raíz el problema de n
    pequeño (n=130-296) que hunde a los modelos profundos entrenados localmente. La
    canalización se cachea a nivel de clase POR NOMBRE de modelo (AI5: el cache sin
    key servía en silencio el primer checkpoint cargado a cualquier instancia con
    otro modelo). Corre en CPU.
    """

    _pipes: dict[str, object] = {}

    def __init__(self, model: str | None = None) -> None:
        from vp_model.config import CHRONOS_MODEL

        self.model = model or CHRONOS_MODEL
        self._series: TimeSeries | None = None

    @classmethod
    def _pipeline(cls, model: str) -> object:
        if model not in cls._pipes:
            from chronos import BaseChronosPipeline

            cls._pipes[model] = BaseChronosPipeline.from_pretrained(model, device_map="cpu")
        return cls._pipes[model]

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

    def historical_forecasts(self, series: TimeSeries, *, start: int | pd.Timestamp, **kwargs: object) -> TimeSeries:
        # Pronóstico a 1 paso en cada origen condicionando SOLO sobre el pasado (zero-shot,
        # leakage-free por construcción): el contexto en el origen t son los datos [0:t].
        # Los kwargs de conveniencia darts (retrain/stride/…) no aplican a un zero-shot.
        start_idx = start if isinstance(start, int) else series.get_index_at_point(start)
        vals = series.values().flatten()
        preds = [float(self._q(vals[:t], 1)[0]) for t in range(start_idx, len(series))]
        idx = series.time_index[start_idx:]
        return TimeSeries.from_times_and_values(idx, np.asarray(preds))


class AutoTheta:
    """FourTheta with DATA-DRIVEN variant selection (AJ4) instead of a fixed θ=2.

    ``FourTheta.select_best_model`` (deterministic darts gridsearch on in-sample
    fitted values) picks θ∈{0,1,2,3} plus the trend/model modes. In the
    walk-forward the selection runs ONCE on the initial window
    (``series[:start]`` — leakage-free for the selection region) and the winning
    configuration is then refit at every origin like any cheap statistical model.
    """

    def __init__(self) -> None:
        self._model: FourTheta | None = None

    @staticmethod
    def _select(train: TimeSeries) -> FourTheta:
        # darts 0.44: select_best_model returns the gridsearch tuple
        # (model, params, score) despite its annotation — keep the model only.
        best = FourTheta.select_best_model(train, thetas=[0, 1, 2, 3])
        return best[0] if isinstance(best, tuple) else best

    def fit(self, series: TimeSeries, **kwargs: object) -> AutoTheta:
        self._model = self._select(series)
        self._model.fit(series)
        return self

    def predict(self, n: int, **kwargs: object) -> TimeSeries:
        assert self._model is not None, "AutoTheta: fit first"
        return self._model.predict(n)

    def historical_forecasts(self, series: TimeSeries, *, start: int | pd.Timestamp, **kwargs: object) -> TimeSeries:
        start_idx = start if isinstance(start, int) else series.get_index_at_point(start)
        model = self._select(series[:start_idx])  # selection sees only pre-origin data
        kwargs.setdefault("forecast_horizon", 1)
        kwargs.setdefault("stride", 1)
        kwargs.setdefault("retrain", True)
        kwargs.setdefault("last_points_only", True)
        kwargs.setdefault("verbose", False)
        return model.historical_forecasts(series, start=start_idx, **kwargs)


class AutoETS:
    """ETS with a small AICc search over {trend x damped} (AJ4).

    Non-seasonal candidates (F_S ~ 0 on these series): (N,N), (A,N), (A_d,N).
    The AICc is computed with statsmodels on the training window ONLY; the winning
    spec is refit at every origin through the darts wrapper. Previously the
    catalog imposed ETS(A,A_d,N) on all 74 series alike.
    """

    # (darts ModelMode trend, damped) pairs; None maps to statsmodels trend=None.
    _CANDIDATES = ((None, False), (ModelMode.ADDITIVE, False), (ModelMode.ADDITIVE, True))

    def __init__(self) -> None:
        self._model: ExponentialSmoothing | None = None

    @classmethod
    def _select(cls, train: TimeSeries) -> ExponentialSmoothing:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing as SmETS

        y = train.values(copy=False).flatten().astype("float64")
        best, best_aicc = (ModelMode.ADDITIVE, True), np.inf  # fallback = historical ETS(A,Ad,N)
        for trend, damped in cls._CANDIDATES:
            try:
                res = SmETS(y, trend=None if trend is None else "add", damped_trend=damped, seasonal=None).fit()
                aicc = float(res.aicc)
            except Exception:  # noqa: BLE001 — unstable candidate -> discarded
                continue
            if np.isfinite(aicc) and aicc < best_aicc:
                best_aicc, best = aicc, (trend, damped)
        trend, damped = best
        return ExponentialSmoothing(trend=ModelMode.NONE if trend is None else trend, damped=damped, seasonal=None)

    def fit(self, series: TimeSeries, **kwargs: object) -> AutoETS:
        self._model = self._select(series)
        self._model.fit(series)
        return self

    def predict(self, n: int, **kwargs: object) -> TimeSeries:
        assert self._model is not None, "AutoETS: fit first"
        return self._model.predict(n)

    def historical_forecasts(self, series: TimeSeries, *, start: int | pd.Timestamp, **kwargs: object) -> TimeSeries:
        start_idx = start if isinstance(start, int) else series.get_index_at_point(start)
        model = self._select(series[:start_idx])  # selection sees only pre-origin data
        kwargs.setdefault("forecast_horizon", 1)
        kwargs.setdefault("stride", 1)
        kwargs.setdefault("retrain", True)
        kwargs.setdefault("last_points_only", True)
        kwargs.setdefault("verbose", False)
        return model.historical_forecasts(series, start=start_idx, **kwargs)


class LLTForecaster:
    """AL4: structural local linear trend (statsmodels ``UnobservedComponents``).

    State space y_t = level_t + eps; level evolves with a stochastic slope —
    the physics of these series (a queue advancing at a slowly varying speed).
    MLE-fitted variances and ANALYTIC prediction intervals (``get_forecast``),
    unlike the N4SID-fitted darts ``KalmanForecaster``.

    Redundancy decision (vs the ``kalman`` entry AI4 added): both stay for now.
    They are NOT the same model — ``kalman`` fits a generic dim_x=2 state space
    by subspace identification (N4SID, no likelihood), while ``llt`` fits the
    structural LLT by MLE and exposes analytic PIs, the property epic AN cares
    about. The AQ campaign compares them head-to-head and drops the loser.
    A DAMPED slope variant was considered and rejected: ``UnobservedComponents``
    has no damped-trend spec, and the damped-trend hypothesis is already covered
    by AutoETS's (A, A_d, N) candidate.

    ``historical_forecasts`` implements the protocol directly (statsmodels has
    no darts API): MLE re-estimation every ``NN_RETRAIN`` origins (same
    cost/validity compromise as the nets), and in between the Kalman FILTER is
    re-run on the expanding window with the last MLE params — every origin
    conditions on all data up to it, parameters come only from the past
    (leakage-free).
    """

    _SPEC = "local linear trend"

    def __init__(self) -> None:
        self._res: object | None = None
        self._series: TimeSeries | None = None

    @classmethod
    def _fit_mle(cls, y: np.ndarray):  # noqa: ANN206 — statsmodels results type, runtime only
        from statsmodels.tsa.statespace.structural import UnobservedComponents

        return UnobservedComponents(y, level=cls._SPEC).fit(disp=0)

    def fit(self, series: TimeSeries, **kwargs: object) -> LLTForecaster:
        self._series = series
        self._res = self._fit_mle(series.values(copy=False).flatten().astype("float64"))
        return self

    def predict(self, n: int, **kwargs: object) -> TimeSeries:
        assert self._res is not None and self._series is not None, "LLTForecaster: fit first"
        mean = np.asarray(self._res.forecast(n), dtype="float64")  # type: ignore[attr-defined]
        idx = pd.date_range(self._series.end_time(), periods=n + 1, freq=self._series.freq_str)[1:]
        return TimeSeries.from_times_and_values(idx, mean)

    def historical_forecasts(self, series: TimeSeries, *, start: int | pd.Timestamp, **kwargs: object) -> TimeSeries:
        # darts convenience kwargs (retrain/stride/...) are ignored: the protocol
        # (1-step, expanding, periodic MLE refit) is defined here.
        from statsmodels.tsa.statespace.structural import UnobservedComponents

        start_idx = start if isinstance(start, int) else series.get_index_at_point(start)
        y = series.values(copy=False).flatten().astype("float64")
        preds: list[float] = []
        params = None
        for step, t in enumerate(range(start_idx, len(series))):
            if params is None or step % NN_RETRAIN == 0:
                try:
                    params = self._fit_mle(y[:t]).params
                except Exception:  # noqa: BLE001 — keep the last stable params on an unstable window
                    if params is None:
                        raise
            res = UnobservedComponents(y[:t], level=self._SPEC).filter(params)
            preds.append(float(res.forecast(1)[0]))
        return TimeSeries.from_times_and_values(series.time_index[start_idx:], np.asarray(preds))


def registry() -> dict[str, Callable[[], Forecaster]]:
    """Mapa nombre -> fábrica perezosa (un modelo nuevo por llamada; deriva de ``_FACTORIES``)."""
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
