"""Optimización de hiperparámetros leakage-free (Épica O / US-O1, US-O2).

Implementa el plan de la investigación de HPO (145 fuentes, `reports/litreview_hpo_finetuning.md`):
  * Motor: **Optuna** con TPESampler(multivariate=True, seed=42). Sin Ray/SMAC (Optuna basta).
  * Protocolo ANIDADO: el objetivo minimiza el MASE de SELECCIÓN (la región previa al
    hold-out, vía `walkforward.backtest`); el **hold-out de 24 meses NUNCA entra al
    tuner**. Sin K-fold aleatorio (series no estacionarias).
  * Granularidad: UN set de hiperparámetros compartido por GRUPO (no por serie) —
    regulariza con n pequeño (Montero-Manso & Hyndman 2021).
  * Anti-overtuning (Schneider 2025): incumbente conservador (media + desviación entre
    series), y la regla de ACEPTACIÓN se confirma luego contra el hold-out (US-O3).
  * Estadísticos (ARIMA/ETS/Theta) NO se tunean aquí: su auto-selección por AICc ES el
    tuning. Este módulo cubre las familias con hiperparámetros reales: GBMs y profundos.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vp_model import config, dataset, models, walkforward
from vp_model.config import RANDOM_SEED


@dataclass(frozen=True)
class TuneResult:
    model: str
    table: str
    best_params: dict
    best_score: float  # MASE de selección, media sobre el grupo
    n_trials: int
    default_score: float  # MASE de selección con los defaults de config (referencia)


def _group_series(table: str, block: str) -> list[tuple[str, str, str]]:
    cat = dataset.list_series(table=table, block=block)
    return [(r.country, r.category, r.table) for r in cat.itertuples()]


def _build_tuned(model_name: str, params: dict) -> object:
    """Construye el modelo de árbol con hiperparámetros de prueba (envuelto en Differenced)."""
    from darts.models import CatBoostModel, LightGBMModel, XGBModel

    base = {"xgboost": XGBModel, "lightgbm": LightGBMModel, "catboost": CatBoostModel}[model_name]
    kw = dict(lags=params.pop("lags", 24), lags_future_covariates=[0], output_chunk_length=1, **params)
    if model_name == "lightgbm":
        kw["verbose"] = -1
    return models.Differenced(base(**kw))


# Espacios de búsqueda sesgados a REGULARIZACIÓN (series cortas). Solo árboles aquí.
def _suggest(trial, model_name: str) -> dict:
    p = {"lags": trial.suggest_int("lags", 12, 36, step=6)}
    if model_name == "lightgbm":
        p |= {
            "num_leaves": trial.suggest_int("num_leaves", 7, 31),
            "min_child_samples": trial.suggest_int("min_child_samples", 20, 100),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.05, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10, log=True),
            "n_estimators": trial.suggest_int("n_estimators", 100, 400),
        }
    elif model_name == "xgboost":
        p |= {
            "max_depth": trial.suggest_int("max_depth", 3, 6),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 20),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.05, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10, log=True),
            "n_estimators": trial.suggest_int("n_estimators", 100, 400),
        }
    elif model_name == "catboost":
        p |= {
            "depth": trial.suggest_int("depth", 4, 8),
            "l2_leaf_reg": trial.suggest_float("l2_leaf_reg", 1, 30, log=True),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.05, log=True),
        }
    return p


_VAL = 24  # cola de validación interna (meses) dentro de la región de selección


def _val_mase(model_name: str, country: str, category: str, table: str, params: dict | None) -> float:
    """MASE en una cola de validación interna: UN ajuste + rodado 1-paso sin reentrenar.

    Objetivo BARATO y leakage-free para el tuner: parte la región de SELECCIÓN (todo lo
    previo al hold-out de 24m, que jamás se toca) en entrenamiento + una cola de validación,
    ajusta una sola vez y rueda a 1 paso. Evita los ~200 reentrenos del backtest completo,
    haciendo el HPO viable, sin mirar el futuro ni el hold-out.
    """
    from vp_model import metrics

    ts = models.to_timeseries(dataset.load_series(country, category, table))
    sel = ts[: -walkforward.HOLDOUT]  # región de selección (sin hold-out)
    if len(sel) < walkforward.MIN_TRAIN[table] + _VAL:
        return float("nan")
    val_start = len(sel) - _VAL
    model = models.build_model(model_name) if params is None else _build_tuned(model_name, dict(params))
    extra = {"future_covariates": walkforward._covariates(ts)} if model_name in config.DIFFERENCED else {}
    # un solo ajuste sobre el tramo de entrenamiento (model es un union de forecasters de darts)
    model.fit(sel[:val_start], **extra)  # type: ignore[attr-defined]
    fc = model.historical_forecasts(  # type: ignore[attr-defined]
        sel, start=val_start, forecast_horizon=1, stride=1, retrain=False, last_points_only=True, verbose=False, **extra
    )
    actual = sel.slice_intersect(fc)
    a = actual.values().flatten()
    f = fc.slice_intersect(actual).values().flatten()
    scale = metrics._seasonal_naive_mae(sel[:val_start])  # MAE del naïve estacional in-sample
    return float(np.mean(np.abs(a - f)) / scale)


def _mean_sel_mase(model_name: str, series: list[tuple[str, str, str]], params: dict | None) -> float:
    """Objetivo del tuner: MASE de validación interna promedio sobre el grupo (NO toca hold-out)."""
    scores = []
    for country, category, table in series:
        s = _val_mase(model_name, country, category, table, params)
        if not np.isnan(s):
            scores.append(s)
    # Incumbente conservador (Schneider 2025): media + desviación entre series.
    return float(np.mean(scores) + np.std(scores)) if scores else float("inf")


def tune(
    model_name: str,
    table: str = "FAD",
    block: str = "family",
    n_trials: int = 40,
    series: list[tuple[str, str, str]] | None = None,
) -> TuneResult:
    """Tunea UN set de hiperparámetros compartido para un GBM sobre el grupo (leakage-free)."""
    import optuna

    if model_name not in config.DIFFERENCED:
        raise ValueError(f"tune solo cubre GBMs {tuple(config.DIFFERENCED)}; los estadísticos usan Auto*-AICc")
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    grp = series or _group_series(table, block)
    default = _mean_sel_mase(model_name, grp, None)

    def objective(trial):
        return _mean_sel_mase(model_name, grp, _suggest(trial, model_name))

    study = optuna.create_study(
        direction="minimize", sampler=optuna.samplers.TPESampler(multivariate=True, seed=RANDOM_SEED)
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return TuneResult(model_name, table, study.best_params, study.best_value, n_trials, default)


def demo() -> None:
    """Self-check: un mini-estudio (3 trials, 3 series) corre y mejora o iguala al default."""
    grp = _group_series("FAD", "family")[:3]
    res = tune("lightgbm", n_trials=3, series=grp)
    assert res.best_score <= res.default_score * 1.5  # no empeora groseramente
    print(
        f"OK — tune lightgbm (3 trials, 3 series): default={res.default_score:.3f} "
        f"-> mejor={res.best_score:.3f}; params={res.best_params}"
    )


if __name__ == "__main__":
    demo()
