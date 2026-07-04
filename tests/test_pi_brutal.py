"""Contract tests for the prediction-interval overhaul (plan MODELOS BRUTAL, epic AN).

Covers, with synthetic data only (no DB, no trained models):
  * ``intervals.aci_alpha`` — pure ACI level replay (AN4b);
  * ``intervals.jeffreys_ci`` — Jeffreys binomial CI with boundary conventions (AN7b);
  * ``intervals.conformal`` — order-statistic quantile (AN7a) and the F-only
    ``calib_dates`` mask that keeps interpolated C/U months out of calibration (AN1);
  * ``generate_web_forecasts._band_halfwidths`` — per-horizon q_h bands with the
    documented sqrt(h) fallback (AN2);
  * ``derive_band80_ratio.compute_scales / validate_scales`` — quantile correctness,
    monotone-in-h envelope, min-n cell floor, held-out validation payload (AN2/AN7);
  * ``improve_conformal.select_gamma`` — gamma chosen from the grid on calibration
    data only (AN4a).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "experiments" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- intervals.aci_alpha (AN4b) ---------------------------------------------


def test_aci_alpha_empty_history_returns_alpha0() -> None:
    from vp_model.intervals import aci_alpha

    assert aci_alpha([], alpha0=0.05, gamma=0.05) == 0.05


def test_aci_alpha_hits_raise_misses_lower() -> None:
    from vp_model.intervals import aci_alpha

    a_hits = aci_alpha([True] * 20, alpha0=0.05, gamma=0.05)
    a_miss = aci_alpha([False] * 20, alpha0=0.05, gamma=0.05)
    assert a_hits > 0.05  # all covered -> can afford narrower intervals
    assert a_miss < 0.05  # all missed -> widen
    # mixed history at the target rate stays near alpha0: 1 miss per 19 hits ~ 5%
    a_mixed = aci_alpha(([True] * 19 + [False]) * 5, alpha0=0.05, gamma=0.05)
    assert abs(a_mixed - 0.05) < 0.02


def test_aci_alpha_clamped() -> None:
    from vp_model.intervals import aci_alpha

    assert aci_alpha([False] * 500, alpha0=0.05, gamma=0.5) == pytest.approx(1e-3)
    assert aci_alpha([True] * 100000, alpha0=0.05, gamma=0.5) <= 0.999


# --- intervals.jeffreys_ci (AN7) --------------------------------------------


def test_jeffreys_ci_bounds_and_boundaries() -> None:
    from vp_model.intervals import jeffreys_ci

    lo, hi = jeffreys_ci(5, 10)
    assert 0.0 < lo < 0.5 < hi < 1.0
    assert jeffreys_ci(0, 10)[0] == 0.0  # k=0 boundary convention
    assert jeffreys_ci(10, 10)[1] == 1.0  # k=n boundary convention
    # wider n -> tighter CI
    lo2, hi2 = jeffreys_ci(50, 100)
    assert hi2 - lo2 < hi - lo
    with pytest.raises(ValueError):
        jeffreys_ci(1, 0)
    with pytest.raises(ValueError):
        jeffreys_ci(11, 10)


# --- intervals.conformal: order statistic + F-only mask (AN7a / AN1) --------


def _ts(values, start="2020-01-01"):
    from darts import TimeSeries

    idx = pd.date_range(start, periods=len(values), freq="MS")
    return TimeSeries.from_series(pd.Series(np.asarray(values, dtype="float64"), index=idx)), idx


def test_conformal_uses_higher_order_statistic() -> None:
    pytest.importorskip("darts")
    from vp_model.intervals import conformal

    pred, _ = _ts([0.0] * 10)
    actual, _ = _ts(np.arange(1.0, 11.0))  # |residuals| = 1..10
    point, _ = _ts([50.0] * 6, start="2021-01-01")
    # alpha=0.3: q_level = ceil(11*0.7)/10 = 0.8 -> order statistic (method='higher') = 9,
    # where linear interpolation would give 8.2 (anti-conservative).
    iv = conformal(point, actual, pred, alpha=0.3)
    half = float((iv.upper.values() - point.values()).flatten()[0])
    assert half == pytest.approx(9.0)


def test_conformal_calib_dates_excludes_interpolated_months() -> None:
    pytest.importorskip("darts")
    from vp_model.intervals import conformal

    # 12 calibration months: 2 real F observations with |resid|=100, 10 interpolated
    # months with resid 0 (the artificially easy points AN1 kicks out of calibration).
    resid = np.zeros(12)
    f_pos = [3, 9]
    resid[f_pos] = 100.0
    pred, idx = _ts([0.0] * 12)
    actual, _ = _ts(resid)
    point, _ = _ts([0.0] * 6, start="2021-01-01")
    f_dates = idx[f_pos]

    masked = conformal(point, actual, pred, alpha=0.5, calib_dates=f_dates)
    unmasked = conformal(point, actual, pred, alpha=0.5)
    half_masked = float((masked.upper.values() - point.values()).flatten()[0])
    half_unmasked = float((unmasked.upper.values() - point.values()).flatten()[0])
    assert half_masked == pytest.approx(100.0)  # calibrated on real errors only
    assert half_unmasked == pytest.approx(0.0)  # interpolated zeros drown the signal
    with pytest.raises(ValueError):
        conformal(point, actual, pred, calib_dates=idx[:0])  # no F dates -> loud failure


# --- generate_web_forecasts._band_halfwidths (AN2) ---------------------------


def test_band_halfwidths_qh_and_fallback() -> None:
    pytest.importorskip("darts")
    gwf = _load("generate_web_forecasts")
    from vp_model import config

    scales = {"FAD": {"80": {"3": 2.0}, "95": {"3": 3.0}}}
    h80, h95, method = gwf._band_halfwidths(3, 100.0, "FAD", scales)
    assert (h80, h95, method) == (200.0, 300.0, "q_h")
    # horizon not calibrated -> documented sqrt(h) fallback
    h80, h95, method = gwf._band_halfwidths(5, 100.0, "FAD", scales)
    assert method == "sqrt_h"
    assert h95 == pytest.approx(100.0 * np.sqrt(5))
    assert h80 == pytest.approx(100.0 * config.BAND80_RATIO * np.sqrt(5))
    # no scales file at all -> same fallback
    assert gwf._band_halfwidths(1, 100.0, "DFF", None)[2] == "sqrt_h"


# --- derive_band80_ratio: per-horizon scales (AN2) ----------------------------


def test_compute_scales_quantiles_monotone_and_min_n() -> None:
    dbr = _load("derive_band80_ratio")
    rng = np.random.default_rng(7)
    rows = []
    rows += [("v1", "FAD", 1, s) for s in np.linspace(0.1, 4.0, 40)]  # wide errors at h=1
    rows += [("v1", "FAD", 2, s) for s in rng.uniform(0.0, 0.5, 40)]  # narrower at h=2
    rows += [("v1", "FAD", 3, s) for s in rng.uniform(0.0, 9.9, 10)]  # n<30 -> omitted
    cal = pd.DataFrame(rows, columns=["origin", "table", "h", "std"])
    scales, n_cal = dbr.compute_scales(cal, min_cell_n=30)

    q80_h1 = scales["FAD"]["80"]["1"]
    assert q80_h1 == pytest.approx(float(np.quantile(cal.loc[(cal.h == 1), "std"], 0.80, method="higher")), rel=1e-6)
    # monotone envelope: h=2 raw quantile is below h=1, so the h=1 value carries over
    assert scales["FAD"]["80"]["2"] == q80_h1
    assert scales["FAD"]["95"]["2"] >= scales["FAD"]["95"]["1"]
    # cell below the floor is omitted from scales but counted in n_cal
    assert "3" not in scales["FAD"]["80"]
    assert n_cal["FAD"]["3"] == 10


def test_validate_scales_coverage_ci_and_floor() -> None:
    dbr = _load("derive_band80_ratio")
    scales = {"FAD": {"80": {"1": 1.0}, "95": {"1": 2.0}}}
    # 40 held-out rows at h=1: 32 inside q80=1.0 (cov 0.8), all inside q95=2.0
    std = np.concatenate([np.full(32, 0.5), np.full(8, 1.5)])
    evl = pd.DataFrame({"origin": "vX", "table": "FAD", "h": 1, "std": std})
    val = dbr.validate_scales(evl, scales)
    v80, v95 = val["FAD"]["80"], val["FAD"]["95"]
    assert v80["coverage"] == pytest.approx(0.8)
    assert v80["ci95"][0] < 0.8 < v80["ci95"][1]
    assert v80["n"] == 40 and not v80["insufficient_n"]
    assert v95["coverage"] == 1.0 and v95["ci95"][1] == 1.0
    # tiny held-out set -> flagged
    val_small = dbr.validate_scales(evl.head(5), scales)
    assert val_small["FAD"]["80"]["insufficient_n"]


# --- improve_conformal.select_gamma (AN4a) -----------------------------------


def test_select_gamma_picks_from_grid_on_calibration_only() -> None:
    pytest.importorskip("darts")
    ic = _load("improve_conformal")
    rng = np.random.default_rng(11)
    res = [rng.normal(0, 1.0, 24) for _ in range(6)]
    gamma = ic.select_gamma(res, alpha=0.05)
    assert gamma in ic.GAMMA_GRID
    # degenerate input (no series long enough) -> safe default
    assert ic.select_gamma([np.array([1.0])], alpha=0.05) == 0.05
