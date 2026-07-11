"""Épica AK — contrato del tuner: máscara F en el objetivo (AK1), dedup de
pseudo-réplicas (AK2), espacios con regularizadores (AK3), Optuna persistente con
pruning y procedencia (AK4), tracking por trial (AK5), split de confirmación
independiente (AK6), flujo candidato->aceptación (AK6/AK7), HPO deep (AK8) y
rank-check objetivo<->despliegue (AK9). Todo con series sintéticas/monkeypatch —
no requiere el almacén DuckDB.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("darts")
pytest.importorskip("optuna")

import optuna  # noqa: E402
from darts import TimeSeries  # noqa: E402

from vp_model import config, confirm_tuning, metrics, run_tuning, tune  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- helpers
def _linear_series(n: int = 160, gaps: tuple[int, ...] = (), slope: float = 30.0) -> pd.Series:
    """Serie F sintética mensual, lineal (naïve estacional con escala exacta)."""
    idx = pd.date_range("2005-01-01", periods=n, freq="MS")
    s = pd.Series(10_000.0 + slope * np.arange(n, dtype="float64"), index=idx, name="synthetic")
    return s.drop(s.index[list(gaps)]) if gaps else s


class _Canned:
    """Forecaster enlatado: perfecto en fechas F reales, basura en las interpoladas."""

    def __init__(self, raw: pd.Series, err_f: float = 0.0, err_interp: float = 1e6) -> None:
        self.raw, self.err_f, self.err_interp = raw, err_f, err_interp

    def fit(self, series, **kwargs):  # noqa: ANN001, ANN003
        return self

    def predict(self, n, **kwargs):  # noqa: ANN001, ANN003 — no usado por _val_mase
        raise NotImplementedError

    def historical_forecasts(self, series, *, start, **kwargs):  # noqa: ANN001, ANN003
        fc = series[start:]
        vals = fc.values().copy()
        interp = ~fc.time_index.isin(self.raw.index)
        vals[interp] += self.err_interp
        vals[~interp] += self.err_f
        return TimeSeries.from_times_and_values(fc.time_index, vals)


# --------------------------------------------------------------------------- AK1
def test_objective_ignores_interpolated_months(monkeypatch):
    """AK1: los meses que to_timeseries interpola NO se puntúan en el objetivo."""
    # huecos (<= MAX_INTERPOLABLE_GAP) dentro de la ventana val-tuning: sel = 136
    # meses de calendario, val-tuning = posiciones 100..123 -> huecos en 105/106/115.
    raw = _linear_series(160, gaps=(105, 106, 115))
    monkeypatch.setattr(tune.dataset, "load_series", lambda *a, **k: raw)
    monkeypatch.setattr(tune, "_build_tuned", lambda name, params: _Canned(raw))
    score = tune._val_mase("lightgbm", "x", "F1", "FAD", {"any": 1}, window="tuning")
    # error 1e6 en los meses interpolados: si se puntuaran, el MASE explota; enmascarados -> 0.
    assert score == pytest.approx(0.0, abs=1e-9)


def test_objective_scores_real_f_dates_with_raw_scale(monkeypatch):
    """AK1: el error en fechas F reales SÍ se puntúa, escalado por naive_scale_before(raw)."""
    raw = _linear_series(160, gaps=(105, 106))
    monkeypatch.setattr(tune.dataset, "load_series", lambda *a, **k: raw)
    monkeypatch.setattr(tune, "_build_tuned", lambda name, params: _Canned(raw, err_f=360.0, err_interp=0.0))
    score = tune._val_mase("lightgbm", "x", "F1", "FAD", {"any": 1}, window="tuning")
    # la fecha de arranque de la ventana val-tuning en el calendario densificado:
    n_sel = 160 - config.HOLDOUT
    start_date = pd.date_range("2005-01-01", periods=160, freq="MS")[n_sel - tune.VAL_CONFIRM - tune.VAL_TUNE]
    expected = 360.0 / metrics.naive_scale_before(raw, start_date)
    assert score == pytest.approx(expected, rel=1e-9)


# --------------------------------------------------------------------------- AK6 (ventanas)
def test_tuning_window_never_sees_confirm_nor_holdout(monkeypatch):
    """AK6: el objetivo del tuner corta val-confirm; confirm corta el hold-out."""
    raw = _linear_series(160)
    seen: dict[str, pd.Timestamp] = {}

    class Spy(_Canned):
        def historical_forecasts(self, series, *, start, **kwargs):  # noqa: ANN001, ANN003
            seen["end"] = series.time_index[-1]
            seen["start"] = series.time_index[start]
            return super().historical_forecasts(series, start=start, **kwargs)

    monkeypatch.setattr(tune.dataset, "load_series", lambda *a, **k: raw)
    monkeypatch.setattr(tune, "_build_tuned", lambda name, params: Spy(raw))

    n_sel = 160 - config.HOLDOUT  # 136 meses de selección
    tune._val_mase("lightgbm", "x", "F1", "FAD", {"any": 1}, window="tuning")
    assert seen["end"] == raw.index[n_sel - tune.VAL_CONFIRM - 1]  # val-confirm invisible
    assert seen["start"] == raw.index[n_sel - tune.VAL_CONFIRM - tune.VAL_TUNE]

    tune._val_mase("lightgbm", "x", "F1", "FAD", {"any": 1}, window="confirm")
    assert seen["end"] == raw.index[n_sel - 1]  # el hold-out jamás entra
    assert seen["start"] == raw.index[n_sel - tune.VAL_CONFIRM]

    with pytest.raises(ValueError):
        tune._val_mase("lightgbm", "x", "F1", "FAD", None, window="holdout")


# --------------------------------------------------------------------------- AK2
def test_group_series_dedups_replicas_and_orders_fast_to_slow(monkeypatch):
    """AK2/AK4: réplicas del corte mundial colapsan a UNA; orden corta->larga."""
    long = _linear_series(200)
    short = _linear_series(120, slope=25.0)
    data = {"india": long, "china": long.copy(), "mexico": short}
    cat = pd.DataFrame({"country": ["india", "china", "mexico"], "category": ["F1"] * 3, "table": ["FAD"] * 3})
    monkeypatch.setattr(tune.dataset, "list_series", lambda **k: cat)
    monkeypatch.setattr(tune.dataset, "load_series", lambda c, cc, t, **k: data[c])
    grp = tune._group_series("FAD", "family")
    assert len(grp) == 2  # india/china idénticas -> una representante
    assert grp[0][0] == "mexico"  # la corta (rápida) va primero para el pruner
    assert grp[1][0] == "india"  # primera aparición de la clase de réplica


# --------------------------------------------------------------------------- AK3
@pytest.mark.parametrize(
    ("model", "required"),
    [
        ("lightgbm", {"feature_fraction", "bagging_fraction", "bagging_freq", "n_estimators", "learning_rate"}),
        ("xgboost", {"subsample", "colsample_bytree", "n_estimators", "learning_rate"}),
        ("catboost", {"subsample", "n_estimators", "depth", "learning_rate"}),
    ],
)
def test_search_spaces_have_regularizers_and_wider_boxes(model, required):
    """AK3: subsampling presente y cajas ampliadas (lr 3e-3–0.1, árboles hasta 1500)."""
    study = optuna.create_study(sampler=optuna.samplers.RandomSampler(seed=0))
    trial = study.ask()
    params = tune._suggest(trial, model)
    assert required <= set(params)
    dists = trial.distributions
    lr = dists["learning_rate"]
    assert lr.low == pytest.approx(3e-3) and lr.high == pytest.approx(0.1) and lr.log
    assert dists["n_estimators"].high == 1500
    if model == "catboost":
        assert dists["depth"].low == 3 and dists["depth"].high == 10


def test_build_tuned_pins_random_state():
    """AK3: los tres GBM quedan sembrados (CatBoost quedaba con seed default)."""
    for model in ("lightgbm", "xgboost", "catboost"):
        m = tune._build_tuned(model, {"n_estimators": 10})
        inner = m.base.model  # type: ignore[attr-defined]  # Differenced -> darts -> estimador
        assert inner.get_params()["random_state"] == config.RANDOM_SEED, model


# --------------------------------------------------------------------------- AK4 + AK5
def _fake_group(monkeypatch, n_series: int = 2) -> list[tuple[str, str, str]]:
    """Grupo sintético con ruido (para que el GBM tenga algo que aprender)."""
    rng = np.random.default_rng(7)
    data = {}
    for i in range(n_series):
        s = _linear_series(150, slope=28.0 + i)
        data[f"c{i}"] = s + rng.normal(0, 15, len(s))
    monkeypatch.setattr(tune.dataset, "load_series", lambda c, cc, t, **k: data[c])
    return [(f"c{i}", "F1", "FAD") for i in range(n_series)]


def test_tune_smoke_persistent_storage_pruner_and_tracking(monkeypatch, tmp_path):
    """AK4/AK5: estudio persistente reanudable + CSV de trials + JSONL de tracking."""
    from vp_data import tracking

    grp = _fake_group(monkeypatch)
    monkeypatch.setattr(tune, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(tracking, "STAGING", tmp_path / "staging")
    storage = f"sqlite:///{tmp_path}/optuna.db"

    res = tune.tune("lightgbm", table="FAD", block="family", n_trials=3, series=grp, storage=storage, mlflow=True)
    assert res.n_trials == 3 and np.isfinite(res.default_score)
    assert res.study_name == f"hpo-lightgbm-FAD-family-{tune.SPACE_VERSION}"
    assert set(res.best_params) >= {"lags", "learning_rate", "n_estimators"}

    # persistencia (AK4): reabrir el estudio y REANUDAR sin descartar historial
    # (E3: vía el context manager — un load_study con url cruda deja el engine vivo)
    with tune._sqlite_storage(storage) as st:
        study = optuna.load_study(study_name=res.study_name, storage=st)
        assert len(study.trials) == 3
        assert isinstance(study.pruner, optuna.pruners.MedianPruner)
    res2 = tune.tune("lightgbm", n_trials=1, series=grp, storage=storage)
    assert res2.n_trials == 4  # acumula, no reinicia

    # procedencia (AK4): trials_dataframe -> reports/campaign/hpo_trials_{study}.csv
    csv = tmp_path / "reports" / "campaign" / f"hpo_trials_{res.study_name}.csv"
    assert csv.exists() and len(pd.read_csv(csv)) == 4

    # tracking por trial (AK5): un record por trial con tags layer=hpo
    jsonl = tmp_path / "staging" / "hpo_lightgbm_FAD.jsonl"
    recs = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert len(recs) == 3  # la reanudación corrió sin mlflow
    assert all(r["tags"]["layer"] == "hpo" for r in recs)
    assert all("value" in r["metrics"] for r in recs if r["params"]["state"] == "COMPLETE")


def test_run_tuning_writes_candidates_and_merges(monkeypatch, tmp_path):
    """AK6/AK7: run_tuning escribe CANDIDATOS (improved=False) y no clobberea entradas ajenas."""
    grp = _fake_group(monkeypatch)
    monkeypatch.setattr(tune, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(tune, "_group_series", lambda table, block: grp)
    out = tmp_path / "tuned_params.json"
    out.write_text(json.dumps({"catboost": {"FAD_family": {"best_params": {}, "improved": True}}}))

    run_tuning.main(
        [
            "--models",
            "lightgbm",
            "--groups",
            "FAD_family",
            "--n-trials",
            "2",
            "--out",
            str(out),
            "--storage",
            f"sqlite:///{tmp_path}/optuna.db",
        ]
    )
    data = json.loads(out.read_text())
    entry = data["lightgbm"]["FAD_family"]
    assert entry["improved"] is False  # candidato: la aceptación es de confirm_tuning
    assert isinstance(entry["improved_tuning_val"], bool)
    assert entry["n_trials"] == 2 and "study_name" in entry and "n_pruned" in entry
    assert data["catboost"]["FAD_family"]["improved"] is True  # merge, no clobber


# --------------------------------------------------------------------------- #20 (AK9)
def test_select_by_deploy_reranks_best_params_by_deploy_score(monkeypatch, tmp_path):
    """FIX #20/AK9: dentro del top-K, la RECETA desplegada (tuned_params.json best_params,
    que lee models._tree_params) se re-elige por el deploy-score del rank-check, no por el
    objetivo barato de Optuna. FALLA antes del fix (sin --select-by-deploy / sin
    tune.deploy_winner_params) y PASA después."""
    grp = _fake_group(monkeypatch)
    monkeypatch.setattr(tune, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(tune, "_group_series", lambda table, block: grp)
    storage = f"sqlite:///{tmp_path}/optuna.db"
    out = tmp_path / "tuned_params.json"

    # 1) tune -> estudio persistido + candidato (best_params = ganador por OBJETIVO)
    run_tuning.main(
        ["--models", "lightgbm", "--groups", "FAD_family", "--n-trials", "5", "--out", str(out), "--storage", storage]
    )
    with tune._sqlite_storage(storage) as st:
        study = optuna.load_study(study_name=f"hpo-lightgbm-FAD-family-{tune.SPACE_VERSION}", storage=st)
        done = sorted([t for t in study.trials if t.value is not None], key=lambda t: t.value)
    obj_winner, deploy_winner = done[0], done[-1]  # peor-por-objetivo -> mejor-por-deploy
    assert obj_winner.number != deploy_winner.number
    assert json.loads(out.read_text())["lightgbm"]["FAD_family"]["best_params"] == dict(obj_winner.params)

    # 2) rank-check ya pagado: deploy_sel minimizado en el trial PEOR-por-objetivo (desajuste AK9)
    ev = tmp_path / "reports" / "eval"
    ev.mkdir(parents=True, exist_ok=True)
    rows = [
        {"trial": t.number, "objective": t.value, "deploy_sel": 0.1 if t.number == deploy_winner.number else 0.9}
        for t in done
    ]
    pd.DataFrame(rows).to_csv(ev / f"hpo_rank_check_hpo-lightgbm-FAD-family-{tune.SPACE_VERSION}.csv", index=False)

    # 3) select-by-deploy reescribe best_params al ganador por deploy (región de selección, sin leakage)
    run_tuning.main(
        [
            "--models",
            "lightgbm",
            "--groups",
            "FAD_family",
            "--select-by-deploy",
            "--out",
            str(out),
            "--storage",
            storage,
        ]
    )
    entry = json.loads(out.read_text())["lightgbm"]["FAD_family"]
    assert entry["best_params"] == dict(deploy_winner.params)  # ganador por deploy enrutado
    assert entry["best_params"] != dict(obj_winner.params)
    assert entry["best_params_source"] == "deploy_rank"
    assert entry["objective_best_params"] == dict(obj_winner.params)  # procedencia retenida


def test_run_tuning_has_employment_groups():
    """AK7: los grupos EB existen como destino de tuning (dejan de correr con defaults)."""
    assert {"FAD_employment", "DFF_employment"} <= set(run_tuning.GROUPS)


# --------------------------------------------------------------------------- AK6 (confirmación)
def test_confirm_decides_on_val_confirm_and_reports_median(monkeypatch, tmp_path):
    """AK6: la aceptación se decide en val-confirm; media+mediana+% siempre juntas."""
    tuned_path = tmp_path / "tuned_params.json"
    tuned_path.write_text(
        json.dumps(
            {
                "lightgbm": {"FAD_family": {"best_params": {"lags": 12}, "improved": False}},
                "xgboost": {"FAD_family": {"best_params": {"lags": 12}, "improved": False}},
            }
        )
    )
    grp = [("a", "F1", "FAD"), ("b", "F2A", "FAD"), ("c", "F3", "FAD")]
    monkeypatch.setattr(tune, "_group_series", lambda table, block: grp)
    monkeypatch.setattr(confirm_tuning, "REPORTS", tmp_path / "reports")
    (tmp_path / "reports" / "eval").mkdir(parents=True)

    # lightgbm mejora TODAS las series; xgboost gana en media pero pierde 2/3 series
    canned = {
        ("lightgbm", None): 1.0,
        ("lightgbm", "t"): 0.8,
        ("xgboost", None): 1.0,
    }
    xgb_tuned = {"a": 0.1, "b": 1.2, "c": 1.3}  # media 0.867 < 1.0 pero mediana/mayoría peor

    def fake_val_mase(model, country, category, table, params, window="tuning"):
        assert window == "confirm"  # la decisión SOLO mira val-confirm
        if model == "xgboost" and params is not None:
            return xgb_tuned[country]
        return canned[(model, None if params is None else "t")]

    monkeypatch.setattr(tune, "_val_mase", fake_val_mase)

    df = confirm_tuning.decide(tuned_path)
    s = confirm_tuning.summary(df)
    lgb = s[s.model == "lightgbm"].iloc[0]
    xgb = s[s.model == "xgboost"].iloc[0]
    assert bool(lgb.acepta) and lgb.pct_improve == 1.0 and bool(lgb.median_agrees)
    assert bool(xgb.acepta)  # la media engaña...
    # ...y el reporte lo delata (summary redondea a 4 decimales)
    assert xgb.pct_improve == pytest.approx(1 / 3, abs=1e-3) and not bool(xgb.median_agrees)

    confirm_tuning.apply_acceptance(s, tuned_path)
    data = json.loads(tuned_path.read_text())
    assert data["lightgbm"]["FAD_family"]["improved"] is True
    assert data["lightgbm"]["FAD_family"]["confirm"]["pct_improve"] == 1.0
    assert (tmp_path / "reports" / "eval" / "tuning_confirmation.csv").exists()


def test_holdout_report_uses_bridge_default_same_vintage(monkeypatch, tmp_path):
    """AK6: el reporte de hold-out re-corre AMBAS variantes en la misma sesión
    (default = build_model sin tabla, el puente) y jamás decide nada."""
    tuned_path = tmp_path / "tuned_params.json"
    tuned_path.write_text(json.dumps({"lightgbm": {"FAD_family": {"best_params": {"lags": 12}, "improved": True}}}))
    grp = [("a", "F1", "FAD")]
    monkeypatch.setattr(tune, "_group_series", lambda table, block: grp)
    monkeypatch.setattr(confirm_tuning, "REPORTS", tmp_path / "reports")
    (tmp_path / "reports" / "eval").mkdir(parents=True)

    calls: list[object] = []
    monkeypatch.setattr(tune, "_build_tuned", lambda name, params: "TUNED")
    monkeypatch.setattr(confirm_tuning.models, "build_model", lambda name, table=None: ("BRIDGE", table))

    class R:
        holdout = {"mase": 0.5}

    def fake_backtest(model_name, country, category, table, model=None):
        calls.append(model)
        return R()

    monkeypatch.setattr(confirm_tuning.walkforward, "backtest", fake_backtest)
    df = confirm_tuning.holdout_report(tuned_path)
    assert calls == ["TUNED", ("BRIDGE", None)]  # default puente, mismo vintage, sin tabla
    assert {"tuned_hold_mase", "default_hold_mase", "accepted"} <= set(df.columns)


# --------------------------------------------------------------------------- AK9
def test_rank_check_spearman_between_objective_and_deployment(monkeypatch, tmp_path):
    """AK9: rank-correlation entre el objetivo barato y el walk-forward con retrain."""
    grp = _fake_group(monkeypatch)
    monkeypatch.setattr(tune, "REPORTS", tmp_path / "reports")
    storage = f"sqlite:///{tmp_path}/optuna.db"
    res = tune.tune("lightgbm", n_trials=3, series=grp, storage=storage)

    class R:
        def __init__(self, mase):
            self.selection = {"mase": mase}

    # despliegue = monótono en n_estimators -> correlación determinista y barata
    def fake_backtest(model_name, country, category, table, model=None):
        return R(model.base.model.get_params()["n_estimators"] / 1000)  # type: ignore[union-attr]

    monkeypatch.setattr(tune, "_group_series", lambda table, block: grp)
    import vp_model.walkforward as wf

    monkeypatch.setattr(wf, "backtest", fake_backtest)
    df = tune.rank_check("lightgbm", n_top=3, n_series=2, storage=storage)
    assert {"trial", "objective", "deploy_sel", "spearman"} <= set(df.columns)
    assert len(df) == 3
    assert (tmp_path / "reports" / "eval" / f"hpo_rank_check_{res.study_name}.csv").exists()
    assert df["spearman"].abs().le(1.0).all() or df["spearman"].isna().all()


# --------------------------------------------------------------------------- AK8
def _load_deep():
    spec = importlib.util.spec_from_file_location("rgd_ak", ROOT / "experiments" / "run_global_deep.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_deep_constants_imported_from_config(monkeypatch):
    """AK8e: HOLDOUT/MAX_GAP/BASE vienen de config (mueren los hardcodes)."""
    deep = _load_deep()
    from vp_data.config import BASE_EPOCH

    assert deep.HOLDOUT is config.HOLDOUT
    assert deep.MAX_GAP == config.MAX_INTERPOLABLE_GAP
    assert deep.BASE == pd.Timestamp(BASE_EPOCH)


def test_deep_accelerator_env_override(monkeypatch):
    """AK8d: VP_DEEP_ACCEL fuerza el accelerator (fallback CPU documentado)."""
    deep = _load_deep()
    monkeypatch.setenv("VP_DEEP_ACCEL", "cpu")
    assert deep._accelerator() == "cpu"
    monkeypatch.delenv("VP_DEEP_ACCEL")
    assert deep._accelerator() in ("mps", "cpu")


def test_deep_auto_spaces_have_architecture_and_early_stop(monkeypatch):
    """AK8a/b: arquitectura en el espacio + early stopping como presupuesto real."""
    deep = _load_deep()
    monkeypatch.setenv("VP_DEEP_ACCEL", "cpu")
    assert set(deep._AUTO_CONFIGS) == {"AutoBiTCN", "AutoNHITS", "AutoTiDE", "AutoPatchTST"}

    def ask():  # cada Auto* corre su PROPIO estudio; espacios con el mismo nombre no chocan
        return optuna.create_study(sampler=optuna.samplers.RandomSampler(seed=0)).ask()

    cfg = deep._cfg_bitcn(ask())
    assert {"hidden_size", "dropout"} <= set(cfg)  # kernel_size is not a BiTCN param in this neuralforecast
    assert cfg["hidden_size"] in (8, 16, 32)
    assert cfg["max_steps"] == deep.MAX_STEPS_HPO == 2000
    assert cfg["early_stop_patience_steps"] == 10 and cfg["val_check_steps"] == 25

    cfg_n = deep._cfg_nhits(ask())
    assert isinstance(cfg_n["n_pool_kernel_size"], tuple) and isinstance(
        cfg_n["n_freq_downsample"], tuple
    )  # tuples: NF MockTrial fix
    assert {"hidden_size", "decoder_output_dim"} <= set(deep._cfg_tide(ask()))
    assert {"n_heads", "patch_len"} <= set(deep._cfg_patchtst(ask()))


def test_deep_build_from_config_keeps_auto_column_name(monkeypatch, tmp_path):
    """AK8c: el re-entreno del ganador conserva la columna Auto* (contrato aggregate/key_facts)."""
    import types

    deep = _load_deep()
    monkeypatch.setenv("VP_DEEP_ACCEL", "cpu")
    # neuralforecast vive en el venv ante_nf, no aquí: se inyecta un stub que captura kwargs.
    fake = types.ModuleType("neuralforecast.models")
    for cls_name in ("BiTCN", "NHITS", "TiDE", "PatchTST"):
        fake.__dict__[cls_name] = type(cls_name, (), {"__init__": lambda self, **kw: setattr(self, "kw", kw)})
    monkeypatch.setitem(sys.modules, "neuralforecast", types.ModuleType("neuralforecast"))
    monkeypatch.setitem(sys.modules, "neuralforecast.models", fake)

    cfg = {"input_size": 24, "learning_rate": 1e-3, "scaler_type": "standard", "hidden_size": 16, "max_steps": 2000}
    (tmp_path / "hpo_deep_best_FAD_AutoBiTCN.json").write_text(json.dumps(cfg))
    builders = deep._build_from_config(["BiTCN"], str(tmp_path / "hpo_deep_best_FAD_Auto{model}.json"), seed=3)
    assert list(builders) == ["AutoBiTCN"]  # la COLUMNA de salida conserva el nombre Auto*
    model = builders["AutoBiTCN"]()
    assert type(model).__name__ == "BiTCN"
    assert model.kw["random_seed"] == 3 and model.kw["hidden_size"] == 16
    assert model.kw["early_stop_patience_steps"] == 10 and model.kw["logger"] is False
    # el ganador ausente se omite con aviso, sin reventar la corrida
    assert deep._build_from_config(["TiDE"], str(tmp_path / "hpo_deep_best_FAD_Auto{model}.json"), seed=3) == {}
