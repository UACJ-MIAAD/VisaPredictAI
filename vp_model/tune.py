"""Leakage-free hyperparameter optimization for the GBMs (Épica AK).

Protocol (AK6): the SELECTION region (everything before the 24-month hold-out)
is split into train / val-tuning (24 months) / val-confirm (12 months). Optuna
minimizes a conservative aggregate (mean + std across the group) of the
val-tuning MASE; ACCEPTANCE is decided later on val-confirm by
``confirm_tuning`` (a window neither the tuner nor the incumbent ever saw); the
hold-out is never touched here and stays reserved for the published number.

Objective correctness (AK1): scores are computed ONLY on real F observations
(``dates=raw.index``) and scaled by the seasonal-naive MAE of the raw F series
before the validation start (``metrics.naive_scale_before``) — the same
primitives as ``walkforward.backtest``. The pre-B1 objective scored the months
that ``to_timeseries`` interpolates for training continuity, i.e. it optimized
the prediction of fabricated points.

Group hygiene (AK2): pseudo-replicas of the worldwide cutoff (identical raw
series across countries) are collapsed to one representative BEFORE tuning, so
the TPE stops weighting the worldwide cutoff 3x in the mean+std it minimizes.

Industrial Optuna (AK4): persistent sqlite storage, versioned study names with
``load_if_exists``, TPESampler(multivariate=True, group=True, seed),
MedianPruner fed per-series intermediate scores (series ordered fast -> slow),
and the full trial history dumped to ``reports/campaign/hpo_trials_{study}.csv``
(provenance). Optional per-trial logging to the env-agnostic tracking bridge
(``vp_data.tracking`` -> MLflow via sync_mlflow) with AK5's callback.

Statistical models (ARIMA/ETS/Theta) are NOT tuned here: their AICc
auto-selection IS the tuning. This module covers the GBMs; deep HPO lives in
``experiments/run_global_deep.py`` (AK8).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from vp_model import config, dataset, metrics, models
from vp_model.config import HOLDOUT, MIN_TRAIN, RANDOM_SEED, get_logger
from vp_model.feature_builder import FeatureBuilder

if TYPE_CHECKING:
    import optuna

log = get_logger("tune")

# --- Protocol constants (AK4/AK6) -------------------------------------------
N_TRIALS = 150  # default budget per (model x table x block); pruning makes it cheap
VAL_TUNE = 24  # months: internal validation tail the tuner optimizes on
VAL_CONFIRM = 12  # months: independent confirmation tail (acceptance, AK6)
# Bump when the search space or the objective changes: a new study name keeps
# old sqlite trials from steering the TPE over an incompatible space.
SPACE_VERSION = "v2"
REPORTS = Path(__file__).resolve().parent.parent / "reports"
OPTUNA_DB = REPORTS / "eval" / "optuna.db"


@dataclass(frozen=True)
class TuneResult:
    model: str
    table: str
    block: str
    study_name: str
    best_params: dict
    best_score: float  # conservative val-tuning MASE (mean+std over the group)
    n_trials: int  # cumulative trials in the study (resumes included)
    n_pruned: int
    default_score: float  # same objective with the current catalog defaults


def _group_series(table: str, block: str) -> list[tuple[str, str, str]]:
    """Series of a (table, block) group: replicas collapsed, ordered fast -> slow.

    AK2: worldwide-cutoff pseudo-replicas (identical raw F series across
    countries, e.g. DFF all_chargeability = china = india) enter ONCE — before
    the fix they weighed 3x in the mean+std the TPE minimizes. AK4: the group is
    sorted by series length ascending so the MedianPruner sees the cheap series
    first and can kill hopeless trials early.
    """
    cat = dataset.list_series(table=table, block=block)
    # The structural catalog includes series with zero F observations (e.g.
    # all_chargeability/EB5_HIGHUNEMP/FAD) and short EB stubs — the tuner can
    # only learn from evaluable series (caught live in the AQ campaign).
    ev = dataset.evaluable_series()
    ok = {
        (r.country, r.category, r.table)
        for r in ev.itertuples()
        if r.table == table and (block is None or r.block == block)
    }
    seen: set[tuple] = set()
    entries: list[tuple[str, str, str, int]] = []
    for r in cat.itertuples():
        if (r.country, r.category, r.table) not in ok:
            log.info("skip non-evaluable %s/%s/%s", r.country, r.category, r.table)
            continue
        raw = dataset.load_series(r.country, r.category, r.table)
        sig = (r.category, tuple(raw.index.asi8), tuple(raw.to_numpy().tolist()))
        if sig in seen:
            log.info("dedup replica %s/%s/%s (worldwide cutoff)", r.country, r.category, r.table)
            continue
        seen.add(sig)
        entries.append((r.country, r.category, r.table, len(raw)))
    entries.sort(key=lambda e: e[3])
    return [(c, cc, t) for c, cc, t, _ in entries]


def _build_tuned(model_name: str, params: dict) -> models.Forecaster:
    """Tree model with trial hyperparameters (wrapped in Differenced), seeded.

    AK3: ``random_state`` is pinned for the three GBMs — CatBoost used to run on
    its default (wall-clock) seed, making trials irreproducible.
    """
    from darts.models import CatBoostModel, LightGBMModel, XGBModel

    base = {"xgboost": XGBModel, "lightgbm": LightGBMModel, "catboost": CatBoostModel}[model_name]
    kw = dict(
        lags=params.pop("lags", 24),
        lags_future_covariates=[0],
        output_chunk_length=1,
        random_state=RANDOM_SEED,
        **params,
    )
    if model_name == "lightgbm":
        kw["verbose"] = -1
    return models.Differenced(base(**kw))


def _suggest(trial: optuna.Trial, model_name: str) -> dict:
    """Search spaces (AK3): wider lr/n_estimators + THE small-n regularizers.

    The v1 boxes were saturated (5/6 studies pinned to the lr floor, catboost
    depth=8 at the ceiling) and had no subsampling at all. lr goes log
    3e-3–0.1, n_estimators up to 1500, and every model gets row/column
    subsampling — the regularizers that matter on 60–270-observation windows.
    """
    p: dict[str, float | int] = {"lags": trial.suggest_int("lags", 12, 36, step=6)}
    if model_name == "lightgbm":
        p |= {
            "num_leaves": trial.suggest_int("num_leaves", 7, 63),
            "min_child_samples": trial.suggest_int("min_child_samples", 10, 100),
            "learning_rate": trial.suggest_float("learning_rate", 3e-3, 0.1, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10, log=True),
            "n_estimators": trial.suggest_int("n_estimators", 100, 1500),
            "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
            "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
            "bagging_freq": trial.suggest_int("bagging_freq", 1, 5),
        }
    elif model_name == "xgboost":
        p |= {
            "max_depth": trial.suggest_int("max_depth", 3, 8),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 20),
            "learning_rate": trial.suggest_float("learning_rate", 3e-3, 0.1, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10, log=True),
            "n_estimators": trial.suggest_int("n_estimators", 100, 1500),
            "subsample": trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        }
    elif model_name == "catboost":
        p |= {
            "depth": trial.suggest_int("depth", 3, 10),
            "l2_leaf_reg": trial.suggest_float("l2_leaf_reg", 1, 30, log=True),
            "learning_rate": trial.suggest_float("learning_rate", 3e-3, 0.1, log=True),
            "n_estimators": trial.suggest_int("n_estimators", 100, 1500),
            # CPU CatBoost defaults to MVS bootstrap, which accepts subsample.
            "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        }
    return p


def _val_mase(
    model_name: str,
    country: str,
    category: str,
    table: str,
    params: dict | None,
    window: str = "tuning",
) -> float:
    """MASE on an internal validation tail: ONE fit + 1-step roll, no retrain.

    Cheap, leakage-free proxy of the walk-forward (avoids ~200 refits per trial).
    AK1: scored ONLY on real F observations (``dates=raw.index``) with the
    seasonal-naive scale of the raw F series before the window start — same
    primitives as ``walkforward.backtest``; the interpolated continuity months
    are never targets. AK6: ``window`` selects the tail —

      * ``"tuning"``  — the 24 months BEFORE val-confirm; the tuner's objective.
        The val-confirm tail is sliced away, so Optuna cannot see it.
      * ``"confirm"`` — the final 12 months of the selection region; used by
        ``confirm_tuning`` for the acceptance decision.

    The hold-out (last 24 months of the full series) is excluded in both cases.
    ``params=None`` scores the incumbent: whatever ``build_model(name, table)``
    currently ships for this table (bridge defaults, or a previously ACCEPTED
    winner from ``tuned_params.json``).
    """
    if window not in ("tuning", "confirm"):
        raise ValueError(f"window debe ser 'tuning' o 'confirm', no {window!r}")
    raw = dataset.load_series(country, category, table).astype("float64")
    fe = FeatureBuilder(model_name)
    ts = fe.to_timeseries(raw)
    sel = ts[:-HOLDOUT]  # selection region (hold-out never enters)
    if len(sel) < MIN_TRAIN[table] + VAL_TUNE + VAL_CONFIRM:
        return float("nan")
    confirm_start = len(sel) - VAL_CONFIRM
    if window == "tuning":
        region = sel[:confirm_start]  # val-confirm stays invisible to the tuner (AK6)
        start = confirm_start - VAL_TUNE
    else:
        region = sel
        start = confirm_start
    model = models.build_model(model_name, table=table) if params is None else _build_tuned(model_name, dict(params))
    cov = fe.covariates(ts)  # per-model covariate policy (AD1/AD8)
    extra: dict[str, object] = {"future_covariates": cov} if cov is not None else {}
    model.fit(region[:start], **extra)
    fc = model.historical_forecasts(
        region, start=start, forecast_horizon=1, stride=1, retrain=False, last_points_only=True, verbose=False, **extra
    )
    # AK1: F-only mask + raw-F naive scale (single source, walkforward parity).
    actual = region.slice_intersect(fc)
    scale = metrics.naive_scale_before(raw, region.time_index[start])
    return metrics.compute(actual, fc, region[:start], dates=raw.index, scale=scale)["mase"]


def _mean_sel_mase(
    model_name: str,
    series: list[tuple[str, str, str]],
    params: dict | None,
    trial: optuna.Trial | None = None,
) -> float:
    """Tuner objective: conservative mean+std of val-tuning MASE over the group.

    Never touches the hold-out nor val-confirm. AK4: when ``trial`` is given,
    the CUMULATIVE conservative score is reported after each series (the group
    comes ordered fast -> slow) so the MedianPruner can stop hopeless configs
    before paying for the slow series.
    """
    scores: list[float] = []
    for step, (country, category, table) in enumerate(series):
        s = _val_mase(model_name, country, category, table, params, window="tuning")
        if not np.isnan(s):
            scores.append(s)
        if trial is not None and scores:
            import optuna

            trial.report(float(np.mean(scores) + np.std(scores)), step=step)
            if trial.should_prune():
                raise optuna.TrialPruned()
    # Conservative incumbent (Schneider 2025): mean + dispersion across series.
    return float(np.mean(scores) + np.std(scores)) if scores else float("inf")


def _tracking_callback(model_name: str, table: str, block: str) -> Callable:
    """AK5: Optuna callback that logs EVERY trial to the tracking bridge.

    Uses the stdlib-pure ``vp_data.tracking`` JSONL staging (synced to MLflow by
    ``experiments/sync_mlflow.py``): experiment ``hpo_{model}_{table}``, params =
    trial params + state, metrics = value + best_so_far, tags layer=hpo.
    """
    from vp_data import tracking

    def cb(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        try:
            best: float | None = float(study.best_value)
        except ValueError:  # no completed trial yet
            best = None
        raw_metrics = {"value": trial.value, "best_so_far": best}
        tracking.log_run(
            f"hpo_{model_name}_{table}",
            f"{study.study_name}-t{trial.number:04d}",
            params={"model": model_name, "table": table, "block": block, "state": trial.state.name, **trial.params},
            metrics={k: v for k, v in raw_metrics.items() if v is not None},
            tags={"layer": "hpo", "study": study.study_name},
        )

    return cb


def tune(
    model_name: str,
    table: str = "FAD",
    block: str = "family",
    n_trials: int = N_TRIALS,
    series: list[tuple[str, str, str]] | None = None,
    storage: str | None = None,
    mlflow: bool = False,
) -> TuneResult:
    """Tune ONE shared hyperparameter set for a GBM over the group (leakage-free).

    AK4: the study persists in sqlite (``reports/eval/optuna.db`` by default)
    under a versioned name — re-running RESUMES instead of discarding history —
    with a grouped multivariate TPE and a MedianPruner fed per-series scores.
    The full trial table lands in ``reports/campaign/hpo_trials_{study}.csv``.
    """
    import optuna

    if model_name not in config.DIFFERENCED:
        raise ValueError(f"tune solo cubre GBMs {tuple(config.DIFFERENCED)}; los estadísticos usan Auto*-AICc")
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    grp = series if series is not None else _group_series(table, block)
    default = _mean_sel_mase(model_name, grp, None)

    study_name = f"hpo-{model_name}-{table}-{block}-{SPACE_VERSION}"
    if storage is None:
        OPTUNA_DB.parent.mkdir(parents=True, exist_ok=True)
        storage = f"sqlite:///{OPTUNA_DB}"
    study = optuna.create_study(
        study_name=study_name,
        storage=storage,
        load_if_exists=True,
        direction="minimize",
        sampler=optuna.samplers.TPESampler(multivariate=True, group=True, seed=RANDOM_SEED),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=10, n_warmup_steps=8),
    )

    def objective(trial: optuna.Trial) -> float:
        return _mean_sel_mase(model_name, grp, _suggest(trial, model_name), trial=trial)

    callbacks = [_tracking_callback(model_name, table, block)] if mlflow else []
    study.optimize(objective, n_trials=n_trials, callbacks=callbacks, show_progress_bar=False)

    trials_csv = REPORTS / "campaign" / f"hpo_trials_{study_name}.csv"
    trials_csv.parent.mkdir(parents=True, exist_ok=True)
    study.trials_dataframe().to_csv(trials_csv, index=False)  # provenance (AK4)

    n_pruned = sum(t.state == optuna.trial.TrialState.PRUNED for t in study.trials)
    try:
        best_params, best_value = dict(study.best_params), float(study.best_value)
    except ValueError:  # every trial pruned/failed
        best_params, best_value = {}, float("inf")
    return TuneResult(
        model=model_name,
        table=table,
        block=block,
        study_name=study_name,
        best_params=best_params,
        best_score=best_value,
        n_trials=len(study.trials),
        n_pruned=n_pruned,
        default_score=default,
    )


def rank_check(
    model_name: str,
    table: str = "FAD",
    block: str = "family",
    n_top: int = 10,
    n_series: int = 6,
    storage: str | None = None,
) -> pd.DataFrame:
    """AK9: rank-correlation between the cheap objective and the deployed protocol.

    Loads the persisted study, takes the top ``n_top`` completed trials, and
    re-scores each config with the REAL protocol (``walkforward.backtest`` =
    monthly retrain, expanding window) on the ``n_series`` fastest deduplicated
    series — using the SELECTION MASE only, so the hold-out stays untouched.
    Writes ``reports/eval/hpo_rank_check_{study}.csv`` with per-config rows and
    the Spearman rho between the two rankings. Run at campaign time: it costs
    ~``n_top * n_series`` backtests.
    """
    import optuna
    from scipy import stats

    from vp_model import walkforward

    study_name = f"hpo-{model_name}-{table}-{block}-{SPACE_VERSION}"
    study = optuna.load_study(study_name=study_name, storage=storage or f"sqlite:///{OPTUNA_DB}")
    done = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE and t.value is not None]
    top = sorted(done, key=lambda t: t.value)[:n_top]  # type: ignore[arg-type, return-value]
    grp = _group_series(table, block)[:n_series]

    rows = []
    for t in top:
        scores: list[float] = []
        for country, category, tb in grp:
            try:
                r = walkforward.backtest(
                    model_name, country, category, tb, model=_build_tuned(model_name, dict(t.params))
                )
            except (ValueError, KeyError) as e:
                log.warning("rank_check skip %s/%s: %s", country, category, e)
                continue
            if not np.isnan(r.selection["mase"]):
                scores.append(r.selection["mase"])
        deploy = float(np.mean(scores) + np.std(scores)) if scores else float("nan")
        rows.append(
            {"trial": t.number, "objective": t.value, "deploy_sel": deploy}
            | {f"param_{k}": v for k, v in t.params.items()}
        )
    df = pd.DataFrame(rows)
    valid = df.dropna(subset=["deploy_sel"]) if len(df) else df
    rho = float(stats.spearmanr(valid["objective"], valid["deploy_sel"]).statistic) if len(valid) >= 3 else float("nan")
    df["spearman"] = rho
    out = REPORTS / "eval" / f"hpo_rank_check_{study_name}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    log.info("rank_check %s: spearman=%.3f sobre %d configs -> %s", study_name, rho, len(df), out.name)
    return df


def demo() -> None:
    """Self-check: mini-estudio persistente (3 trials, 3 series) corre y no explota."""
    import tempfile

    grp = _group_series("FAD", "family")[:3]
    with tempfile.TemporaryDirectory() as d:
        res = tune("lightgbm", n_trials=3, series=grp, storage=f"sqlite:///{d}/optuna_demo.db")
    assert res.best_score <= res.default_score * 1.5  # no empeora groseramente
    print(
        f"OK — tune lightgbm (3 trials, 3 series): default={res.default_score:.3f} "
        f"-> mejor={res.best_score:.3f}; pruned={res.n_pruned}; params={res.best_params}"
    )


if __name__ == "__main__":
    demo()
