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
    # Cifras del censo EDA que la prosa (web/.tex) cita: sin estas claves, el guardián
    # era ciego a su drift (audit 3-jul H3). Misma derivación que build_eda_facts.
    f_sorted = df[df.status == "F"].sort_values("bulletin_date").copy()
    deltas = f_sorted.groupby(["country", "block", "category", "table"])["days_since_base"].diff().dropna()
    dv_path = ROOT / "data" / "raw" / "dv_visa_rank_timecourse.csv"
    n_dv = int(len(pd.read_csv(dv_path))) if dv_path.exists() else 0
    return {
        "n_series_structural": int(df.groupby(["country", "category", "table"]).ngroups),
        "n_series_with_F": int((fcount >= 1).sum()),
        "n_series_evaluable": int(n_eval),
        "n_obs": int(len(df)),
        "n_obs_F": int((df.status == "F").sum()),
        "pct_trainable_F": int(round(100 * (df.status == "F").mean())),
        "date_first": df.bulletin_date.min()[:7],
        "date_last": df.bulletin_date.max()[:7],
        "n_months": int(pd.to_datetime(df.bulletin_date).dt.to_period("M").nunique()),
        "n_retro_events": int((deltas < 0).sum()),
        "pct_retro": round(100 * float((deltas < 0).mean()), 1),
        "pct_frozen": int(round(100 * float((deltas == 0).mean()))),
        "dv_n_rows": n_dv,
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
            med = pool.groupby("model").hold_mase.median()
            out[keys[0]] = round(float(mean.get("ets", float("nan"))), 3)
            out[keys[1]] = round(float(mean.get("theta", float("nan"))), 3)
            # AQ: the random-walk floor is now a first-class canonical figure — it
            # won both MCS at h=1 and every claim must be stated relative to it.
            out[f"naive1_{tbl.lower()}_mean"] = round(float(mean.get("naive1", float("nan"))), 3)
            out[f"naive1_{tbl.lower()}_median"] = round(float(med.get("naive1", float("nan"))), 3)
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
    # Tamaño del catálogo comparado (hoy 24): derivado de la MISMA tabla de comparación
    # que alimenta el .tex — la prosa del scorecard web lo cita y el guardián lo vigila.
    mc = pd.read_csv(REPORTS / "eval" / "model_comparison_FAD21.csv")
    out["n_models"] = int(mc.model.nunique())
    aa = pd.read_csv(REPORTS / "eval" / "auto_arima_baseline.csv")
    for tbl in ("FAD", "DFF"):
        d = aa[aa.table == tbl].hold_mase
        out[f"autoarima_{tbl.lower()}_mean"] = round(float(d.mean()), 3)
        out[f"autoarima_{tbl.lower()}_median"] = round(float(d.median()), 3)
    # (audit r4) mcs_fad/mcs_dff se retiraron de key_facts.json: PAYLOAD MUERTO —
    # se emitían pero NADIE los leía (son listas → sin macro \fact; las tripwires
    # MCS del guardián regexean la PROSA, no estos arrays; el web no fetchea
    # key_facts; el .tex no los usa). El claim MCS={naive1} vive donde SÍ se lee:
    # la prosa vigilada del deliverable/paper/web + el chunk del model card del RAG.
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


def _champion_challenger() -> dict:
    """Medias campeón/retador (sobre series deduplicadas) y CRPS del veredicto vigente.

    La prosa de A.8 las cita (0.121/0.100 campeón, 0.105/0.077 naïve-1, 32.1/31.3 CRPS);
    sin macro se hardcodeaban. Fuente: el manifiesto de gobernanza campeón--retador.
    """
    cc = json.loads((REPORTS / "governance" / "champion_challenger.json").read_text())
    out: dict = {}
    for tbl in ("FAD", "DFF"):
        d = cc[tbl]
        out[f"{tbl.lower()}_champion_mean"] = round(float(d["champion_mean"]), 3)
        out[f"crps_{tbl.lower()}"] = round(float(d["champion_crps"]), 1)
        for c in d.get("challengers", []):
            if c.get("challenger") == "naive1":
                out[f"naive1_{tbl.lower()}_dedup_mean"] = round(float(c["mean"]), 3)
                break
    return out


def _tuning() -> dict:
    """Conteo de grupos GBM cuyo tuneo se ACEPTÓ en val-confirmación (improved=true).

    La prosa de §2 (efecto de la optimización de hiperparámetros) cita "N de M grupos
    aceptados"; sin macro se hardcodeaba y quedó a un pelo de shipear "7/12" (era AQ)
    cuando la re-campaña del 6-jul aceptó 11/12 — el guardián era ciego porque el conteo
    NO era key_fact. Fuente: reports/eval/tuned_params.json (modelo -> tabla_bloque ->
    improved). Degradación C1: si el archivo falta (el cron semanal no re-tunea), conserva
    los conteos previos.
    """
    keys = ("tuning_groups", "tuning_accepted", "tuning_accepted_family", "tuning_accepted_employment")
    p = REPORTS / "eval" / "tuned_params.json"
    if not p.exists():
        prev = _prev_facts()
        return {k: prev[k] for k in keys if k in prev}
    tp = json.loads(p.read_text())
    groups = [
        (tb, info) for tbs in tp.values() for tb, info in tbs.items() if isinstance(info, dict) and "improved" in info
    ]
    acc = [g for g in groups if g[1].get("improved") is True]
    return {
        "tuning_groups": len(groups),
        "tuning_accepted": len(acc),
        "tuning_accepted_family": sum(1 for tb, _ in acc if tb.endswith("family")),
        "tuning_accepted_employment": sum(1 for tb, _ in acc if tb.endswith("employment")),
    }


def _census_significance() -> dict:
    """Cifras de significancia / censo / cobertura conforme que la prosa del deliverable tenía
    como LITERALES sin fuente: rango promedio de Friedman del naïve-1, censo de estacionariedad,
    cobertura de la inferencia conforme adaptativa (ACI), y en cuántas de las 25 series FAD el
    naïve-1 es el mejor modelo en hold-out. Ahora derivadas -> macro (de-hardcode de raíz)."""
    out: dict = {}
    sig = REPORTS / "eval" / "significance_summary.json"
    if sig.exists():
        rk = json.loads(sig.read_text()).get("ranking", {})
        for tbl in ("FAD", "DFF"):
            v = rk.get(tbl, {}).get("avg_rank", {}).get("naive1")
            if v is not None:
                out[f"friedman_rank_naive1_{tbl.lower()}"] = round(float(v), 2)
    eda = REPORTS / "eda" / "eda_facts.json"
    if eda.exists():
        ss = json.loads(eda.read_text()).get("stationarity_summary", {})
        for k in ("difference", "mixed"):
            if k in ss:
                out[f"stationarity_{k}"] = int(ss[k])
    cov = REPORTS / "eval" / "conformal_coverage.csv"
    if cov.exists():
        c = pd.read_csv(cov).set_index("table")["aci_coverage"]
        for tbl in ("FAD", "DFF"):
            if tbl in c.index:
                out[f"aci_coverage_{tbl.lower()}"] = round(float(c[tbl]), 2)
    mc = REPORTS / "eval" / "model_comparison_FAD21.csv"
    if mc.exists():
        d = pd.read_csv(mc)
        fam = d[d.category.isin(["F1", "F2A", "F2B", "F3", "F4"])]
        wins = fam.loc[fam.groupby(["country", "category"])["hold_mase"].idxmin(), "model"]
        out["naive1_fad_holdout_wins"] = int((wins == "naive1").sum())
    return out


def build() -> dict:
    facts = {
        "_source": "experiments/build_key_facts.py — NO editar a mano",
        **_dataset(),
        **_prospective(),
        **_models(),
        **_champion_challenger(),
        **_tuning(),
        **_census_significance(),
    }
    (REPORTS / "governance" / "key_facts.json").write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n")

    # macros LaTeX (\factNObs, \factProspMASE, …) — camelCase del key
    def macro(k: str) -> str:
        # Los nombres de comando LaTeX NO admiten dígitos (\factProspCov95 tipografiaba
        # "95" en el preámbulo -> "Missing \begin{document}"; lo cazó Overleaf al
        # estrenarse el \input). Cada dígito se deletrea.
        digits = {
            "0": "Zero",
            "1": "One",
            "2": "Two",
            "3": "Three",
            "4": "Four",
            "5": "Five",
            "6": "Six",
            "7": "Seven",
            "8": "Eight",
            "9": "Nine",
        }
        name = "fact" + "".join(w.capitalize() for w in k.split("_"))
        return "".join(digits.get(c, c) for c in name)

    lines = ["% Auto-generado por experiments/build_key_facts.py — \\input este archivo y usa \\factXxx.\n"]
    for k, v in facts.items():
        if k.startswith("_") or isinstance(v, list):
            continue
        # Las MASE (medias/medianas por serie) se emiten con 3 decimales para que la prosa
        # rinda 0.100/0.120 (no 0.1/0.12); el guardián compara numéricamente (0.120==0.12).
        vs = format(v, ".3f") if isinstance(v, float) and (k.endswith(("_mean", "_median")) or "mase" in k) else v
        lines.append(f"\\newcommand{{\\{macro(k)}}}{{{vs}}}\n")
        # AH1: variante formateada \factXxxFmt para los conteos de miles — la prosa
        # usa 27{,}611 (coma tipográfica LaTeX); el macro crudo era inutilizable ahí.
        if isinstance(v, int) and v >= 1000:
            lines.append(f"\\newcommand{{\\{macro(k)}Fmt}}{{{format(v, ',').replace(',', '{,}')}}}\n")
    (REPORTS / "latex" / "key_facts.tex").write_text("".join(lines))
    return facts


if __name__ == "__main__":
    f = build()
    print(json.dumps({k: v for k, v in f.items() if not k.startswith("_")}, indent=2, ensure_ascii=False))
