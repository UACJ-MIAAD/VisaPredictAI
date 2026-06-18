"""Evalúa los intervalos conformes del ganador profundo: cobertura 95%, interval score (MSIS) y CRPS.

Lee ``reports/deep_pi_{table}.csv`` (punto + cuantiles conformes ya reintegrados a nivel) y
calcula, por serie y promedio sobre el bloque familiar:
  * cobertura empírica del PI 95% (fracción de y dentro de [lo95, hi95]; objetivo 0.95),
  * MSIS (mean scaled interval score, M5) al 95%, escalado por el naïve estacional (= escala MASE),
  * CRPS aproximado por cuantiles (2·media de la pérdida pinball sobre los niveles disponibles),
para comparar contra el CRPS de los clásicos del .tex (SARIMA 48 / ARIMA 49 / DeepAR 174).

Corre en el entorno PRINCIPAL (ante/bin/python). Uso: ante/bin/python eval_deep_pi.py --table DFF
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from vp_model.metrics import naive_scale_before

# nivel L -> (cuantil_bajo, cuantil_alto)
QPAIRS = {50: (0.25, 0.75), 80: (0.10, 0.90), 90: (0.05, 0.95), 95: (0.025, 0.975)}


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
    args = ap.parse_args()
    from vp_model import dataset

    df = pd.read_csv(f"reports/deep_pi_{args.table}.csv", parse_dates=["ds"])
    model = next(c for c in df.columns if "-" not in c and c not in ("unique_id", "ds", "y"))
    rows = []
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
        lo95, hi95 = g[f"{model}-lo-95"].to_numpy(), g[f"{model}-hi-95"].to_numpy()
        cov = float(np.mean((y >= lo95) & (y <= hi95)))
        iscore = _interval_score(y, lo95, hi95, alpha=0.05)
        # CRPS ~ 2 * media de pinball sobre todos los cuantiles disponibles (mediana + pares lo/hi)
        taus, qs = [0.5], [g[model].to_numpy()]
        for lvl, (qlo, qhi) in QPAIRS.items():
            taus += [qlo, qhi]
            qs += [g[f"{model}-lo-{lvl}"].to_numpy(), g[f"{model}-hi-{lvl}"].to_numpy()]
        crps = 2 * float(np.mean([_pinball(y, q, t) for q, t in zip(qs, taus, strict=True)]))
        rows.append(
            {"country": country, "category": category, "coverage95": cov, "msis95": iscore / scale, "crps": crps}
        )
    r = pd.DataFrame(rows)
    print(f"\n=== {model} · {args.table}/{args.block} · PI conforme · {len(r)} series ===")
    print(f"  cobertura 95% : {r.coverage95.mean():.3f}  (objetivo 0.95)")
    print(f"  MSIS (95%)    : {r.msis95.mean():.3f}")
    print(f"  CRPS (días)   : {r.crps.mean():.1f}   [comparar: SARIMA 48 / ARIMA 49 / DeepAR 174]")


if __name__ == "__main__":
    main()
