"""Tests for epic AL (world-class models that were missing).

Covers: the LLT state-space adapter (AL4) and its extra-catalog registration,
the ante_nf statsforecast bridge replicas anchored against vp_model.metrics
(AL1 — same contract as the constants pinned in test_feature_builder), the
causality of the global-GBM feature builder (AL2), the hurdle combination
rule (AL3) and the cone projection (AL5).

Runs with pytest *or* as a plain script (no pytest required):
    ante/bin/python tests/test_newmodels_brutal.py
"""

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skip cleanly in the base CI job (no model extra). lightgbm loads its OpenMP
# runtime before anything pulls in torch (macOS double-libomp discipline).
pytest.importorskip("lightgbm")
pytest.importorskip("darts")
pytest.importorskip("statsmodels")

from vp_model import metrics, models  # noqa: E402
from vp_model.config import HOLDOUT, MIN_BACKTEST_BUFFER, MIN_TRAIN, SEASONAL_PERIOD  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _trend_ts(n: int = 150, noise: float = 20.0, seed: int = 0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2005-01-01", periods=n, freq="MS")
    s = pd.Series(np.linspace(10_000, 14_000, n) + rng.normal(0, noise, n), index=idx)
    return models.to_timeseries(s)


# --- AL4: LLT state space -----------------------------------------------------


def test_llt_promoted_into_campaign_catalog():
    # AQ re-campaign (4-jul-2026) PROMOTED llt into the canonical catalog (24 models):
    # analytic PIs + same parsimony class as ets/theta. The extras mechanism stays
    # (empty) for future candidates.
    assert "llt" in models._FACTORIES and models._EXTRA_MODELS == frozenset()
    assert "llt" in models.MODEL_NAMES and "llt" in models.registry()
    m = models.build_model("llt")
    assert hasattr(m, "fit") and hasattr(m, "predict") and hasattr(m, "historical_forecasts")
    with pytest.raises(ValueError):
        models.build_model("nope")


def test_llt_fit_predict_extrapolates_trend():
    ts = _trend_ts()
    m = models.build_model("llt")
    m.fit(ts[:-12])
    fc = m.predict(12)
    assert len(fc) == 12 and np.isfinite(fc.values()).all()
    # A local linear trend must keep climbing past the last training level.
    assert float(fc.values()[-1, 0]) > float(ts[:-12].values()[-1, 0])


def test_llt_historical_forecasts_leakage_free():
    ts = _trend_ts(n=110)
    vals = ts.values(copy=True).flatten()
    tampered = vals.copy()
    tampered[100:] += 5_000.0  # rewrite the future only
    from darts import TimeSeries

    ts_b = TimeSeries.from_times_and_values(ts.time_index, tampered)
    fc_a = models.build_model("llt").historical_forecasts(ts, start=95)
    fc_b = models.build_model("llt").historical_forecasts(ts_b, start=95)
    # Origins 95..99 share an identical past — identical forecasts, or the
    # adapter is peeking at the future.
    a = fc_a.values().flatten()[:5]
    b = fc_b.values().flatten()[:5]
    assert np.allclose(a, b), "LLT forecast at origin t changed when only the future changed"
    assert len(fc_a) == len(ts) - 95


# --- AL1: statsforecast bridge (replicas anchored to vp_model) -----------------


def test_statsforecast_bridge_pinned_to_config():
    sf = _load_module(ROOT / "experiments" / "run_statsforecast.py", "sf_bridge")
    assert sf.HOLDOUT == HOLDOUT and sf.MIN_TRAIN == MIN_TRAIN
    assert sf.MIN_BACKTEST_BUFFER == MIN_BACKTEST_BUFFER and sf.SEASONAL_M == SEASONAL_PERIOD


def test_statsforecast_mask_replicas_match_metrics():
    sf = _load_module(ROOT / "experiments" / "run_statsforecast.py", "sf_bridge2")
    rng = np.random.default_rng(3)
    idx = pd.date_range("2010-01-01", periods=90, freq="MS")
    full = pd.Series(rng.normal(30, 40, 90).cumsum() + 12_000, index=idx)
    gappy = full.drop(full.index[[10, 11, 40]])  # C/U-style holes
    cutoff = full.index[-24]
    got = sf.naive_scale_before(gappy, cutoff)
    want = metrics.naive_scale_before(gappy, cutoff)
    assert got == pytest.approx(want), "ante_nf replica diverged from vp_model.metrics"
    # Degenerate scale: both sides must say NaN (never a silent 1.0).
    const = pd.Series(np.full(30, 7.0), index=idx[:30])
    assert math.isnan(sf.seasonal_naive_mae(const.to_numpy()))
    assert math.isnan(metrics.seasonal_naive_mae(const.to_numpy()))


def test_statsforecast_densify_keeps_f_values():
    sf = _load_module(ROOT / "experiments" / "run_statsforecast.py", "sf_bridge3")
    idx = pd.date_range("2020-01-01", periods=12, freq="MS")
    raw = pd.Series(np.arange(12, dtype="float64") * 10 + 100, index=idx).drop(idx[[4, 5]])
    dense = sf.densify(raw)
    assert len(dense) == 12 and not dense.isna().any()
    assert np.allclose(dense.loc[raw.index].to_numpy(), raw.to_numpy())  # observed F values untouched


def test_statsforecast_densify_is_causal_locf():
    # F1: la rejilla es LOCF causal — el hueco arrastra el bracket IZQUIERDO (no una
    # rampa hacia el bracket futuro) y es la MISMA función canónica de vp_model.
    from vp_model import preprocess

    sf = _load_module(ROOT / "experiments" / "run_statsforecast.py", "sf_bridge4")
    assert sf.to_regular_monthly_causal is preprocess.to_regular_monthly_causal
    idx = pd.date_range("2020-01-01", periods=12, freq="MS")
    raw = pd.Series(np.arange(12, dtype="float64") * 10 + 100, index=idx).drop(idx[[4, 5]])
    dense = sf.densify(raw)
    assert (dense.loc[idx[[4, 5]]] == raw.loc[idx[3]]).all()
    # metamórfico: mutar SOLO el futuro no cambia ningún mes en/antes del origen
    mutated = raw.copy()
    mutated.loc[idx[8] :] += 9_000.0
    pd.testing.assert_series_equal(sf.densify(mutated).loc[: idx[7]], dense.loc[: idx[7]])


# --- AL2: global GBM feature causality -----------------------------------------


def test_global_gbm_features_are_causal():
    from experiments.run_global_gbm import CALENDAR_COLS, build_series_frame

    #        t:    0    1  2  3  4   5  6  7  8  9
    dy = np.array([np.nan, 0, 0, 5, 5, -3, 0, 0, 0, 2])
    idx = pd.date_range("2020-01-01", periods=10, freq="MS")
    level = pd.Series(10_000 + np.nancumsum(np.nan_to_num(dy)), index=idx)
    f = build_series_frame(level)
    assert np.allclose(f["target_dy"].iloc[1:].to_numpy(), dy[1:])
    assert f["dy_lag1"].iloc[4] == 5 and f["dy_lag1"].iloc[5] == 5
    # months_frozen at t = zero-run ending at t-1 (dy_t itself must NOT count).
    assert f["months_frozen"].iloc[3] == 2  # dy_1=dy_2=0
    assert f["months_frozen"].iloc[4] == 0  # dy_3=5 broke the freeze
    assert f["months_frozen"].iloc[9] == 3  # dy_6..dy_8 = 0
    # advance streak ending at t-1
    assert f["advance_streak"].iloc[5] == 2  # dy_3=dy_4=5
    assert f["advance_streak"].iloc[6] == 0  # dy_5=-3 broke it
    # recent retrogression: any dy<0 in the 6 months BEFORE t
    assert f["retro_recent"].iloc[5] == 0.0
    assert f["retro_recent"].iloc[6] == 1.0
    # 'year' is deliberately not a feature of the global GBM; spreads NaN without refs
    assert "year" not in f.columns and set(CALENDAR_COLS).issubset(f.columns)
    assert f["spread_other_table"].isna().all() and f["spread_vs_allcharg"].isna().all()


def test_global_gbm_spread_uses_only_past():
    from experiments.run_global_gbm import build_series_frame

    idx = pd.date_range("2020-01-01", periods=8, freq="MS")
    level = pd.Series(np.arange(8, dtype="float64") * 10 + 1_000, index=idx)
    other = pd.Series(np.arange(8, dtype="float64") * 10 + 900, index=idx)
    f = build_series_frame(level, other_table_level=other)
    # spread at t = level(t-1) - other(t-1) = 100 for every complete origin
    assert np.allclose(f["spread_other_table"].iloc[1:].to_numpy(), 100.0)
    assert np.isnan(f["spread_other_table"].iloc[0])


def test_global_gbm_dense_level_is_causal_locf():
    # F1: dense_level = to_regular_monthly_causal (LOCF forward-only). El hueco arrastra
    # el bracket IZQUIERDO y mutar el futuro no cambia ninguna fila pasada del diseño.
    from experiments.run_global_gbm import dense_level

    idx = pd.date_range("2020-01-01", periods=12, freq="MS")
    raw = pd.Series(np.arange(12, dtype="float64") * 10 + 100, index=idx).drop(idx[[4, 5, 6, 7]])
    dense = dense_level(raw)
    assert len(dense) == 12 and not dense.isna().any()
    assert (dense.loc[idx[4:8]] == raw.loc[idx[3]]).all()  # LOCF, no rampa al bracket futuro
    mutated = raw.copy()
    mutated.loc[idx[8] :] += 5_000.0  # reescribe SOLO el futuro
    pd.testing.assert_series_equal(dense_level(mutated).loc[: idx[7]], dense.loc[: idx[7]])


# --- AL3: hurdle combination ----------------------------------------------------


def test_hurdle_point_expected_value_and_threshold():
    from experiments.run_hurdle import hurdle_point

    p = np.array([0.2, 0.8, 0.5])
    e = np.array([10.0, -5.0, 4.0])
    assert np.allclose(hurdle_point(p, e, threshold=False), [2.0, -4.0, 2.0])
    assert np.allclose(hurdle_point(p, e, threshold=True), [0.0, -5.0, 4.0])


# --- AL5: cone projection --------------------------------------------------------


def _cone_frame() -> pd.DataFrame:
    rows = [
        # FAD > DFF for mexico/F1 (violation); india/F1 coherent
        {"country": "mexico", "category": "F1", "table": "FAD", "date": "2026-08-01", "days": 12_000},
        {"country": "mexico", "category": "F1", "table": "DFF", "date": "2026-08-01", "days": 11_900},
        {"country": "india", "category": "F1", "table": "FAD", "date": "2026-08-01", "days": 10_000},
        {"country": "india", "category": "F1", "table": "DFF", "date": "2026-08-01", "days": 10_500},
        # china F2 above the all_chargeability reference (violation)
        {"country": "all_chargeability", "category": "F2", "table": "FAD", "date": "2026-08-01", "days": 15_000},
        {"country": "china", "category": "F2", "table": "FAD", "date": "2026-08-01", "days": 15_400},
    ]
    df = pd.DataFrame(rows)
    for col, off in (("lo80", -50), ("hi80", 50), ("lo95", -100), ("hi95", 100)):
        df[col] = df["days"] + off
    return df


def test_cone_projection_fixes_violations_and_preserves_band_widths():
    # F1: single-source — la proyección vive en vp_model.cone (el publicador y la
    # herramienta de auditoría retrospectiva importan las MISMAS funciones).
    from vp_model.cone import (
        apply_country_cap,
        apply_fad_dff,
        count_country_violations,
        count_fad_dff_violations,
    )

    df = _cone_frame()
    assert count_fad_dff_violations(df) == 1 and count_country_violations(df) == 1
    proj = apply_fad_dff(apply_country_cap(df))
    assert count_fad_dff_violations(proj) == 0 and count_country_violations(proj) == 0
    # the coherent pair is untouched
    india = proj[proj["country"] == "india"].set_index("table")["days"]
    assert india["FAD"] == 10_000 and india["DFF"] == 10_500
    # the violating pair became its order statistics (min/max)
    mx = proj[proj["country"] == "mexico"].set_index("table")["days"]
    assert mx["FAD"] == 11_900 and mx["DFF"] == 12_000
    # china clipped at the reference; band widths preserved everywhere
    assert proj.loc[proj["country"] == "china", "days"].iloc[0] == 15_000
    assert np.allclose((proj["hi95"] - proj["lo95"]).to_numpy(), 200.0)
    assert np.allclose((proj["hi80"] - proj["lo80"]).to_numpy(), 100.0)
    # idempotent: projecting a coherent frame changes nothing
    again = apply_fad_dff(apply_country_cap(proj))
    pd.testing.assert_frame_equal(again.reset_index(drop=True), proj.reset_index(drop=True))


def test_cone_project_counters_and_passthrough():
    from vp_model.cone import count_country_violations, count_fad_dff_violations, project

    df = _cone_frame()
    projected, counters = project(df)
    assert counters["cone_violations_pre"] == 2 and counters["cone_violations_post"] == 0
    assert counters["cone_violations_detail"]["fad_le_dff"] == {"pre": 1, "post": 0}
    assert counters["cone_violations_detail"]["country_le_allcharg"] == {"pre": 1, "post": 0}
    assert count_fad_dff_violations(projected) == 0 and count_country_violations(projected) == 0
    # coherent input -> the SAME frame back (byte-stable passthrough), counters all zero
    clean, counters2 = project(projected)
    assert clean is projected and counters2["cone_violations_pre"] == 0
    assert counters2["cone_violations_post"] == 0


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
