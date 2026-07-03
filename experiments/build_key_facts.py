"""Fuente ÚNICA de verdad de las cifras del proyecto, generada del pipeline.

Produce ``reports/governance/key_facts.json`` (consumido por ``tools/check_consistency.py``) y
``reports/latex/key_facts.tex`` (macros \\newcommand para que el LaTeX pueda \\input la
fuente en vez de hardcodear números). TODO se computa de los datos/reportes, incluidas
las cifras del run profundo multi-semilla (de los CSV de la campaña) y el listón
parsimonioso (del pool FAD); si un insumo falta, se degrada al key_facts previo (C1).

Regla del proyecto (máxima): si una cifra cambia aquí, ``check_consistency`` falla hasta
que TODOS los artefactos (web/LaTeX/paper/RAG/README/docs) se reconcilien.

Uso (ante):  ante/bin/python experiments/build_key_facts.py   (o `make key-facts`)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from vp_model import config

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DATA = ROOT / "data" / "processed" / "visa_panel_long.csv"


def _prev_facts() -> dict:
    """key_facts.json previo (versionado): fallback cuando falta un insumo (C1).

    En el runner del Action un CSV intermedio ausente NO debe tumbar el pipeline
    semanal: se conservan las cifras previas con un warning ruidoso. Si tampoco hay
    key_facts previo, el KeyError posterior aborta (mejor explotar que inventar).
    """
    p = REPORTS / "governance" / "key_facts.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _dataset() -> dict:
    df = pd.read_csv(DATA)
    f = df[df.status == "F"].copy()
    fcount = f.groupby(["country", "category", "table"]).size()
    # N1: definición ÚNICA de evaluable (vp_model.dataset.is_evaluable) — riqueza
    # de datos (≥84 F, el criterio publicado) Y factibilidad del walk-forward
    # (span F densificado). Verificado: mismo cohort de 74 series.
    from vp_model.dataset import is_evaluable

    per = pd.to_datetime(f.bulletin_date).dt.to_period("M")
    f["_per"] = per
    spans = f.groupby(["country", "category", "table"])["_per"].agg(lambda s: (s.max() - s.min()).n + 1)
    tables = {k: k[2] for k in fcount.index}
    n_eval = sum(1 for k in fcount.index if is_evaluable(int(fcount[k]), int(spans[k]), tables[k]))
    return {
        "n_series_structural": int(df.groupby(["country", "category", "table"]).ngroups),
        "n_series_with_F": int((fcount >= 1).sum()),
        "n_series_evaluable": int(n_eval),
        "n_obs": int(len(df)),
        "n_obs_F": int((df.status == "F").sum()),
        "pct_trainable_F": int(round(100 * (df.status == "F").mean())),
        "date_first": df.bulletin_date.min()[:7],
        "date_last": df.bulletin_date.max()[:7],
    }


def _prospective() -> dict:
    m = json.loads((REPORTS / "prospective" / "forecast_scorecard_meta.json").read_text())
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


def _deep_seed_mean(table: str, prefix: str, model: str) -> float | None:
    """Media multi-semilla del deep global (misma agregación que aggregate_seeds).

    Import perezoso y tolerante: eval_neuralforecast arrastra la pila de modelado,
    que no está instalada en todos los runners — sin ella (o sin los CSV de la
    campaña) se devuelve None y el caller degrada al key_facts previo (patrón C1).
    """
    try:
        from vp_model.eval_neuralforecast import eval_global_deep

        df = eval_global_deep(table)
        df = df[(df.block == "family") & (df.model == model) & df.variant.str.startswith(prefix)]
        if df.empty:
            return None
        return round(float(df.groupby("variant").hold_mase.mean().mean()), 3)
    except (ImportError, FileNotFoundError, OSError) as exc:  # insumo/dep ausente -> degradar
        # (un rename de columna u otro bug estructural sí debe explotar, no degradar en silencio)
        print(f"WARN: deep {model}/{prefix}*/{table} no derivable ({exc}); se conserva el previo", file=sys.stderr)
        return None


def _models() -> dict:
    out: dict = {}
    prev = _prev_facts()
    for tbl in ("FAD", "DFF"):
        path = REPORTS / "campaign" / f"campaign_pool_{tbl}_family.csv"
        keys = (f"ets_{tbl.lower()}_mean", f"theta_{tbl.lower()}_mean")
        if path.exists():
            pool = pd.read_csv(path)
            pool = pool[pool.run_id == pool.run_id.max()]  # última corrida, no el histórico acumulado
            mean = pool.groupby("model").hold_mase.mean()
            out[keys[0]] = round(float(mean.get("ets", float("nan"))), 3)
            out[keys[1]] = round(float(mean.get("theta", float("nan"))), 3)
            if tbl == "FAD":
                # listón parsimonioso FAD = mejor sel_mase medio de {ets, theta},
                # derivado del MISMO pool que la tabla de 21 modelos del .tex
                sel = pool.groupby("model").sel_mase.mean()
                candidates = [v for v in (sel.get("ets"), sel.get("theta")) if v is not None]
                out["fad_champion_mase"] = round(float(min(candidates)), 3) if candidates else prev["fad_champion_mase"]
        else:
            # C1: insumo ausente (runner limpio / campaña no re-corrida) — degradar, no crashear
            print(f"WARN: {path.name} ausente; se conservan {keys} del key_facts previo", file=sys.stderr)
            for k in keys:
                out[k] = prev[k]
            if tbl == "FAD":
                out["fad_champion_mase"] = prev["fad_champion_mase"]
    aa = pd.read_csv(REPORTS / "eval" / "auto_arima_baseline.csv")
    for tbl in ("FAD", "DFF"):
        d = aa[aa.table == tbl].hold_mase
        out[f"autoarima_{tbl.lower()}_mean"] = round(float(d.mean()), 3)
        out[f"autoarima_{tbl.lower()}_median"] = round(float(d.median()), 3)
    sig = json.loads((REPORTS / "eval" / "significance_summary.json").read_text())
    out["mcs_fad"] = sorted(sig["ranking"]["FAD"]["mcs_alpha10"])
    out["mcs_dff"] = sorted(sig["ranking"]["DFF"]["mcs_alpha10"])
    # Cifras del run profundo multi-semilla, derivadas de los CSV de la campaña con la
    # misma agregación que aggregate_seeds (media por semilla -> media entre semillas).
    for key, args in (
        ("bitcn_dff_mean", ("DFF", "camp_diff_s", "BiTCN")),
        ("autobitcn_fad_mean", ("FAD", "camp_auto_s", "AutoBiTCN")),
    ):
        derived = _deep_seed_mean(*args)
        out[key] = derived if derived is not None else prev[key]
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
    (REPORTS / "governance" / "key_facts.json").write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n")

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
