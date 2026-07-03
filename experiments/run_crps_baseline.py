"""CRPS de los modelos DISTRIBUCIONALES clásicos sobre el hold-out (deuda-2, 2-jul-2026).

Regenera ``reports/eval/crps_fad.csv`` (model, country, category, crps) — el insumo del panel
CRPS de ``make_result_figures.fig_coverage_crps`` y de la prosa del entregable ("SARIMA
48 / ARIMA 49 / DeepAR 174 días"). El CSV original fue una corrida one-off sin escritor
versionado; este script la hace reproducible y, tras B1, la puntúa SOLO sobre fechas F
reales (``walkforward.crps_holdout`` ya enmascara).

Corre en ``ante``:  ante/bin/python experiments/run_crps_baseline.py [--table FAD]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vp_model import dataset, walkforward  # noqa: E402
from vp_model.config import PROBABILISTIC, get_logger  # noqa: E402

log = get_logger("crps_baseline")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", default="FAD", choices=("FAD", "DFF"))
    ap.add_argument("--block", default="family")
    args = ap.parse_args()

    cat = dataset.list_series(table=args.table, block=args.block)
    rows = []
    for r in cat.itertuples():
        for m in sorted(PROBABILISTIC):
            try:
                c = walkforward.crps_holdout(m, r.country, r.category, args.table)
            except Exception as e:  # noqa: BLE001 — B6: fallo visible, serie no desaparece muda
                log.warning("FAIL %s %s/%s: %s: %s", m, r.country, r.category, type(e).__name__, str(e)[:60])
                c = float("nan")
            rows.append({"model": m, "country": r.country, "category": r.category, "crps": c})
            log.info("%s/%s %-8s CRPS=%.1f", r.country, r.category, m, c)
    out = ROOT / "reports" / "eval" / f"crps_{args.table.lower()}.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    df = pd.DataFrame(rows)
    log.info("escrito -> %s · medias: %s", out, df.groupby("model").crps.mean().round(1).to_dict())


if __name__ == "__main__":
    main()
