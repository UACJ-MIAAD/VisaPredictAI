"""Evalúa los intervalos conformes del ganador profundo: cobertura 95%, interval score (MSIS) y CRPS.

Lee ``reports/eval/deep_pi_{table}.csv`` (punto + cuantiles conformes ya reintegrados a nivel) y
calcula, por serie y promedio sobre el bloque familiar:
  * cobertura empírica del PI 95% (fracción de y dentro de [lo95, hi95]; objetivo 0.95),
    con CI de Jeffreys sobre los hits agrupados y piso n>=30 (AN7),
  * MSIS (mean scaled interval score, M5) al 95%, escalado por el naïve estacional (= escala MASE),
  * CRPS aproximado por cuantiles (2·media de la pérdida pinball sobre los niveles disponibles),
para comparar contra el CRPS de los clásicos del .tex (SARIMA 48 / ARIMA 49 / DeepAR 174).

Los niveles se detectan de las columnas presentes, así el mismo script evalúa tanto el CSV
conforme (niveles 50/80/90/95) como el CQR (``--suffix _cqr``, niveles 80/95; ver run_deep_pi).

Corre en el entorno PRINCIPAL (ante/bin/python). Uso: ante/bin/python experiments/eval_deep_pi.py --table DFF
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from vp_model.intervals import jeffreys_ci
from vp_model.metrics import mase_by_series, naive_scale_before

# nivel L -> (cuantil_bajo, cuantil_alto)
QPAIRS = {50: (0.25, 0.75), 80: (0.10, 0.90), 90: (0.05, 0.95), 95: (0.025, 0.975)}
N_FLOOR = 30  # AN7: below this, the pooled coverage is flagged insufficient_n


def _pinball(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    e = y - q
    return float(np.mean(np.maximum(tau * e, (tau - 1) * e)))


def _interval_score(y, lo, hi, alpha) -> float:
    """Interval score de Gneiting-Raftery para un PI (1-alpha)."""
    return float(np.mean((hi - lo) + (2 / alpha) * (lo - y) * (y < lo) + (2 / alpha) * (y - hi) * (y > hi)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="DFF")
    ap.add_argument("--block", default="family")
    ap.add_argument("--suffix", default="", help="CSV suffix, e.g. '_cqr' for the CQR run")
    args = ap.parse_args()
    from vp_model import dataset

    df = pd.read_csv(f"reports/eval/deep_pi_{args.table}{args.suffix}.csv", parse_dates=["ds"])
    model = next(c for c in df.columns if "-" not in c and c not in ("unique_id", "ds", "y"))
    # levels actually present in this CSV (the CQR variant only carries 80/95)
    levels = [lvl for lvl in QPAIRS if f"{model}-lo-{lvl}" in df.columns and f"{model}-hi-{lvl}" in df.columns]
    if 95 not in levels:
        raise SystemExit(f"deep_pi CSV lacks the 95% band columns for {model}")
    rows = []
    k_pool = n_pool = 0
    for uid, g in df.groupby("unique_id"):
        country, block, category = uid.split("/")
        if block != args.block:
            continue
        g = g.sort_values("ds")
        full = dataset.load_series(country, category, args.table).astype("float64")
        # escala y nivel real alineados por FECHA (robusto a huecos C/U del bloque empleo).
        if (full.index < g["ds"].min()).sum() == 0:
            continue
        scale = naive_scale_before(full, g["ds"].min())
        # OJO: en el pipeline diferenciado, la columna `y` del CSV es Δy (objetivo de
        # entrenamiento), no el nivel. El nivel real del hold-out = la serie alineada por fecha.
        y = full.reindex(g["ds"]).to_numpy()
        # B1: medir SOLO donde hay F real — reindex deja NaN en meses C/U y un NaN
        # en la comparación contaba como "fuera del intervalo" (sesgaba cobertura abajo).
        fin = np.isfinite(y)
        if not fin.any():
            continue
        g = g[fin]
        y = y[fin]
        lo95, hi95 = g[f"{model}-lo-95"].to_numpy(), g[f"{model}-hi-95"].to_numpy()
        hits = (y >= lo95) & (y <= hi95)
        k_pool += int(hits.sum())
        n_pool += int(len(hits))
        cov = float(np.mean(hits))
        iscore = _interval_score(y, lo95, hi95, alpha=0.05)
        # CRPS ~ 2 * media de pinball sobre todos los cuantiles disponibles (mediana + pares lo/hi)
        taus, qs = [0.5], [g[model].to_numpy()]
        for lvl in levels:
            qlo, qhi = QPAIRS[lvl]
            taus += [qlo, qhi]
            qs += [g[f"{model}-lo-{lvl}"].to_numpy(), g[f"{model}-hi-{lvl}"].to_numpy()]
        crps = 2 * float(np.mean([_pinball(y, q, t) for q, t in zip(qs, taus, strict=True)]))
        rows.append(
            {"country": country, "category": category, "coverage95": cov, "msis95": iscore / scale, "crps": crps}
        )
    r = pd.DataFrame(rows)
    if not n_pool:
        raise SystemExit("no evaluable rows (block/table mismatch or all-NaN actuals)")
    lo_ci, hi_ci = jeffreys_ci(k_pool, n_pool)
    flag = f"  [INSUFFICIENT n<{N_FLOOR}]" if n_pool < N_FLOOR else ""
    # AM4d: point MASE via the canonical scorer (F-only mask + shared naive scale) instead
    # of another hand-rolled loop; the coverage/MSIS loop above stays because it needs the
    # interval columns and residual alignment, outside the point scorer's contract.
    pf = df[["unique_id", "ds", model]].rename(columns={"ds": "date", model: "forecast"})
    parts = pf.unique_id.str.split("/")
    pf = pf.assign(country=parts.str[0], block=parts.str[1], category=parts.str[2])
    point = mase_by_series(pf[pf.block == args.block], args.table)
    print(f"\n=== {model} · {args.table}{args.suffix}/{args.block} · PI · {len(r)} series · levels {levels} ===")
    print(f"  MASE puntual  : {point.mean():.3f}  (scorer canónico, {int(point.count())} series)")
    print(f"  cobertura 95% : {r.coverage95.mean():.3f}  (objetivo 0.95)")
    print(f"  pooled 95%    : {k_pool / n_pool:.3f}  CI95 Jeffreys [{lo_ci:.3f}, {hi_ci:.3f}]  n={n_pool}{flag}")
    print(f"  MSIS (95%)    : {r.msis95.mean():.3f}")
    print(f"  CRPS (días)   : {r.crps.mean():.1f}   [comparar: SARIMA 48 / ARIMA 49 / DeepAR 174]")


if __name__ == "__main__":
    main()
