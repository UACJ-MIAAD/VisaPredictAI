"""Contract tests for the CORE stories of the MODELOS BRUTAL plan (epics AP/AI/AJ).

Covers:
  * AP1 — the Forecaster Protocol includes ``historical_forecasts``.
  * AP2 — ``metrics.mase_by_series`` is the canonical F-only per-series scorer.
  * AP3 — registry/_FACTORIES is the single source of the 24-model catalog.
  * AI1 — naive1/drift baselines exist and retrain each step.
  * AI2 — ``mase1`` (m=1 scale) is reported alongside the canonical MASE.
  * AI3 — auto-ARIMA trend spec matches the differencing order.
  * AI5 — Chronos pipeline cache is keyed by model name.
  * AJ1 — local NNs are wrapped in ``Differenced``.
  * AJ2 — likelihood models are sampled and collapsed to a stable median point.
  * AJ5 — GBMs pick up tuned hyperparameters per table (bridge otherwise).
  * AJ6 — rlinear is a real ridge (L2) behind standardized lags.

Skipped without the modeling extra or the DuckDB warehouse (same policy as
``test_models.py``).
"""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("darts")

from darts import TimeSeries  # noqa: E402

from vp_model import config, dataset, metrics, models, walkforward  # noqa: E402

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente")


# ---------------------------------------------------------------- AP3 / AI1
def test_catalog_has_24_models_and_factories_are_the_source() -> None:
    assert len(config.MODEL_NAMES) == 24
    # AL4: _FACTORIES = canonical catalog + declared extras (today: llt, pending
    # AQ promotion); registry()/run_comparison keep iterating MODEL_NAMES only.
    assert set(models._FACTORIES) == set(config.MODEL_NAMES) | models._EXTRA_MODELS
    assert set(models.registry()) == set(config.MODEL_NAMES)
    # AI1: the honest floors exist and retrain at every step (they are free).
    for name in ("naive1", "drift"):
        assert name in config.MODEL_NAMES
        assert name in config.RETRAIN_EACH_STEP


def test_every_factory_builds_and_satisfies_the_protocol() -> None:
    # AP1: everything the walk-forward engine calls must exist on every model.
    for name in config.MODEL_NAMES:
        m = models.build_model(name)
        for attr in ("fit", "predict", "historical_forecasts"):
            assert callable(getattr(m, attr, None)), f"{name} lacks {attr}"


def test_naive1_and_drift_backtest_smoke() -> None:
    for name in ("naive1", "drift"):
        r = walkforward.backtest(name, "mexico", "F3", "FAD")
        assert np.isfinite(r.holdout["mase"]) and r.holdout["mase"] > 0
        assert r.holdout["n"] == config.HOLDOUT


# --------------------------------------------------------------------- AI2
def test_compute_reports_mase1_alongside_mase() -> None:
    idx = pd.date_range("2000-01-01", periods=48, freq="MS")
    y = pd.Series(np.arange(48, dtype="float64") * 30 + 1000, index=idx)
    ts = TimeSeries.from_series(y)
    m = metrics.compute(ts[36:], ts[36:], ts[:36])
    assert m["mase"] == 0.0 and m["mase1"] == 0.0
    # linear series: naive-12 in-sample MAE = 12*30, naive-1 = 30 -> mase1 = 12*mase
    biased = ts[36:] + 60.0
    m = metrics.compute(biased, ts[36:], ts[:36], dates=idx[36:])
    assert m["mase1"] == pytest.approx(12.0 * m["mase"], rel=1e-9)


def test_backtest_rows_carry_mase1() -> None:
    r = walkforward.backtest("naive", "mexico", "F3", "FAD")
    for part in (r.selection, r.holdout):
        assert "mase1" in part and np.isfinite(part["mase1"])


# --------------------------------------------------------------------- AP2
def test_mase_by_series_matches_manual_loop() -> None:
    raw = dataset.load_series("mexico", "F3", "FAD").astype("float64")
    dates = raw.index[-10:]
    preds = raw.loc[dates] + 100.0  # constant error of 100 days
    frame = pd.DataFrame({"country": "mexico", "category": "F3", "date": dates, "forecast": preds.to_numpy()})
    got = metrics.mase_by_series(frame, "FAD")
    expected = 100.0 / metrics.naive_scale_before(raw, dates.min())
    assert got.loc[("mexico", "F3")] == pytest.approx(expected, rel=1e-12)


def test_mase_by_series_enforces_f_mask_and_skips_unknown() -> None:
    raw = dataset.load_series("mexico", "F3", "FAD").astype("float64")
    dates = list(raw.index[-3:])
    fake = pd.Timestamp("1990-06-15")  # not an F observation -> must be dropped
    frame = pd.DataFrame(
        {
            "country": ["mexico"] * 4 + ["atlantis"],
            "category": ["F3"] * 4 + ["F9"],
            "date": [*dates, fake, dates[0]],
            "forecast": [float(raw.loc[d]) for d in dates] + [999999.0, 1.0],
        }
    )
    got = metrics.mase_by_series(frame, "FAD")
    assert list(got.index) == [("mexico", "F3")]
    assert got.iloc[0] == pytest.approx(0.0)  # the fake date did not poison the MAE


# --------------------------------------------------------------------- AI3
def test_auto_arima_trend_matches_differencing_order() -> None:
    aab = pytest.importorskip("experiments.auto_arima_baseline")
    assert aab._trend_for(0) == "c"
    assert aab._trend_for(1) == "t"
    assert "auto_arima" in config.RETRAIN_EACH_STEP  # monthly retrain like arima/sarima


def test_auto_arima_declares_non_converging_series(monkeypatch, tmp_path, caplog) -> None:
    """FIX #21c: una serie que no converge debe DECLARARSE en WARNING, no descartarse
    en silencio. FALLA antes del fix (except a INFO 'skip', sin n por tabla, sin WARNING)
    y PASA después."""
    import logging
    from types import SimpleNamespace

    aab = pytest.importorskip("experiments.auto_arima_baseline")

    def fake_list_series(*, table, block, countries):  # noqa: ARG001
        if table != "FAD":
            return pd.DataFrame(columns=["country", "category"])
        return pd.DataFrame([{"country": "mexico", "category": "F1"}, {"country": "india", "category": "F2A"}])

    idx = pd.date_range("2001-12-01", periods=30, freq="MS")
    raw = pd.Series(np.arange(30, dtype="float64"), index=idx)

    def fake_backtest(name, country, category, table, *, model):  # noqa: ARG001
        if (country, category) == ("india", "F2A"):
            raise np.linalg.LinAlgError("LU decomposition error.")
        return SimpleNamespace(holdout={"mase": 0.10})

    monkeypatch.setattr(aab.dataset, "list_series", fake_list_series)
    monkeypatch.setattr(aab.dataset, "load_series", lambda *a, **k: raw)
    monkeypatch.setattr(aab.models, "to_timeseries", lambda s: SimpleNamespace(time_index=s.index))
    monkeypatch.setattr(aab, "_select_order", lambda y: (3, 1, 3))
    monkeypatch.setattr(aab.walkforward, "backtest", fake_backtest)
    monkeypatch.setattr(aab.config, "TABLES", ("FAD",))
    monkeypatch.setattr(aab, "REPORTS", tmp_path)
    (tmp_path / "eval").mkdir()

    with caplog.at_level(logging.WARNING):
        out = aab.run()

    df = pd.read_csv(out)
    # La serie convergida se sigue escribiendo; la fallida sigue ausente (comportamiento del CSV preservado)...
    assert list(zip(df.country, df.category, strict=True)) == [("mexico", "F1")]
    # ...pero el descarte AHORA se DECLARA en WARNING nombrando la serie exacta.
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("india" in m and "F2A" in m for m in warnings), "la serie no convergida debe declararse en WARNING"


def test_timesfm_and_tabpfn_liston_same_nature_across_tables() -> None:
    """FIX #21d: las barras de referencia FAD y DFF deben ser la MISMA métrica,
    diferenciándose solo por el token de tabla — si no, TimesFM/TabPFN se juzgan
    apples-to-oranges por tabla."""
    import re
    from pathlib import Path

    for script in ("experiments/improve_timesfm.py", "experiments/improve_tabpfn.py"):
        src = Path(script).read_text()
        m = re.search(r'liston\s*=\s*kf\[(?P<fad>[^\]]+)\][^\n]*?"FAD"[^\n]*?else\s*kf\[(?P<dff>[^\]]+)\]', src)
        assert m, f"no se localizó la línea del listón por tabla en {script}"
        fad = m.group("fad").strip().strip("\"'").replace("fad", "<T>")
        dff = m.group("dff").strip().strip("\"'").replace("dff", "<T>")
        assert fad == dff, f"{script}: listones de distinta naturaleza ({fad!r} vs {dff!r})"


def test_deployed_band95_holds_nominal_heldout_coverage() -> None:
    """Guardián de #21e (INVESTIGADO → no aplicado): las bandas al 95 % desplegadas
    (pi_scale_by_h.json) nunca deben bajar del 0.95 nominal de cobertura HELD-OUT.
    Un piso ingenuo al semiancho (el #21e literal) sin piso pareado en
    generate_web_forecasts empujaría DFF bajo 0.95 — la sub-cobertura silenciosa que
    se rehúsa desplegar."""
    dbr = pytest.importorskip("experiments.derive_band80_ratio")
    payload = dbr.derive_by_h()
    for table in ("FAD", "DFF"):
        v = payload["validation_heldout"][table]["95"]
        assert v.get("n", 0) >= 30, f"{table}: n held-out muy chico para juzgar cobertura: {v}"
        assert v["coverage"] >= 0.95, f"{table}: la banda 95 % sub-cubre held-out: {v}"


# --------------------------------------------------------------------- AI5
def test_chronos_cache_is_keyed_by_model_name() -> None:
    assert config.CHRONOS_MODEL == "amazon/chronos-bolt-base"
    models.ChronosForecaster._pipes["stub-a"] = "A"
    models.ChronosForecaster._pipes["stub-b"] = "B"
    try:
        assert models.ChronosForecaster._pipeline("stub-a") == "A"
        assert models.ChronosForecaster._pipeline("stub-b") == "B"  # no silent first-loaded reuse
    finally:
        models.ChronosForecaster._pipes.pop("stub-a")
        models.ChronosForecaster._pipes.pop("stub-b")


# --------------------------------------------------------------------- AJ1
def test_local_nns_are_differenced() -> None:
    for name in sorted(config.NN_DIFFERENCED):
        m = models.build_model(name)
        assert isinstance(m, models.Differenced), f"{name} must predict the first difference"


def test_differenced_reintegrates_samples() -> None:
    # A stochastic base (deepar/tft) must keep its sample dimension through the
    # cumsum reintegration instead of being flattened into one corrupt path.
    idx = pd.date_range("2010-01-01", periods=6, freq="MS")

    class StochasticBase:
        def fit(self, series, **kwargs):
            return self

        def predict(self, n, **kwargs):
            rng = np.random.default_rng(0)
            vals = rng.normal(10.0, 1.0, size=(n, 1, 50))
            t = pd.date_range(idx[-1] + pd.offsets.MonthBegin(1), periods=n, freq="MS")
            return TimeSeries.from_times_and_values(t, vals)

        def historical_forecasts(self, series, *, start, **kwargs):
            return self.predict(2)

    level = TimeSeries.from_times_and_values(idx, np.arange(6, dtype="float64") * 5 + 100)
    d = models.Differenced(StochasticBase())
    d.fit(level)
    fc = d.predict(2)
    assert fc.n_samples == 50
    med = fc.median(axis=2).values().flatten()
    # last level 125 + ~10 per step (median of N(10,1) draws)
    assert med[0] == pytest.approx(135.0, abs=2.0)
    assert med[1] == pytest.approx(145.0, abs=3.0)


# --------------------------------------------------------------------- AJ2
def test_likelihood_models_get_sampled_median_point() -> None:
    assert config.LIKELIHOOD_MODELS == {"deepar", "tft"}

    captured: dict[str, object] = {}
    raw = dataset.load_series("mexico", "F3", "FAD")
    ts_len = len(models.to_timeseries(raw))

    class StubLikelihoodModel:
        def fit(self, series, **kwargs):
            return self

        def predict(self, n, **kwargs):
            raise NotImplementedError

        def historical_forecasts(self, series, *, start, **kwargs):
            captured.update(kwargs)
            n = len(series) - start
            rng = np.random.default_rng(7)
            vals = rng.normal(0.5, 0.01, size=(n, 1, int(kwargs.get("num_samples", 1))))
            return TimeSeries.from_times_and_values(series.time_index[start:], vals)

    ts, fc = walkforward.run_forecasts("deepar", "mexico", "F3", "FAD", model=StubLikelihoodModel())
    assert captured["num_samples"] == config.NUM_SAMPLES_POINT  # sampling requested
    assert fc.n_samples == 1  # collapsed to the median point
    assert len(fc) == ts_len - config.MIN_TRAIN["FAD"]


def test_median_point_is_stable_across_predict_seeds() -> None:
    # AC AJ2: two runs with different predict seeds give (nearly) the same point.
    idx = pd.date_range("2010-01-01", periods=1, freq="MS")

    def draw(seed: int) -> TimeSeries:
        rng = np.random.default_rng(seed)
        vals = rng.normal(1000.0, 50.0, size=(1, 1, config.NUM_SAMPLES_POINT))
        return TimeSeries.from_times_and_values(idx, vals)

    p1 = float(walkforward._median_point(draw(1)).values()[0, 0])
    p2 = float(walkforward._median_point(draw(2)).values()[0, 0])
    # median SE ~ 1.25*sigma/sqrt(n) ~ 2.8 days; allow a generous 6-sigma band
    assert abs(p1 - p2) < 6 * 1.25 * 50.0 / np.sqrt(config.NUM_SAMPLES_POINT)
    # while a single draw (the old num_samples=1 behavior) is far noisier
    assert np.std([np.random.default_rng(s).normal(1000.0, 50.0) for s in range(20)]) > 20


# --------------------------------------------------------------------- AJ5
def test_trees_pick_up_tuned_params_per_table() -> None:
    if not models._TUNED_PARAMS_PATH.exists():
        pytest.skip("tuned_params.json ausente")
    import json

    tuned = json.loads(models._TUNED_PARAMS_PATH.read_text())["xgboost"]["FAD_family"]["best_params"]
    m_fad = cast(Any, models.build_model("xgboost", table="FAD"))
    assert m_fad.base.model.learning_rate == pytest.approx(tuned["learning_rate"])
    m_none = cast(Any, models.build_model("xgboost"))
    assert m_none.base.model.learning_rate == pytest.approx(0.02)  # bridge default
    assert m_none.base.model.n_estimators == 200


# --------------------------------------------------------------------- AJ6
def test_rlinear_is_a_real_ridge() -> None:
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline

    m = cast(Any, models.build_model("rlinear"))
    est = m.model
    assert isinstance(est, Pipeline)
    assert isinstance(est.steps[-1][1], Ridge)


# --------------------------------------------------------------------- AO2
def test_run_metadata_carries_data_lineage() -> None:
    meta = config.run_metadata()
    lineage = meta["data_lineage"]
    assert set(lineage) == {"panel_parquet_md5", "dvc_lock_md5"}
    for v in lineage.values():
        assert v is None or (isinstance(v, str) and len(v) == 12)
