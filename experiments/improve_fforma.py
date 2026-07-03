"""FFORMS/FFORMA: selección de modelo POR SERIE basada en features (meta-learning).

Investigación: el No-Free-Lunch motiva elegir el modelo por serie según sus características.
FFORMS = un meta-learner (XGBoost) aprende, de las features de la serie, qué modelo del pool
minimiza el error. Aquí se evalúa LEAVE-ONE-SERIES-OUT (anti-leakage) sobre las 25 series y se
compara contra el mejor modelo FIJO (ETS) y contra el ORÁCULO de selección perfecta (~0.112 FAD).

⚠️ Con solo 25 series el meta-learner sobreajusta (lo advierte la investigación); el resultado
honesto suele ser que NO bate al fijo. Corre en ``ante``. Uso:  ante/bin/python experiments/improve_fforma.py [--mlflow]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

from vp_data import tracking
from vp_model import dataset
from vp_model.metrics import naive_scale_before

REPORTS = Path(__file__).resolve().parent.parent / "reports"


def _features(s: np.ndarray) -> list[float]:
    """Features compactas por serie: largo, tendencia, variabilidad, autocorrelación, curtosis."""
    d = np.diff(s)
    acf1 = float(np.corrcoef(s[:-1], s[1:])[0, 1]) if len(s) > 2 else 0.0
    trend = float(abs(np.polyfit(np.arange(len(s)), s, 1)[0]) / (np.std(s) + 1e-9))
    return [
        len(s),
        trend,
        float(np.std(d) / (abs(np.mean(d)) + 1e-9)),
        acf1,
        float(pd.Series(d).kurt()),
        float(np.mean(np.abs(d))),
    ]


def _evaluate(table: str) -> dict:
    fc = pd.read_csv(REPORTS / f"finalist_forecasts_{table}.csv", parse_dates=["date"])
    pool_all = sorted(fc.model.unique())  # todos los finalistas disponibles (deep + local)
    series, X, mase, fcast = [], [], {}, {}
    for (country, category), g in fc.groupby(["country", "category"]):
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        piv = g.pivot_table(index="date", columns="model", values="forecast")
        act = g.pivot_table(index="date", columns="model", values="actual").iloc[:, 0]
        if piv.shape[1] < max(5, len(pool_all) - 2):  # casi todos los modelos presentes
            continue
        scale = naive_scale_before(full, piv.index.min())
        m = {col: float(np.mean(np.abs(act - piv[col]))) / scale for col in piv.columns}
        uid = f"{country}/{category}"
        series.append(uid)
        X.append(_features(full[full.index < piv.index.min()].to_numpy()))
        mase[uid] = m
        fcast[uid] = (act, piv)
    Xa = np.array(X)
    pool = sorted(set.intersection(*[set(mase[s]) for s in series]))  # modelos comunes a todas
    best_model = [min(pool, key=lambda m: mase[s][m]) for s in series]  # noqa: B023
    labels = sorted(set(best_model))  # solo los modelos que alguna vez ganan (clases contiguas)
    y = np.array([labels.index(b) for b in best_model])
    # mejor modelo FIJO = el de menor MASE promedio (el baseline a batir)
    best_fixed = min(pool, key=lambda m: np.mean([mase[s][m] for s in series]))  # noqa: B023
    # leave-one-out: predecir el mejor modelo de cada serie con un XGBoost sobre las otras 24
    sel_mase, fixed_mase, oracle_mase = [], [], []
    for i, uid in enumerate(series):
        tr = [j for j in range(len(series)) if j != i]
        clf = XGBClassifier(n_estimators=60, max_depth=3, learning_rate=0.1, verbosity=0, random_state=42)
        clf.fit(Xa[tr], y[tr])
        chosen = labels[int(clf.predict(Xa[i : i + 1])[0])]
        sel_mase.append(mase[uid][chosen])
        fixed_mase.append(mase[uid][best_fixed])
        oracle_mase.append(min(mase[uid][m] for m in pool))
    return {
        "fforms": float(np.mean(sel_mase)),
        "fixed": float(np.mean(fixed_mase)),
        "best_fixed": best_fixed,
        "oracle": float(np.mean(oracle_mase)),
        "n": len(series),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    for table in ("FAD", "DFF"):
        r = _evaluate(table)
        print(f"\n=== FFORMS {table} ({r['n']} series, leave-one-out) ===")
        print(f"  selección por features (FFORMS): MASE {r['fforms']:.4f}")
        print(f"  mejor fijo ({r['best_fixed']})         : MASE {r['fixed']:.4f}")
        print(f"  oráculo (selección perfecta)   : MASE {r['oracle']:.4f}")
        v = "MEJORA" if r["fforms"] < r["fixed"] else "no mejora"
        print(f"  -> {v} vs fijo; gap al oráculo {r['fforms'] - r['oracle']:.4f}")
        if args.mlflow:
            tracking.log_run(
                f"improve_{table}",
                f"fforms/{table}",
                params={"method": "fforms", "table": table, "layer": "improve", "best_fixed": r["best_fixed"]},
                metrics={"hold_mase": r["fforms"], "fixed": r["fixed"], "oracle": r["oracle"]},
                tags={"layer": "improve", "technique": "fforma"},
            )


if __name__ == "__main__":
    main()
