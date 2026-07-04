"""FFORMA real: ponderación de modelos por serie vía meta-learning (AM3).

Replaces the previous caricature (6 hand-rolled features, an XGBClassifier predicting the
argmin model, plain LOO over 25 series contaminated by pseudo-replicas of the worldwide
cutoff). Per the FFORMA recipe (Montero-Manso et al. 2020):

  (a) meta-features = catch22/catch24 (+ series length) computed on the PRE-hold-out
      stretch of the raw F series. Computed directly with ``pycatch22`` (the library
      behind ``series_characterization.catch22_vector``) because that helper cleans and
      characterizes the FULL series — using it verbatim would leak the hold-out into the
      meta-features;
  (b) target = continuous per-model SELECTION-region MASE (``sel_mase`` from
      ``model_comparison_{table}21.csv`` — no argmin discretization). One XGBRegressor
      per model; combination weights = softmax(-predicted_error / T) with T = median
      training error (adaptive temperature, in MASE units);
  (c) grouped leave-one-CLASS-out: pseudo-replica classes (series with identical hold-out
      actuals — same signature as ``champion.replica_representatives``) never straddle
      the train/test split, so replicas cannot leak the answer;
  (d) runs on every series with persisted hold-out forecasts. Today that is 25 series
      (15/10 effective after dedup) — an honest n; the AQ campaign extends
      ``holdout_forecasts_{table}.csv`` to the 74 evaluable series (documented here, not
      re-derived now).

Scored with ``metrics.mase_by_series`` (canonical F-only scorer, AM4d) over deduplicated
replica representatives (AM4b), against the best FIXED model chosen by selection-region
MASE (leakage-free baseline) and the per-series hold-out ORACLE (upper bound, labeled).

Corre en ``ante``. Uso:  ante/bin/python experiments/improve_fforma.py [--mlflow]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

from vp_data import tracking
from vp_model import dataset, ensemble
from vp_model.metrics import mase_by_series

REPORTS = Path(__file__).resolve().parent.parent / "reports"


def replica_classes(fc: pd.DataFrame) -> pd.Series:
    """Class id per (country, category): identical hold-out ``actual`` vectors share a class.

    Same signature as ``champion.replica_representatives`` (which only returns one
    representative; the grouped-LOO folds need the full class assignment).
    """
    sig = fc.drop_duplicates(subset=["country", "category", "date"]).pivot_table(
        index=["country", "category"], columns="date", values="actual"
    )
    return sig.fillna(-1.0).groupby(list(sig.columns)).ngroup()


def softmax_weights(pred_err: np.ndarray, temp: float) -> np.ndarray:
    """FFORMA combination weights: softmax(-predicted_error / T); lower error, more weight."""
    z = -np.asarray(pred_err, dtype="float64") / max(temp, 1e-9)
    z -= z.max()
    w = np.exp(z)
    return w / w.sum()


def _meta_features(country: str, category: str, table: str, before: pd.Timestamp) -> list[float]:
    """catch24 + length on the raw F series STRICTLY BEFORE the hold-out (leakage-free)."""
    import pycatch22

    hist = dataset.load_series(country, category, table).astype("float64")
    hist = hist[hist.index < before]
    res = pycatch22.catch22_all(hist.tolist(), catch24=True)
    return [float(v) for v in res["values"]] + [float(len(hist))]


def _evaluate(table: str) -> dict:
    fc = pd.read_csv(REPORTS / "eval" / f"holdout_forecasts_{table}.csv", parse_dates=["date"])
    mc = pd.read_csv(REPORTS / "eval" / f"model_comparison_{table}21.csv").pipe(lambda d: d[d.run_id == d.run_id.max()])
    avail = sorted(set(fc.model.unique()) & set(mc.model.unique()))
    err = mc[mc.model.isin(avail)].pivot_table(index=["country", "category"], columns="model", values="sel_mase")
    err = err[avail].dropna()
    err = err[err.index.isin(set(map(tuple, fc[["country", "category"]].drop_duplicates().itertuples(index=False))))]

    holdout_start = fc.groupby(["country", "category"])["date"].min()
    x = np.array([_meta_features(c, cat, table, holdout_start.loc[(c, cat)]) for c, cat in err.index])
    classes = replica_classes(fc).reindex(err.index)

    # grouped leave-one-CLASS-out: one XGBRegressor per model, weights = softmax(-err/T)
    weights: dict[tuple[str, str], np.ndarray] = {}
    for cls in sorted(classes.unique()):
        tr, te = (classes != cls).to_numpy(), (classes == cls).to_numpy()
        if not tr.any() or not te.any():
            continue
        temp = float(np.median(err.to_numpy()[tr]))
        pred_err = np.column_stack(
            [
                XGBRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, verbosity=0, random_state=42)
                .fit(x[tr], err[m].to_numpy()[tr])
                .predict(x[te])
                for m in avail
            ]
        )
        for row, key in zip(pred_err, err.index[te], strict=True):
            weights[key] = softmax_weights(row, temp)

    # weighted combination of the persisted hold-out forecasts, scored canonically
    parts = []
    for (country, category), w in weights.items():
        g = fc[(fc.country == country) & (fc.category == category)]
        piv = g.pivot_table(index="date", columns="model", values="forecast")
        act = g.pivot_table(index="date", columns="model", values="actual").mean(axis=1)
        # a series missing a pool model entirely contributes no scorable rows (honest n)
        piv = piv.reindex(columns=avail).dropna()
        if piv.empty:
            continue
        parts.append(
            pd.DataFrame(
                {
                    "country": country,
                    "category": category,
                    "date": piv.index,
                    "pred": piv.to_numpy() @ w,
                    "actual": act.loc[piv.index].to_numpy(),
                }
            )
        )
    comb = pd.concat(parts, ignore_index=True)
    comb, n_raw, n_eff = ensemble.representative_filter(comb, table, fc)
    fforma = mase_by_series(comb, table, pred_col="pred", actual_col="actual")

    # baselines on the SAME deduplicated series: best fixed by selection + hold-out oracle
    hold = pd.concat(
        [mase_by_series(fc[fc.model == m], table, actual_col="actual").rename(m) for m in avail], axis=1
    ).loc[fforma.index]
    best_fixed = str(err.mean().idxmin())  # chosen on the SELECTION region (leakage-free)
    return {
        "fforma": float(fforma.mean()),
        "fixed": float(hold[best_fixed].mean()),
        "best_fixed": best_fixed,
        "oracle": float(hold.min(axis=1).mean()),  # upper bound: picks winners on the hold-out
        "n_raw": n_raw,
        "n_eff": n_eff,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    for table in ("FAD", "DFF"):
        r = _evaluate(table)
        print(
            f"\n=== FFORMA {table} ({r['n_raw']} series, {r['n_eff']} efectivas, grouped-LOO por clase de réplica) ==="
        )
        print(f"  FFORMA (softmax de errores predichos): MASE {r['fforma']:.4f}")
        print(f"  mejor fijo por selección ({r['best_fixed']})  : MASE {r['fixed']:.4f}")
        print(f"  oráculo (mira el hold-out)           : MASE {r['oracle']:.4f}")
        v = "MEJORA" if r["fforma"] < r["fixed"] else "no mejora"
        print(f"  -> {v} vs fijo; gap al oráculo {r['fforma'] - r['oracle']:.4f}")
        print(
            f"  n honesto: hoy {r['n_raw']} series con pool completo persistido; "
            "la campaña AQ extiende holdout_forecasts a las 74 evaluables"
        )
        if args.mlflow:
            tracking.log_run(
                f"improve_{table}",
                f"fforma/{table}",
                params={"method": "fforma", "table": table, "layer": "improve", "best_fixed": r["best_fixed"]},
                metrics={"hold_mase": r["fforma"], "fixed": r["fixed"], "oracle": r["oracle"]},
                tags={"layer": "improve", "technique": "fforma"},
            )


if __name__ == "__main__":
    main()
