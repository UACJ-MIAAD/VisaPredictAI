"""Conformal avanzado para subir la cobertura del PI del ganador profundo (objetivo 0.95).

La investigación señaló EnbPI / Adaptive Conformal Inference (ACI) / CQR. Aquí se comparan,
sobre los pronósticos puntuales del deep global (BiTCN, ``reports/campaign/global_{table}_camp_diff_s1.csv``):
  * split-conformal con el cuantil FINITE-SAMPLE correcto ceil((n+1)(1-α))/n de |residuales|,
  * ACI (Gibbs & Candès 2021): ajusta α_t online tras cada punto para clavar la cobertura,
contra el baseline neuralforecast-conformal, que se DERIVA de ``reports/eval/deep_pi_{table}.csv``
(AN4c: antes era un literal stale hardcodeado que contradecía el CSV medible).

Calibración leakage-free: por serie, los primeros 12 meses del hold-out calibran; los últimos
12 se evalúan. El gamma de ACI se elige de un grid {0.01, 0.03, 0.05, 0.1} usando SOLO el tramo
de calibración (sub-split interno 2/3 pool / 1/3 online) — el test nunca participa en la
selección (AN4a). El gamma elegido por tabla se publica en ``reports/eval/aci_gamma.json``
para que el flujo desplegado (``generate_web_forecasts``) lo consuma.

Hygiene (AN7): pooled coverages carry a Jeffreys CI and n; means of per-series coverages are
reported as before for continuity but the binomial claim is the pooled one.

Corre en ``ante``.  Uso:  ante/bin/python experiments/improve_conformal.py [--mlflow]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from vp_data import tracking
from vp_model import dataset
from vp_model.intervals import jeffreys_ci
from vp_model.metrics import mase_by_series, naive_scale_before

REPORTS = Path(__file__).resolve().parent.parent / "reports"
ALPHA = 0.05  # PI al 95%
GAMMA_GRID = (0.01, 0.03, 0.05, 0.1)
N_FLOOR = 30  # AN7: below this a pooled coverage is flagged insufficient_n


def _split_conformal(res_cal: np.ndarray, alpha: float) -> float:
    """Semiancho conformal: cuantil finite-sample de |residuales| de calibración."""
    n = len(res_cal)
    q_level = min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)
    return float(np.quantile(np.abs(res_cal), q_level, method="higher"))


def _aci(res_cal: np.ndarray, res_test: np.ndarray, alpha: float, gamma: float) -> tuple[list[bool], list[float]]:
    """Adaptive Conformal Inference: ajusta alpha_t online; devuelve (hits, anchos)."""
    a = alpha
    pool = list(np.abs(res_cal))
    covered: list[bool] = []
    widths: list[float] = []
    for r in res_test:
        a_eff = min(max(a, 1e-3), 0.999)
        q = float(np.quantile(pool, 1 - a_eff, method="higher"))
        covered.append(bool(abs(r) <= q))
        widths.append(2 * q)
        a = a + gamma * (alpha - (0 if abs(r) <= q else 1))  # err_t=1 si fuera del PI
        pool.append(abs(r))
    return covered, widths


def select_gamma(res_cal_by_series: list[np.ndarray], alpha: float, grid: tuple[float, ...] = GAMMA_GRID) -> float:
    """Pick gamma on CALIBRATION data only (AN4a): sub-split each series' calibration
    residuals into a 2/3 pool + 1/3 online stretch, replay ACI per gamma, and choose the
    gamma whose pooled coverage is closest to (1 - alpha), breaking ties by mean width."""
    best: tuple[float, float, float] | None = None  # (|cov - target|, mean_width, gamma)
    for gamma in grid:
        hits: list[bool] = []
        widths: list[float] = []
        for res in res_cal_by_series:
            cut = max(1, (2 * len(res)) // 3)
            if len(res) - cut < 1:
                continue
            h, w = _aci(res[:cut], res[cut:], alpha, gamma)
            hits += h
            widths += w
        if not hits:
            continue
        key = (abs(float(np.mean(hits)) - (1 - alpha)), float(np.mean(widths)), gamma)
        if best is None or key[:2] < best[:2]:
            best = key
    return best[2] if best is not None else 0.05


def _baseline_cov(table: str) -> float | None:
    """Derive the neuralforecast-conformal baseline coverage from ``deep_pi_{table}.csv``
    (F-only mask, mean of per-series coverages — same recipe as eval_deep_pi). AN4c: the
    previous hardcoded {FAD: 0.89, DFF: 0.77} contradicted the measurable CSVs."""
    path = REPORTS / "eval" / f"deep_pi_{table}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["ds"])
    model = next(c for c in df.columns if "-" not in c and c not in ("unique_id", "ds", "y"))
    covs = []
    for uid, g in df.groupby("unique_id"):
        country, _block, category = uid.split("/")
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        g = g.sort_values("ds")
        y = full.reindex(g["ds"]).to_numpy()
        fin = np.isfinite(y)
        if not fin.any():
            continue
        g, y = g[fin], y[fin]
        covs.append(float(np.mean((y >= g[f"{model}-lo-95"].to_numpy()) & (y <= g[f"{model}-hi-95"].to_numpy()))))
    return float(np.mean(covs)) if covs else None


def _point_mase(deep: pd.DataFrame, table: str) -> float:
    """Point MASE of the deep forecasts via the canonical scorer (AM4d).

    ``metrics.mase_by_series`` owns the F-only mask + shared naive scale; the residual /
    coverage machinery below keeps its own loop because it needs per-series residual
    SPLITS (calibration/test), which is outside the point scorer's contract.
    """
    frame = deep[deep["BiTCN"].notna()][["unique_id", "ds", "BiTCN"]].rename(
        columns={"ds": "date", "BiTCN": "forecast"}
    )
    parts = frame.unique_id.str.split("/")
    frame = frame.assign(country=parts.str[0], category=parts.str[2])
    return float(mase_by_series(frame, table).mean())


def _evaluate(table: str) -> dict:
    deep = pd.read_csv(REPORTS / "campaign" / f"global_{table}_camp_diff_s1.csv", parse_dates=["ds"])
    series: list[dict] = []
    for uid, g in deep.groupby("unique_id"):
        country, _b, category = uid.split("/")
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        g = g.sort_values("ds")
        g = g[g["ds"].isin(full.index)]  # F-only
        if len(g) < 16 or "BiTCN" not in g.columns:
            continue
        y = full.reindex(g["ds"]).to_numpy()
        res = y - g["BiTCN"].to_numpy()  # residuales del pronóstico
        cut = len(res) // 2
        scale = naive_scale_before(full, g["ds"].iloc[cut])
        series.append({"res_cal": res[:cut], "res_test": res[cut:], "scale": scale})

    point_mase = _point_mase(deep, table)

    # AN4a: gamma selected on calibration residuals only, then frozen for the test pass.
    gamma = select_gamma([s["res_cal"] for s in series], ALPHA)

    cov_split, w_split = [], []
    aci_hits_all: list[bool] = []
    cov_aci, w_aci = [], []
    split_hits_all: list[bool] = []
    for s in series:
        q = _split_conformal(s["res_cal"], ALPHA)
        hits_split = list(np.abs(s["res_test"]) <= q)
        split_hits_all += hits_split
        cov_split.append(float(np.mean(hits_split)))
        w_split.append(2 * q / s["scale"])
        hits, widths = _aci(s["res_cal"], s["res_test"], ALPHA, gamma)
        aci_hits_all += hits
        cov_aci.append(float(np.mean(hits)))
        w_aci.append(float(np.mean(widths)) / s["scale"])

    def _pooled(hits: list[bool]) -> dict:
        k, n = int(sum(hits)), len(hits)
        lo, hi = jeffreys_ci(k, n)
        return {"cov": k / n, "ci": (round(lo, 3), round(hi, 3)), "n": n, "insufficient_n": n < N_FLOOR}

    return {
        "gamma": gamma,
        "point_mase": point_mase,
        "split_cov": float(np.mean(cov_split)),
        "split_width": float(np.mean(w_split)),
        "split_pooled": _pooled(split_hits_all),
        "aci_cov": float(np.mean(cov_aci)),
        "aci_width": float(np.mean(w_aci)),
        "aci_pooled": _pooled(aci_hits_all),
        "n": len(series),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    rows = []
    gammas: dict[str, float] = {}
    for table in ("FAD", "DFF"):
        r = _evaluate(table)
        gammas[table] = r["gamma"]
        base = _baseline_cov(table)
        base_txt = f"{base:.3f} (derived from deep_pi_{table}.csv)" if base is not None else "n/a (no deep_pi csv)"
        sp, ap_ = r["split_pooled"], r["aci_pooled"]
        print(f"\n=== CONFORMAL AVANZADO {table} ({r['n']} series, target cobertura 0.95) ===")
        print(f"  MASE puntual BiTCN (scorer canónico): {r['point_mase']:.3f}  [informativo]")
        print(f"  baseline (nf conformal)      : cobertura {base_txt}")
        print(
            f"  split-conformal finite-sample: cobertura {r['split_cov']:.3f} "
            f"[pooled {sp['cov']:.3f} CI95 {sp['ci']} n={sp['n']}]  ancho {r['split_width']:.2f}"
        )
        print(
            f"  ACI (gamma={r['gamma']}, grid-cal)  : cobertura {r['aci_cov']:.3f} "
            f"[pooled {ap_['cov']:.3f} CI95 {ap_['ci']} n={ap_['n']}]  ancho {r['aci_width']:.2f}"
        )
        if sp["insufficient_n"] or ap_["insufficient_n"]:
            print(f"  WARNING: n < {N_FLOOR} — coverage claims flagged insufficient_n")
        rows.append(
            {
                "table": table,
                "n_series": r["n"],
                "baseline_coverage": round(base, 4) if base is not None else None,
                "aci_gamma": r["gamma"],
                "split_coverage": round(r["split_cov"], 4),
                "split_coverage_pooled": round(sp["cov"], 4),
                "split_cov_ci_lo": sp["ci"][0],
                "split_cov_ci_hi": sp["ci"][1],
                "split_width_scaled": round(r["split_width"], 4),
                "aci_coverage": round(r["aci_cov"], 4),
                "aci_coverage_pooled": round(ap_["cov"], 4),
                "aci_cov_ci_lo": ap_["ci"][0],
                "aci_cov_ci_hi": ap_["ci"][1],
                "aci_width_scaled": round(r["aci_width"], 4),
                "n_pooled": sp["n"],
                "insufficient_n": sp["insufficient_n"],
            }
        )
        if args.mlflow:
            for m, cov, w in [
                ("split_conformal", r["split_cov"], r["split_width"]),
                ("aci", r["aci_cov"], r["aci_width"]),
            ]:
                tracking.log_run(
                    f"improve_{table}",
                    f"conformal_{m}/{table}",
                    params={"method": f"conformal_{m}", "table": table, "layer": "improve"},
                    metrics={"coverage95": cov, "width_scaled": w},
                    tags={"layer": "improve", "technique": "conformal"},
                )
    pd.DataFrame(rows).to_csv(
        REPORTS / "eval" / "conformal_coverage.csv", index=False
    )  # artefacto reproducible del claim ACI
    # AN4: the deployed flow (generate_web_forecasts) reads the selected gamma from here.
    (REPORTS / "eval" / "aci_gamma.json").write_text(
        json.dumps({**gammas, "grid": list(GAMMA_GRID), "selected_on": "calibration split only"}, indent=2) + "\n"
    )
    print(f"\naci_gamma.json -> {gammas}")


if __name__ == "__main__":
    main()
