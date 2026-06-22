"""Deriva BAND80_RATIO en un split temporal DISJUNTO (calibración ⟂ evaluación).

La banda 80 % del demostrador es ``half95 * BAND80_RATIO``. Si el ratio se ajustara
sobre TODO el histórico y luego se reportara la cobertura sobre los mismos datos, la
cobertura 80 % sería circular (tautológica). Aquí el ratio se calibra SOLO sobre las
añadas ``config.BAND80_CAL_VINTAGES`` y la cobertura se valida sobre las añadas
RESTANTES (held-out) → la cobertura 80 % reportada es out-of-sample.

ratio = P80( |error| / half95 )  sobre las observaciones de calibración, donde
half95 = (hi95 - lo95)/2 es el semiancho conforme al 95 % (independiente del ratio).

Uso (read-only, no escribe nada):  ante/bin/python experiments/derive_band80_ratio.py
Si el valor impreso difiere de ``config.BAND80_RATIO``, actualízalo en config.py.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import config, dataset

REPORTS = Path(__file__).resolve().parent.parent / "reports"


def derive() -> dict:
    log = pd.read_csv(REPORTS / "forecast_log.csv")
    actuals = dataset.actuals_F()
    cal_set = set(config.BAND80_CAL_VINTAGES)

    rows = []
    for r in log.itertuples():
        a = actuals.get((r.country, r.category, r.table, r.date))
        if a is None:
            continue
        half95 = (r.hi95 - r.lo95) / 2.0
        if half95 <= 0:
            continue
        rows.append((r.origin, abs(r.days - a) / half95))
    df = pd.DataFrame(rows, columns=["origin", "std"])
    cal = df[df["origin"].isin(cal_set)]
    evl = df[~df["origin"].isin(cal_set)]
    if cal.empty or evl.empty:
        raise ValueError(f"split vacío: cal={len(cal)} eval={len(evl)} (¿BAND80_CAL_VINTAGES presentes?)")

    ratio = float(np.quantile(cal["std"], 0.80))
    out = {
        "ratio": round(ratio, 4),
        "n_cal": int(len(cal)),
        "n_eval": int(len(evl)),
        "cov80_cal_insample": round(float((cal["std"] <= ratio).mean()), 3),
        "cov80_eval_heldout": round(float((evl["std"] <= ratio).mean()), 3),
        "cal_vintages": sorted(cal_set),
    }
    return out


if __name__ == "__main__":
    r = derive()
    print(f"BAND80_RATIO calibrado (P80 sobre {r['cal_vintages']}, n={r['n_cal']}) = {r['ratio']}")
    print(f"cov80 in-sample (calibración)      = {r['cov80_cal_insample']}  (≈0.80 por construcción)")
    print(f"cov80 HELD-OUT (n={r['n_eval']}, honesto)   = {r['cov80_eval_heldout']}  <-- número a reportar")
    print(
        f"config.BAND80_RATIO actual = {config.BAND80_RATIO}",
        "(coincide)" if abs(r["ratio"] - config.BAND80_RATIO) < 1e-9 else "← ACTUALIZAR en config.py",
    )
