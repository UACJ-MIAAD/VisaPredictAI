"""Conformal avanzado para subir la cobertura del PI del ganador profundo (hoy 0.77–0.89 → 0.95).

La investigación señaló EnbPI / Adaptive Conformal Inference (ACI) / CQR. Aquí se comparan,
sobre los pronósticos puntuales del deep global (BiTCN, ``reports/global_{table}_camp_diff_s1.csv``):
  * split-conformal con el cuantil FINITE-SAMPLE correcto ceil((n+1)(1-α))/n de |residuales|,
  * ACI (Gibbs & Candès 2021): ajusta α_t online tras cada punto para clavar la cobertura,
contra el baseline (neuralforecast conformal, cobertura 0.77 DFF / 0.89 FAD).

Calibración leakage-free: por serie, los primeros 12 meses del hold-out calibran; los últimos
12 se evalúan (cobertura empírica del PI 95% + ancho medio escalado). Corre en ``ante``.
Uso:  ante/bin/python improve_conformal.py [--mlflow]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import tracking
from vp_model import dataset
from vp_model.metrics import naive_scale_before

REPORTS = Path(__file__).resolve().parent / "reports"
ALPHA = 0.05  # PI al 95%


def _split_conformal(res_cal: np.ndarray, alpha: float) -> float:
    """Semiancho conformal: cuantil finite-sample de |residuales| de calibración."""
    n = len(res_cal)
    q_level = min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)
    return float(np.quantile(np.abs(res_cal), q_level, method="higher"))


def _aci(res_cal: np.ndarray, res_test: np.ndarray, alpha: float, gamma: float = 0.05) -> tuple[float, float]:
    """Adaptive Conformal Inference: ajusta alpha_t online; devuelve (cobertura, ancho medio)."""
    a = alpha
    pool = list(np.abs(res_cal))
    covered, widths = [], []
    for r in res_test:
        a_eff = min(max(a, 1e-3), 0.999)
        q = float(np.quantile(pool, 1 - a_eff, method="higher"))
        covered.append(abs(r) <= q)
        widths.append(2 * q)
        a = a + gamma * (alpha - (0 if abs(r) <= q else 1))  # err_t=1 si fuera del PI
        pool.append(abs(r))
    return float(np.mean(covered)), float(np.mean(widths))


def _evaluate(table: str) -> dict:
    deep = pd.read_csv(REPORTS / f"global_{table}_camp_diff_s1.csv", parse_dates=["ds"])
    cov_split, cov_aci, w_split, w_aci = [], [], [], []
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
        # split conformal
        q = _split_conformal(res[:cut], ALPHA)
        cov_split.append(float(np.mean(np.abs(res[cut:]) <= q)))
        w_split.append(2 * q / scale)
        # ACI
        c, w = _aci(res[:cut], res[cut:], ALPHA)
        cov_aci.append(c)
        w_aci.append(w / scale)
    return {
        "split_cov": float(np.mean(cov_split)),
        "split_width": float(np.mean(w_split)),
        "aci_cov": float(np.mean(cov_aci)),
        "aci_width": float(np.mean(w_aci)),
        "n": len(cov_split),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    base_cov = {"FAD": 0.89, "DFF": 0.77}
    for table in ("FAD", "DFF"):
        r = _evaluate(table)
        print(f"\n=== CONFORMAL AVANZADO {table} ({r['n']} series, target cobertura 0.95) ===")
        print(f"  baseline (nf conformal)      : cobertura ~{base_cov[table]}")
        print(f"  split-conformal finite-sample: cobertura {r['split_cov']:.3f}  ancho {r['split_width']:.2f}")
        print(f"  ACI (adaptive)               : cobertura {r['aci_cov']:.3f}  ancho {r['aci_width']:.2f}")
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


if __name__ == "__main__":
    main()
