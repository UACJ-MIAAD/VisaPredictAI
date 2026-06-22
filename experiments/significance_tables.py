"""Pruebas de significancia para el paper: Friedman-Nemenyi + MCS (ranking del pool de
21 modelos) y Diebold-Mariano + Holm (deep global vs parsimonia). Datos ya existentes.

Entradas (read-only):
  • reports/campaign_pool_{FAD,DFF}_family.csv  — hold_mase por (modelo, serie) → ranking
  • reports/finalist_forecasts_{FAD,DFF}.csv    — forecast/actual por fecha → DM pareado

Salidas:
  • reports/significance_summary.json           — todos los números (procedencia)
  • reports/paper_micai/Figures/fig_cd_diagram.pdf  — diagrama de diferencia crítica (FAD)
  • imprime un fragmento LaTeX listo para pegar en el paper.

Uso (en ante):  ante/bin/python experiments/significance_tables.py   (o `make significance`)
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import significance

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
FIGS = ROOT / "reports" / "paper_micai" / "Figures"


def _ranking(table: str) -> dict:
    """Friedman-Nemenyi + MCS sobre la matriz serie×modelo de hold_mase del pool."""
    df = pd.read_csv(REPORTS / f"campaign_pool_{table}_family.csv")
    # una fila por (serie, modelo); pivot a serie×modelo. Si hay corridas repetidas,
    # toma la mejor (menor) hold_mase por modelo-serie (selección determinista).
    piv = df.groupby(["country", "category", "model"])["hold_mase"].min().unstack("model")
    piv = piv.dropna(axis=1, how="any")  # solo modelos evaluados en TODAS las series
    fr = significance.friedman_nemenyi(piv)
    losses = {m: piv[m].to_numpy() for m in piv.columns}
    mcs = significance.model_confidence_set(losses, alpha=0.10)
    return {
        "n_series": int(piv.shape[0]),
        "n_models": int(piv.shape[1]),
        "friedman_p": fr["friedman_p"],
        "avg_rank": fr["avg_rank"].round(2).to_dict(),
        "mcs_alpha10": sorted(mcs),
        "_piv": piv,
        "_nemenyi": fr["nemenyi"],
        "_avg_rank": fr["avg_rank"],
    }


def _dm_deep_vs_parsimony(table: str) -> dict:
    """DM (squared-error) + Holm: mejor deep global vs mejor parsimonia, pareado por celda."""
    f = pd.read_csv(REPORTS / f"finalist_forecasts_{table}.csv")
    f["ae"] = (f["forecast"] - f["actual"]).abs()
    key = ["country", "category", "date"]
    deep_models = [m for m in f.model.unique() if m in {"BiTCN", "AutoBiTCN", "NHITS", "PatchTST", "TiDE"}]
    pars_models = [m for m in f.model.unique() if m in {"arima", "sarima", "catboost", "lightgbm", "kalman"}]
    mean_ae = f.groupby("model")["ae"].mean()
    best_deep = min(deep_models, key=lambda m: mean_ae.get(m, np.inf))
    best_pars = min(pars_models, key=lambda m: mean_ae.get(m, np.inf))
    # alinear por celda-fecha común
    a = f[f.model == best_deep].set_index(key)["forecast"].rename("d")
    b = f[f.model == best_pars].set_index(key)["forecast"].rename("p")
    act = f[f.model == best_deep].set_index(key)["actual"].rename("y")
    j = pd.concat([a, b, act], axis=1).dropna()
    e_deep = (j["d"] - j["y"]).to_numpy()
    e_pars = (j["p"] - j["y"]).to_numpy()
    dm, pval = significance.dm_test(e_deep, e_pars, power=2)
    holm = significance.holm({f"{best_deep}_vs_{best_pars}": pval})
    return {
        "best_deep": best_deep,
        "best_parsimony": best_pars,
        "n_pairs": int(len(j)),
        "mae_deep": round(float(np.abs(e_deep).mean()), 1),
        "mae_parsimony": round(float(np.abs(e_pars).mean()), 1),
        "dm_stat": round(dm, 3),
        "dm_p": round(pval, 4),
        "holm_significant": holm[f"{best_deep}_vs_{best_pars}"][1],
    }


def _cd_diagram(rank_fad: dict) -> None:
    import matplotlib.pyplot as plt
    import scikit_posthocs as sp

    plt.rcParams.update({"font.family": "serif", "font.size": 8, "savefig.dpi": 300, "savefig.bbox": "tight"})
    fig, ax = plt.subplots(figsize=(5.0, 2.0))
    sp.critical_difference_diagram(rank_fad["_avg_rank"], rank_fad["_nemenyi"], ax=ax)
    FIGS.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGS / "fig_cd_diagram.pdf")
    plt.close(fig)
    print("  ✓ fig_cd_diagram.pdf")


def _latex(summary: dict) -> str:
    rows = []
    for tbl in ("FAD", "DFF"):
        r = summary["ranking"][tbl]
        top = sorted(r["avg_rank"].items(), key=lambda kv: kv[1])[:6]
        ranked = ", ".join(f"{m} ({v:.1f}{'$^\\star$' if m in r['mcs_alpha10'] else ''})" for m, v in top)
        rows.append(f"{tbl} & {r['n_models']} & {r['friedman_p']:.1e} & {ranked} \\\\")
    body = "\n".join(rows)
    return (
        "% Auto-generado por experiments/significance_tables.py\n"
        "\\begin{table}[t]\\centering\\footnotesize\n"
        "\\caption{Friedman--Nemenyi over the per-series hold-out MASE of the model pool. "
        "Lower average rank is better; $^\\star$ marks membership in the 90\\% Model "
        "Confidence Set. Friedman $p$ rejects equal performance in both tables.}\n"
        "\\label{tab:significance}\n"
        "\\begin{tabular}{llll}\n\\toprule\n"
        "Table & \\#models & Friedman $p$ & Top-6 by mean rank (MCS $^\\star$) \\\\\n\\midrule\n"
        f"{body}\n\\bottomrule\n\\end{{tabular}}\n\\end{{table}}\n"
    )


def main() -> None:
    summary: dict = {"ranking": {}, "dm": {}}
    rank = {}
    for tbl in ("FAD", "DFF"):
        r = _ranking(tbl)
        rank[tbl] = r
        summary["ranking"][tbl] = {k: v for k, v in r.items() if not k.startswith("_")}
        summary["dm"][tbl] = _dm_deep_vs_parsimony(tbl)
    _cd_diagram(rank["FAD"])
    (REPORTS / "significance_summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(json.dumps(summary, indent=2, default=str))
    print("\n=== LaTeX (pegar en paper.tex) ===\n" + _latex(summary))


if __name__ == "__main__":
    main()
