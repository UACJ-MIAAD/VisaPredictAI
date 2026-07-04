"""TimesFM 2.5 (foundation model de Google) — zero-shot, walk-forward 1 paso.

La investigación destacó TimesFM con in-context fine-tuning (ICF). El checkpoint ICF no es
público; aquí se evalúa el TimesFM 2.5 BASE zero-shot (200M, torch, local — sin API ni cuota),
con walk-forward de 1 paso sobre el hold-out de 24m por serie, contra el listón VIGENTE
leído de ``reports/governance/key_facts.json`` (AI6: nunca cifras hardcodeadas — las
anteriores eran de la era pre-B1). Evaluación F-only, leakage-free. Corre en
``ante_tfm``.
Uso:  ante_tfm/bin/python experiments/improve_timesfm.py [--table FAD] [--mlflow]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.parquet"
HOLDOUT = 24
CONTEXT = 256


def _naive_scale(values: np.ndarray, m: int = 12) -> float:
    v = np.asarray(values, dtype="float64")
    d = np.abs(v[m:] - v[:-m]) if len(v) > m else np.abs(np.diff(v))
    s = float(np.mean(d)) if len(d) else 0.0
    return s if np.isfinite(s) and s > 0 else 1.0


def _actuals(parquet: pd.DataFrame, country: str, category: str, table: str) -> pd.Series:
    """Serie F-only (days_since_base donde status='F'), indexada por fecha — sin vp_model."""
    g = parquet[
        (parquet["country"] == country)
        & (parquet["category"] == category)
        & (parquet["table"] == table)
        & (parquet["status"] == "F")
    ]
    return g.set_index(pd.to_datetime(g["bulletin_date"]))["days_since_base"].astype("float64").sort_index()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="FAD")
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()

    import run_global_deep as R
    from timesfm import ForecastConfig
    from timesfm.timesfm_2p5.timesfm_2p5_torch import TimesFM_2p5_200M_torch

    model = TimesFM_2p5_200M_torch.from_pretrained("google/timesfm-2.5-200m-pytorch")
    model.compile(ForecastConfig(max_context=CONTEXT, max_horizon=1, normalize_inputs=True))

    panel = R.load_panel(args.table, "family")
    uids = sorted(panel["unique_id"].unique())
    series = {u: panel[panel.unique_id == u].sort_values("ds") for u in uids}
    dates = sorted(panel["ds"].unique())
    holdout = dates[-HOLDOUT:]

    preds: dict[str, list[tuple]] = {u: [] for u in uids}  # (ds, forecast)
    for t in holdout:  # walk-forward 1 paso: contexto = historia < t por serie
        ctx, items = [], []
        for u in uids:
            h = series[u][series[u]["ds"] < t]["y"].to_numpy()[-CONTEXT:]
            if len(h) >= 32:
                ctx.append(h.astype("float32"))
                items.append(u)
        if not ctx:
            continue
        point, _q = model.forecast(horizon=1, inputs=ctx)
        for u, p in zip(items, point, strict=True):
            preds[u].append((t, float(np.asarray(p).reshape(-1)[0])))

    raw = pd.read_parquet(PANEL)
    mases = []
    for u in uids:
        country, _b, category = u.split("/")
        full = _actuals(raw, country, category, args.table)
        if full.empty:
            continue
        rows = [(d, f) for d, f in preds[u] if d in full.index]  # F-only
        if not rows:
            continue
        ds = [d for d, _ in rows]
        y = full.reindex(ds).to_numpy()
        f = np.array([v for _, v in rows])
        scale = _naive_scale(full[full.index < min(ds)].to_numpy())
        mases.append(float(np.mean(np.abs(y - f))) / scale)
    mase = float(np.mean(mases))
    # AI6: the bar comes from the single source of truth (same pattern as
    # improve_tabpfn) — the previously hardcoded bars were dead pre-B1 numbers.
    import json

    kf = json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())
    liston = kf["fad_champion_mase"] if args.table == "FAD" else kf["bitcn_dff_mean"]
    print(f"\n=== TimesFM 2.5 zero-shot {args.table} ({len(mases)} series) ===")
    print(f"  MASE {mase:.4f}  vs listón {liston}  -> {'MEJORA' if mase < liston else 'no mejora'}")
    if args.mlflow:
        import sys

        sys.path.insert(0, str(ROOT))
        from vp_data import tracking

        tracking.log_run(
            f"improve_{args.table}",
            f"timesfm25/{args.table}",
            params={"method": "timesfm_2p5_zeroshot", "table": args.table, "layer": "improve"},
            metrics={"hold_mase": mase},
            tags={"layer": "improve", "technique": "timesfm"},
        )


if __name__ == "__main__":
    main()
