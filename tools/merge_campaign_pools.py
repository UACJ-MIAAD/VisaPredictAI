#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66 — extraído del heredoc de run_campaign_aq{,_tail}.sh).

UNA campaña = UN run_id: 9 consumidores aguas abajo filtran por `run_id==max()`. El runbook escribe la
mitad no-GBM (lane P) y la GBM (stage B) como invocaciones SEPARADAS de run_comparison → dos run_ids; sin
esta colapsada los consumidores veían solo la mitad GBM (cazado en vivo: ets_fad_mean=NaN en key_facts). El
id por-mitad sobrevive en `source_run_id`.
"""

from __future__ import annotations

import pathlib
import sys

import pandas as pd


def merge() -> int:
    camp = pathlib.Path("reports/campaign")
    ev = pathlib.Path("reports/eval")
    for table in ("FAD", "DFF"):
        for block in ("family", "employment"):
            parts = []
            for kind in ("nongbm", "gbm"):
                f = camp / f"aq_pool_{kind}_{table}_{block}.csv"
                if f.exists():
                    parts.append(pd.read_csv(f))
                else:
                    print(f"MISSING pool half: {f}")
            if not parts:
                continue
            full = pd.concat(parts, ignore_index=True)
            full["source_run_id"] = full["run_id"]
            full["run_id"] = full["run_id"].max()
            full.to_csv(camp / f"campaign_pool_{table}_{block}.csv", index=False)
            tgt = f"model_comparison_{table}21.csv" if block == "family" else f"model_comparison_EB_{table}21.csv"
            full.to_csv(ev / tgt, index=False)
            print(f"{table}/{block}: {len(full)} rows -> {tgt}")
    return 0


if __name__ == "__main__":
    sys.exit(merge())
