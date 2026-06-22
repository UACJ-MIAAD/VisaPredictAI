"""Fuente ÚNICA de verdad de las cifras del proyecto, generada del pipeline.

Produce ``reports/key_facts.json`` (consumido por ``tools/check_consistency.py``) y
``reports/latex/key_facts.tex`` (macros \\newcommand para que el LaTeX pueda \\input la
fuente en vez de hardcodear números). TODO se computa de los datos/reportes — nada se
escribe a mano salvo unas pocas cifras del run profundo, marcadas como ``curated`` con
su procedencia.

Regla del proyecto (máxima): si una cifra cambia aquí, ``check_consistency`` falla hasta
que TODOS los artefactos (web/LaTeX/paper/RAG/README/docs) se reconcilien.

Uso (ante):  ante/bin/python experiments/build_key_facts.py   (o `make key-facts`)
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from vp_model import config

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DATA = ROOT / "data" / "processed" / "visa_panel_long.csv"


def _dataset() -> dict:
    df = pd.read_csv(DATA)
    f = df[df.status == "F"]
    fcount = f.groupby(["country", "category", "table"]).size()
    return {
        "n_series_structural": int(df.groupby(["country", "category", "table"]).ngroups),
        "n_series_with_F": int((fcount >= 1).sum()),
        "n_series_evaluable": int((fcount >= config.MIN_TRAIN["FAD"] + config.HOLDOUT).sum()),  # >=84 F
        "n_obs": int(len(df)),
        "n_obs_F": int((df.status == "F").sum()),
        "pct_trainable_F": int(round(100 * (df.status == "F").mean())),
        "date_first": df.bulletin_date.min()[:7],
        "date_last": df.bulletin_date.max()[:7],
    }


def _prospective() -> dict:
    m = json.loads((REPORTS / "forecast_scorecard_meta.json").read_text())
    o = m["overall"]
    return {
        "prosp_n_scored": int(o["n"]),
        "prosp_mae_days": int(round(o["mae_days"])),
        "prosp_mase": round(float(o["mase"]), 3),
        "prosp_cov95": round(float(o["cov95"]), 2),
        "prosp_cov80_heldout": round(float(m["band80_calibration"]["cov80_heldout"]), 2),
        "prosp_n_vintages_effective": int(m["n_vintages_effective"]),
        "band80_ratio": float(config.BAND80_RATIO),
    }


def _models() -> dict:
    out: dict = {}
    for tbl in ("FAD", "DFF"):
        p = pd.read_csv(REPORTS / f"campaign_pool_{tbl}_family.csv")
        mean = p.groupby("model").hold_mase.mean()
        out[f"ets_{tbl.lower()}_mean"] = round(float(mean.get("ets", float("nan"))), 3)
        out[f"theta_{tbl.lower()}_mean"] = round(float(mean.get("theta", float("nan"))), 3)
    aa = pd.read_csv(REPORTS / "auto_arima_baseline.csv")
    for tbl in ("FAD", "DFF"):
        d = aa[aa.table == tbl].hold_mase
        out[f"autoarima_{tbl.lower()}_mean"] = round(float(d.mean()), 3)
        out[f"autoarima_{tbl.lower()}_median"] = round(float(d.median()), 3)
    sig = json.loads((REPORTS / "significance_summary.json").read_text())
    out["mcs_fad"] = sorted(sig["ranking"]["FAD"]["mcs_alpha10"])
    out["mcs_dff"] = sorted(sig["ranking"]["DFF"]["mcs_alpha10"])
    # Cifras del run profundo multi-semilla (curated; fuente: experiments/aggregate_seeds.py).
    # Si se re-corre el deep, actualizar aquí y reconciliar artefactos.
    out["bitcn_dff_mean"] = 0.090
    out["autobitcn_fad_mean"] = 0.112
    out["fad_champion_mase"] = 0.117  # listón parsimonioso ETS/Theta en FAD
    # margen DFF del deep vs el mejor clásico afinado (Auto-ARIMA media), en %
    out["deep_dff_margin_pct"] = int(
        round(100 * (out["autoarima_dff_mean"] - out["bitcn_dff_mean"]) / out["autoarima_dff_mean"])
    )
    return out


def build() -> dict:
    facts = {
        "_source": "experiments/build_key_facts.py — NO editar a mano",
        **_dataset(),
        **_prospective(),
        **_models(),
    }
    (REPORTS / "key_facts.json").write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n")

    # macros LaTeX (\factNObs, \factProspMASE, …) — camelCase del key
    def macro(k: str) -> str:
        return "fact" + "".join(w.capitalize() for w in k.split("_"))

    lines = ["% Auto-generado por experiments/build_key_facts.py — \\input este archivo y usa \\factXxx.\n"]
    for k, v in facts.items():
        if k.startswith("_") or isinstance(v, list):
            continue
        lines.append(f"\\newcommand{{\\{macro(k)}}}{{{v}}}\n")
    (REPORTS / "latex" / "key_facts.tex").write_text("".join(lines))
    return facts


if __name__ == "__main__":
    f = build()
    print(json.dumps({k: v for k, v in f.items() if not k.startswith("_")}, indent=2, ensure_ascii=False))
