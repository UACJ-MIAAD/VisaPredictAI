"""Tests de la épica AM (ensembles que sí pueden ganar) — plan MODELOS BRUTAL 2026-07-04.

Cubre: AM1 (median-of-best-K por serie, elegido por sel_mase — leakage-free), AM2 (pesos
simplex del stacking global), AM3 (piezas puras del FFORMA real: softmax de errores y
clases de pseudo-réplica), AM4 (mediana entre semillas del deep antes de combinar,
denominador deduplicado uniforme, comentario stale corregido, migraciones a
``metrics.mase_by_series`` y ``build_model(..., table=)``).

Datos y rutas mockeados (patrón de ``test_ensemble.py``): no dependen de los CSV reales.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("darts")  # capa de modelado: se salta sin el extra `model`

from vp_model import dataset, ensemble  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _load_experiment(name: str):
    spec = importlib.util.spec_from_file_location(f"ens_brutal_{name}", ROOT / "experiments" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- fixtures sintéticas -------------------------------------------------------------

DATES = pd.to_datetime(["2024-01-01", "2024-02-01"])


def _holdout_frame() -> pd.DataFrame:
    """3 series: mexico/india son pseudo-réplicas (mismo `actual`), china es distinta.

    Modelos: m1 (mejor sel_mase, fc 10), m2 (2o sel_mase, fc 14), m3 (PEOR sel_mase pero
    pronóstico perfecto en hold-out) — si best-K mirara el hold-out elegiría m3.
    """
    fcs = {"m1": 10.0, "m2": 14.0, "m3": 11.0}
    rows = []
    for country, actual in (("mexico", 11.0), ("india", 11.0), ("china", 20.0)):
        for d in DATES:
            for m, fc in fcs.items():
                rows.append(
                    {"country": country, "category": "F1", "date": d, "model": m, "actual": actual, "forecast": fc}
                )
    return pd.DataFrame(rows)


def _comparison_frame() -> pd.DataFrame:
    rows = []
    for country in ("mexico", "india", "china"):
        for m, sel, hold in (("m1", 0.1, 0.5), ("m2", 0.2, 0.6), ("m3", 0.9, 0.0)):
            rows.append(
                {
                    "run_id": 1,
                    "model": m,
                    "country": country,
                    "category": "F1",
                    "table": "FAD",
                    "sel_mase": sel,
                    "hold_mase": hold,
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture
def synthetic_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(ensemble, "REPORTS", tmp_path)
    (tmp_path / "eval").mkdir()
    _holdout_frame().to_csv(tmp_path / "eval" / "holdout_forecasts_FAD.csv", index=False)
    _comparison_frame().to_csv(tmp_path / "eval" / "model_comparison_FAD21.csv", index=False)
    # serie cruda: rampa mensual que CONTIENE las fechas del hold-out (máscara F-only) y
    # cuya escala naïve estacional previa a 2024-01 es exactamente 12.
    s = pd.Series(np.arange(124.0), index=pd.date_range("2014-01-01", periods=124, freq="MS"))
    monkeypatch.setattr(dataset, "load_series", lambda *a, **k: s)
    return tmp_path


# --- AM1: median-of-best-K por serie --------------------------------------------------


def test_best_k_selects_by_selection_not_holdout(synthetic_reports):
    """best-2 elige m1+m2 (mejor sel_mase) aunque m3 sea perfecto en el hold-out."""
    strat, per_series = ensemble.best_k_combination("FAD", 2)
    assert set(per_series.models) == {"m1+m2"}
    # mediana(10, 14) = 12; |11-12| = 1; escala = 12 -> MASE 1/12 por serie deduplicada
    assert per_series.hold_mase.iloc[0] != pytest.approx(0.0)
    # si hubiera hecho trampa con m3 el MASE sería 0
    assert strat.hold_mase > 0


def test_best_k_dedups_replicas(synthetic_reports):
    """El denominador es el de representantes: 3 series crudas -> 2 efectivas (AM4b)."""
    strat, per_series = ensemble.best_k_combination("FAD", 2)
    assert len(per_series) == 2  # china + una representante de {mexico, india}
    assert "2 series efectivas" in strat.detail


def test_best_k_report_aggregate_rows(synthetic_reports):
    report = ensemble.best_k_report("FAD", ks=(2, 3))
    agg = report[report.country == "ALL"]
    assert list(agg.k) == [2, 3]
    # k=3 mediana(10,14,11)=11 = actual de mexico/india -> mejor que k=2 en las réplicas
    assert agg[agg.k == 3].hold_mase.iloc[0] <= agg[agg.k == 2].hold_mase.iloc[0]
    per = report[report.country != "ALL"]
    assert set(per[per.k == 3].models) == {"m1+m2+m3"}


def test_curated_and_combinations_use_dedup_denominator(synthetic_reports):
    """AM4b: TODOS los reportes de combinaciones puntúan sobre las series efectivas."""
    strat = ensemble.curated_combination("FAD", subset=("m1", "m2", "m3"))
    assert "2 series efectivas" in strat.detail
    for s in ensemble.combinations("FAD"):
        assert "2 series efectivas" in s.detail


def test_stale_five_percent_claim_removed():
    """AM4c: el comentario 'la mediana supera al mejor único (~5%)' era falso post-resurrección."""
    src = (ROOT / "vp_model" / "ensemble.py").read_text()
    assert "supera al mejor único" not in src
    assert "0.1136" in src  # el empate real quedó documentado


# --- AM2: stacking simplex global ------------------------------------------------------


def test_simplex_weights_recover_convex_truth():
    stacking = _load_experiment("improve_stacking")
    rng = np.random.default_rng(7)
    f = rng.normal(1000, 200, size=(80, 3))
    y = 0.3 * f[:, 0] + 0.7 * f[:, 1]  # verdad convexa; el 3er modelo es ruido
    w = stacking.fit_simplex_weights(f, y, np.full(80, 12.0))
    assert w.shape == (3,)
    assert np.all(w >= 0) and np.isclose(w.sum(), 1.0)
    assert np.allclose(w[:2], [0.3, 0.7], atol=0.02) and w[2] < 0.02


def test_simplex_weights_scale_invariance_across_series():
    """El escalado por serie evita que una serie de nivel gigante domine el ajuste."""
    stacking = _load_experiment("improve_stacking")
    # serie chica: prefiere el modelo 0 con margen 10 (escala 5 -> 2 en unidades MASE);
    # serie grande: prefiere el modelo 1 con margen 1e6 (escala 5e5 -> 2, SIMÉTRICO escalado)
    f_small = np.array([[10.0, 20.0]] * 30)
    y_small = f_small[:, 0]
    f_big = np.array([[2e6, 1e6]] * 30)
    y_big = f_big[:, 1]
    f = np.vstack([f_small, f_big])
    y = np.concatenate([y_small, y_big])
    scale = np.concatenate([np.full(30, 5.0), np.full(30, 5e5)])
    w = stacking.fit_simplex_weights(f, y, scale)
    # escalado: las dos series pesan igual -> óptimo interior w ~ [0.5, 0.5]
    assert w[0] == pytest.approx(0.5, abs=0.05)
    # SIN escalar, la serie grande domina y aplasta a la chica (el bug que AM2 corrige)
    w_raw = stacking.fit_simplex_weights(f, y, np.ones(60))
    assert w_raw[0] < 0.05


# --- AM3: piezas puras del FFORMA real -------------------------------------------------


def test_softmax_weights_monotone_and_normalized():
    fforma = _load_experiment("improve_fforma")
    w = fforma.softmax_weights(np.array([0.1, 0.2, 0.9]), temp=0.1)
    assert np.isclose(w.sum(), 1.0)
    assert w[0] > w[1] > w[2]  # menor error predicho -> mayor peso
    # temperatura alta -> pesos casi uniformes
    w_flat = fforma.softmax_weights(np.array([0.1, 0.2, 0.9]), temp=1e6)
    assert np.allclose(w_flat, 1 / 3, atol=1e-3)


def test_replica_classes_group_identical_actuals():
    fforma = _load_experiment("improve_fforma")
    classes = fforma.replica_classes(_holdout_frame())
    assert classes.loc[("mexico", "F1")] == classes.loc[("india", "F1")]
    assert classes.loc[("china", "F1")] != classes.loc[("mexico", "F1")]


# --- AM4a: mediana entre semillas del deep ---------------------------------------------


def test_load_deep_median_aggregates_seeds(tmp_path):
    run_ens = _load_experiment("run_ensembles")
    for i, val in enumerate((0.0, 10.0, 4.0), start=1):
        pd.DataFrame({"unique_id": ["mexico/family/F1"], "ds": ["2024-01-01"], "BiTCN": [val]}).to_csv(
            tmp_path / f"global_FAD_camp_diff_s{i}.csv", index=False
        )
    med = run_ens.load_deep_median(sorted(tmp_path.glob("global_FAD_camp_diff_s*.csv")), "BiTCN")
    assert len(med) == 1 and med.BiTCN.iloc[0] == pytest.approx(4.0)  # mediana(0, 10, 4)
    empty = run_ens.load_deep_median([], "BiTCN")
    assert empty.empty


# --- migraciones de la Oleada 1: build_model(..., table=) ------------------------------


def test_build_model_receives_table_at_migrated_call_sites():
    """Los GBMs deben recibir la tabla para usar sus params tuneados por tabla."""
    expectations = {
        ROOT / "experiments" / "generate_web_forecasts.py": ("build_model(name, table=table)", 2),
        ROOT / "experiments" / "save_finalists.py": ("build_model(name, table=table)", 1),
        ROOT / "experiments" / "export_forecasts.py": ("build_model(name, table=table)", 1),
        ROOT / "vp_model" / "report.py": ("build_model(model_name, table=table)", 1),
    }
    for path, (needle, count) in expectations.items():
        src = path.read_text()
        assert src.count(needle) == count, f"{path.name}: esperaba {count}x '{needle}'"


def test_mase_loops_migrated_to_canonical_scorer():
    """AM4d: los archivos de la épica usan metrics.mase_by_series (no loops a mano)."""
    for name in ("run_ensembles", "improve_stacking", "improve_fforma", "improve_conformal", "eval_deep_pi"):
        src = (ROOT / "experiments" / f"{name}.py").read_text()
        assert "mase_by_series" in src, f"{name}.py no usa el scorer canónico"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
