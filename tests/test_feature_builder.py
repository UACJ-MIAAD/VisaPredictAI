"""Tests del motor de FE (plan FE/cleaning, épica AD).

Cubre lo que la auditoría encontró SIN probar: valores de la codificación cíclica
(no solo el shape), round-trip exacto de la diferenciación, política de covariables
por modelo, escalado sin leakage, y el ancla de las constantes re-declaradas en los
scripts de venvs aislados (run_global_deep / aws_gpu) contra config.

Runs with pytest *or* as a plain script (no pytest required):
    ante/bin/python tests/test_feature_builder.py
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# El job de CI de la capa de datos no instala el extra [model]; igual que el resto
# de tests de modelado, este módulo se salta limpio si darts no está disponible.
pytest.importorskip("darts")

from vp_model import preprocess  # noqa: E402
from vp_model.config import (  # noqa: E402
    COVARIATES,
    DIFFERENCED,
    MASK_COVARIATES,
    MAX_INTERPOLABLE_GAP,
    NN_DIFFERENCED,
    TREE_FUTURE_COV_LAGS,
)
from vp_model.config import HOLDOUT as CFG_HOLDOUT  # noqa: E402
from vp_model.feature_builder import FE_DECISIONS, FeatureBuilder  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_cyclic_encoding_values_not_just_shape():
    # 12 meses consecutivos: las cuerdas (sin,cos) entre meses ADYACENTES son todas
    # iguales — incluida diciembre->enero, el punto entero de la codificación.
    idx = pd.date_range("2025-01-01", periods=13, freq="MS")
    cal = preprocess.calendar_features(idx)
    pts = cal[["month_sin", "month_cos"]].to_numpy()
    chords = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    assert np.allclose(chords, chords[0]), "la distancia dic<->ene difiere del resto de meses"
    # Octubre es el origen del año fiscal: fiscal_pos=0 -> sin=0, cos=1.
    oct_row = cal[cal.index.month == 10].iloc[0]
    assert abs(oct_row["fiscal_sin"]) < 1e-12 and abs(oct_row["fiscal_cos"] - 1.0) < 1e-12


def test_difference_round_trip_exact():
    rng = np.random.default_rng(7)
    s = pd.Series(rng.normal(0, 30, 90).cumsum() + 10_000, index=pd.date_range("2015-01-01", periods=90, freq="MS"))
    d = preprocess.difference(s)
    back = preprocess.undifference(d.iloc[1:], last_level=float(s.iloc[0]))
    assert np.allclose(back.to_numpy(), s.iloc[1:].to_numpy()), "round-trip diff/undiff no es exacto"


def test_covariate_policy_per_model():
    # Covariables: SOLO los árboles (AD8). OJO: "diferenciado" ya NO implica
    # "lleva covariables" — las NN también predicen Δy (AJ1, NN_DIFFERENCED)
    # pero van conscientemente sin calendario.
    assert set(COVARIATES) == set(DIFFERENCED), "hoy SOLO los árboles llevan covariables (AD8)"
    assert set(COVARIATES).isdisjoint(NN_DIFFERENCED), "las NN diferenciadas van sin covariables"
    assert FeatureBuilder("xgboost").covariate_cols == COVARIATES["xgboost"]
    # F1: las máscaras MNAR siguen la MISMA política (solo los GBM diferenciados) y sus
    # retardos son estrictamente negativos (el régimen del mes objetivo no se conoce
    # antes de publicarse el boletín: retardo 0 sería fuga).
    assert set(MASK_COVARIATES) == set(DIFFERENCED), "las máscaras MNAR van solo a los GBM (F1)"
    assert FeatureBuilder("xgboost").mask_covariate_cols == MASK_COVARIATES["xgboost"]
    for col in MASK_COVARIATES["xgboost"]:
        assert all(lag <= -1 for lag in TREE_FUTURE_COV_LAGS[col]), f"máscara {col} debe ir a retardo <= -1"
    for col in COVARIATES["xgboost"]:
        assert TREE_FUTURE_COV_LAGS[col] == [0], f"calendario {col} va a retardo 0 (determinista)"
    for plain in ("ets", "theta", "rlinear", "tft"):
        assert FeatureBuilder(plain).covariate_cols == (), f"{plain} debe ir sin covariables (política AD8)"
        assert FeatureBuilder(plain).mask_covariate_cols == (), f"{plain} debe ir sin máscaras (política F1)"


def test_realized_lineage_complete():
    r = FeatureBuilder("lightgbm").realized()
    assert r["differenced"] and r["differenced_family"] == "trees" and not r["scaled"] and r["lags"] == 24
    # Actualización DELIBERADA del golden (cambio de contrato US-F1, 12-jul-2026): la
    # rejilla de modelado dejó de usar el cap de interpolación (era bidireccional y
    # fugaba el bracket futuro del hueco) — el linaje ahora declara la política causal
    # y las máscaras MNAR. MAX_INTERPOLABLE_GAP sigue vivo en la rejilla DESCRIPTIVA
    # del EDA (to_regular_monthly / series_characterization) y anclado abajo en
    # test_isolated_venv_scripts_pinned_to_config.
    assert r["gap_policy"] == "locf_causal" and r["fe_version"].startswith("2.")
    assert r["mask_covariates"] == ["observed", "months_since_obs"]
    ids = [d["id"] for d in FE_DECISIONS]
    assert len(ids) == len(set(ids)) and "differencing_trees" in ids
    assert "missingness_mask_covariates" in ids  # F1: la decisión quedó registrada


def test_realized_lineage_nn_differenced():
    # Leftover Oleada 1: el linaje reportaba differenced=False para las NN locales
    # porque no conocía config.NN_DIFFERENCED (AJ1). Ahora el flag entra al linaje.
    for nn in sorted(NN_DIFFERENCED):
        r = FeatureBuilder(nn).realized()
        assert r["differenced"], f"{nn} predice Δy (AJ1) y el linaje debe decirlo"
        assert r["differenced_family"] == "nn"
    ets = FeatureBuilder("ets").realized()
    assert not ets["differenced"] and ets["differenced_family"] is None


def test_scaler_fit_window_only():
    # El scaler ajustado en la ventana no puede conocer el máximo del futuro.
    from vp_model import models

    trend = pd.Series(np.linspace(1_000, 20_000, 120), index=pd.date_range("2010-01-01", periods=120, freq="MS"))
    ts = models.to_timeseries(trend)
    sc = FeatureBuilder("dlinear").fit_scaler(ts, 60)
    assert sc is not None
    z = sc.transform(ts)
    assert abs(float(z[:60].values().max()) - 1.0) < 1e-9, "el máx de la ventana de train define el rango"
    assert float(z.values().max()) > 1.5, "la serie completa debe salirse del rango: el scaler no vio el futuro"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_isolated_venv_scripts_pinned_to_config():
    # run_global_deep/train_gpu re-declaran constantes porque sus venvs no tienen
    # vp_model; este test (que corre en ante) las ANCLA a config para que no deriven.
    from vp_data.config import BASE_EPOCH

    deep = _load_module(ROOT / "experiments" / "run_global_deep.py", "rgd_pin")
    assert deep.HOLDOUT == CFG_HOLDOUT and deep.MAX_GAP == MAX_INTERPOLABLE_GAP
    assert deep.BASE == pd.Timestamp(BASE_EPOCH)
    gpu = _load_module(ROOT / "aws_gpu" / "train_gpu.py", "gpu_pin")
    assert gpu.HOLDOUT == CFG_HOLDOUT and gpu.MAX_GAP == MAX_INTERPOLABLE_GAP
    assert gpu.BASE == pd.Timestamp(BASE_EPOCH)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{passed}/{passed + failed} casos OK" + (" ✓" if not failed else f"  ({failed} FALLAN)"))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
