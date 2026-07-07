"""Artefacto vivo del campeón POR HORIZONTE (vp_model.horizon).

Corre el campeón-por-horizonte + el test de significancia (drift vs random walk) para
FAD y DFF, y emite DOS artefactos regenerables:
  * ``reports/eval/horizon_facts.json`` — números (campeón y significancia por horizonte).
  * ``reports/latex/horizon_champion.tex`` — tabla booktabs para ``\\input`` en el .tex.

Es el resultado HONESTO del desvío al deep GPU (memoria project_gpu_multihorizon_showdown):
el frontier deep no aportó, pero ``drift`` (random walk con deriva) le gana al RW puro a
casi todo horizonte y CRECE con él — significativo por Wilcoxon per-serie + Holm.

⚠️ Regla #0: son cifras de un backtest ROLLING (todo el span), NO comparables 1:1 con el
hold-out fijo canónico (MCS={naive1}). NO propagar a las cifras canónicas sin re-correr el
champion-challenger canónico con drift. Este .tex/JSON es un anexo de horizonte, autoetiquetado.

Uso (ante):  ante/bin/python experiments/build_horizon_facts.py
"""

from __future__ import annotations

import json
from pathlib import Path

from vp_model import horizon
from vp_model.config import HORIZONS, TABLES

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def _tex(facts: dict) -> str:
    """Tabla booktabs: por tabla, MASE del campeón vs el random walk y su ganancia %."""
    lines = [
        "% Auto-generado por experiments/build_horizon_facts.py — NO editar a mano.",
        "% Backtest ROLLING (todo el span), F-only, escala canónica. Anexo de horizonte;",
        "% NO son el hold-out fijo canónico (MCS={naive1}).",
        "\\begin{tabular}{lrrrrc}",
        "\\toprule",
        "Tabla & $h$ (meses) & drift & naïve-1 (RW) & $\\Delta$\\,\\% & signif. \\\\",
        "\\midrule",
    ]
    for table in TABLES:
        sig = {r["h"]: r for r in facts[table]["significance"]}
        for i, h in enumerate(HORIZONS):
            r = sig.get(h)
            if r is None:
                continue
            tag = table if i == 0 else ""
            star = "$\\checkmark$" if r["sig"] else "--"
            lines.append(f"{tag} & {h} & {r['drift']:.3f} & {r['naive1']:.3f} & {r['delta_pct']:+.1f} & {star} \\\\")
        lines.append("\\midrule")
    lines[-1] = "\\bottomrule"
    lines += ["\\end{tabular}"]
    return "\n".join(lines) + "\n"


def build() -> dict:
    facts: dict = {"_source": "experiments/build_horizon_facts.py — NO editar a mano", "horizons": list(HORIZONS)}
    for table in TABLES:
        champ = horizon.champion_by_horizon(table)
        sig = horizon.significance_by_horizon(table, champion="drift", baseline="naive1")
        facts[table] = {
            "champion_by_h": {int(h): str(champ.loc[h, "champion"]) for h in champ.index},
            "significance": [
                {
                    "h": int(h),
                    "drift": float(sig.loc[h, "drift"]),
                    "naive1": float(sig.loc[h, "naive1"]),
                    "delta_pct": float(sig.loc[h, "delta_pct"]),
                    "holm_p": float(sig.loc[h, "holm_p"]),
                    "sig": bool(sig.loc[h, "sig"]),
                    "n": int(sig.loc[h, "n"]),
                }
                for h in sig.index
            ],
        }
    (REPORTS / "eval" / "horizon_facts.json").write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n")
    (REPORTS / "latex" / "horizon_champion.tex").write_text(_tex(facts))
    return facts


def main() -> None:
    facts = build()
    for table in TABLES:
        sig = facts[table]["significance"]
        wins = [r["h"] for r in sig if r["sig"]]
        print(
            f"{table}: campeón h=1 -> {facts[table]['champion_by_h'].get(1)}; "
            f"drift bate al RW signif. en h={wins} (hasta {max((r['delta_pct'] for r in sig), default=0):+.0f}%)"
        )
    print("escrito reports/eval/horizon_facts.json + reports/latex/horizon_champion.tex")


if __name__ == "__main__":
    main()
