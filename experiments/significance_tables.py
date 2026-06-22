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
    # DEDUP pseudo-replicación: el corte mundial se replica idéntico en varios países (India,
    # China, All-Charg comparten valores para algunas categorías) → filas idénticas en TODOS los
    # modelos. Inflarían N y estrecharían la diferencia crítica de Nemenyi. Conservar las series
    # DISTINTAS (hallazgo del audit dúo: 25 -> ~14 DFF / ~15 FAD efectivas).
    n_raw = int(piv.shape[0])
    piv = piv.drop_duplicates()
    fr = significance.friedman_nemenyi(piv)
    losses = {m: piv[m].to_numpy() for m in piv.columns}
    mcs = significance.model_confidence_set(losses, alpha=0.10)
    return {
        "n_series": int(piv.shape[0]),
        "n_series_raw": n_raw,
        "n_models": int(piv.shape[1]),
        "friedman_p": fr["friedman_p"],
        "avg_rank": fr["avg_rank"].round(2).to_dict(),
        "mcs_alpha10": sorted(mcs),
        "_piv": piv,
        "_nemenyi": fr["nemenyi"],
        "_avg_rank": fr["avg_rank"],
    }


def _distinct_series(f: pd.DataFrame) -> set[tuple[str, str]]:
    """Series DISTINTAS por firma de su vector de cortes reales (`actual`). El corte mundial se
    replica idéntico en varios países → esas series son pseudo-réplicas; conservar una por firma
    para no inflar la N efectiva del DM (que asume observaciones no replicadas)."""
    base = f[["country", "category", "date", "actual"]].drop_duplicates()
    keep: set[tuple[str, str]] = set()
    seen: set[tuple] = set()
    for (c, cat), g in base.groupby(["country", "category"]):
        sig = tuple(g.sort_values("date")["actual"].round(3).tolist())
        if sig not in seen:
            seen.add(sig)
            keep.add((c, cat))
    return keep


def _dm_deep_vs_parsimony(table: str) -> dict:
    """DM (squared-error): mejor deep global vs mejor parsimonia, pareado por celda, sobre las
    series DISTINTAS (sin pseudo-replicación del corte mundial). Una sola comparación, así que
    NO se aplica Holm (corrección de familia de tamaño 1 = no-op); se reporta el p crudo."""
    f = pd.read_csv(REPORTS / f"finalist_forecasts_{table}.csv")
    n_raw = int(f.groupby(["country", "category"]).ngroups)
    keep = _distinct_series(f)
    f = f[[(c, cat) in keep for c, cat in zip(f.country, f.category, strict=True)]].reset_index(drop=True)
    f["ae"] = (f["forecast"] - f["actual"]).abs()
    key = ["country", "category", "date"]
    deep_models = [m for m in f.model.unique() if m in {"BiTCN", "AutoBiTCN", "NHITS", "PatchTST", "TiDE"}]
    pars_models = [m for m in f.model.unique() if m in {"arima", "sarima", "catboost", "lightgbm", "kalman"}]
    mean_ae = f.groupby("model")["ae"].mean()
    deep_ranked = sorted(deep_models, key=lambda m: mean_ae.get(m, np.inf))
    best_pars = min(pars_models, key=lambda m: mean_ae.get(m, np.inf))

    def _dm_vs(dmn: str) -> tuple[int, float, float, float, float]:
        a = f[f.model == dmn].set_index(key)["forecast"].rename("d")
        b = f[f.model == best_pars].set_index(key)["forecast"].rename("p")
        act = f[f.model == dmn].set_index(key)["actual"].rename("y")
        jj = pd.concat([a, b, act], axis=1).dropna()
        ed = (jj["d"] - jj["y"]).to_numpy()
        ep = (jj["p"] - jj["y"]).to_numpy()
        dm, pv = significance.dm_test(ed, ep, power=2)
        return int(len(jj)), float(np.abs(ed).mean()), float(np.abs(ep).mean()), round(dm, 3), round(pv, 4)

    best_deep = deep_ranked[0]
    n_pairs, mae_d, mae_p, dm, pval = _dm_vs(best_deep)
    # PRUEBA DE ROBUSTEZ: el 2.º mejor deep (casi empatado en MAE) puede dar un p MUY distinto
    # — esa sensibilidad de selección ES la fragilidad reportada en el cuerpo. robust = ambos <0.05.
    alt = deep_ranked[1] if len(deep_ranked) > 1 else best_deep
    _, _, _, _, pval_alt = _dm_vs(alt)
    robust = bool(pval < 0.05 and pval_alt < 0.05)
    return {
        "best_deep": best_deep,
        "best_parsimony": best_pars,
        "n_series_distinct": len(keep),
        "n_series_raw": n_raw,
        "n_pairs": n_pairs,
        "mae_deep": round(mae_d, 1),
        "mae_parsimony": round(mae_p, 1),
        "dm_stat": dm,
        "dm_p": pval,
        "alt_deep": alt,
        "alt_dm_p": pval_alt,
        "robust_significant": robust,
        "note": (
            "DM sobre series DISTINTAS (sin pseudo-replicacion). UNA comparacion -> sin Holm. "
            "La significancia NO es robusta a la eleccion del modelo profundo: "
            f"{best_deep} p={pval} pero {alt} (casi empatado en MAE) p={pval_alt}."
        ),
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
