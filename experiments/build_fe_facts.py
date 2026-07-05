"""Catálogo versionado de ingeniería de características -> reports/fe/fe_facts.json.

La fuente única de la narrativa de FE + limpieza (AD10): las decisiones magistrales
(FE_DECISIONS + CLEANING_DECISIONS, derivadas del código, no prosa suelta), las
constantes con su porqué, la política de covariables por modelo, el ledger de
limpieza del build vigente y — AD5, la parte VIVA — la selección FRESH (FDR
Benjamini-Yekutieli) + des-redundancia mRMR del conjunto unido de características
de caracterización (descriptores + diagnósticos avanzados + catch22) contra la
dificultad real de pronóstico de cada serie (hold-out MASE del campeón elegido en
la región de selección, campaign pool canónico). Lo consumen la web (#fe), el
reporte fe_report.pdf y las tablas del .tex.

Reglas: 0 cifras a mano (config/código/pools); determinista (sin reloj).

Uso (ante):  ante/bin/python experiments/build_fe_facts.py    (o `make fe-facts`)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from vp_data.cleaning import CLEANING_DECISIONS, LEDGER_PATH
from vp_data.decisions_i18n import DECISIONS_EN
from vp_model import feature_select
from vp_model import series_characterization as sc
from vp_model.config import (
    ALPHA,
    BASE_EPOCH,
    COVARIATES,
    DAYS_PER_YEAR,
    DIFFERENCED,
    HOLDOUT,
    HYPERPARAMS,
    MAX_INTERPOLABLE_GAP,
    MIN_TRAIN,
    NEEDS_SCALING,
    SEASONAL_PERIOD,
)
from vp_model.feature_builder import FE_DECISIONS, FE_VERSION, FeatureBuilder

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.csv"
POOLS = {t: ROOT / "reports" / "campaign" / f"campaign_pool_{t}_family.csv" for t in ("FAD", "DFF")}
OUT = ROOT / "reports" / "fe" / "fe_facts.json"

# Gate: la selección debe cubrir un mínimo razonable de series con campaña
# (equivale al espíritu del gate C2 del EDA: no publicar un catálogo mutilado).
MIN_SELECTION_SERIES = 20


def _bilingual(d: dict) -> dict:
    """Decision + its EN title/rationale (keyed by id) so the web #fe section
    reads English from data instead of a hand-kept dict (AT5). Spanish is
    canonical; a decision without a translation ships es-only (guarded by test)."""
    en = DECISIONS_EN.get(d["id"], {})
    return {**d, "title_en": en.get("title"), "rationale_en": en.get("rationale")}


def _champion_difficulty() -> pd.DataFrame:
    """Una fila por serie con la dificultad real: hold-out MASE del campeón.

    El campeón se elige con sel_mase (región de selección, como en la campaña) y
    se reporta su hold_mase — la dificultad ALCANZADA, no la teórica.
    """
    rows = []
    for table, fp in POOLS.items():
        if not fp.exists():
            raise SystemExit(f"GATE FE: falta el pool canónico {fp} — no se publica catálogo sin procedencia")
        pool = pd.read_csv(fp)
        for (country, category), g in pool.groupby(["country", "category"]):
            champ = g.loc[g.sel_mase.idxmin()]
            rows.append(
                {
                    "country": country,
                    "category": category,
                    "table": table,
                    "champion": champ.model,
                    "hold_mase": float(champ.hold_mase),
                }
            )
    return pd.DataFrame(rows)


def _union_features(series_rows: pd.DataFrame) -> pd.DataFrame:
    """Conjunto UNIDO de características por serie (el que nombra el .tex §1.2.2):
    descriptores de pronóstico + diagnósticos avanzados + catch22."""
    from dataclasses import asdict

    out = []
    for i, r in enumerate(series_rows.itertuples(), 1):
        base = asdict(sc.features(r.country, r.category, r.table))
        adv = asdict(sc.advanced(r.country, r.category, r.table))
        c22 = sc.catch22_vector(r.country, r.category, r.table)
        rec = {k: v for k, v in {**base, **adv}.items() if k not in ("country", "category", "table")}
        out.append({**rec, **c22})
        if i % 10 == 0:
            print(f"  características {i}/{len(series_rows)}", file=sys.stderr)
    return pd.DataFrame(out, index=series_rows.index)


def _feature_selection() -> dict:
    """AD5: FRESH (FDR) + mRMR sobre el conjunto unido, vs dificultad del campeón."""
    diff = _champion_difficulty()
    if len(diff) < MIN_SELECTION_SERIES:
        raise SystemExit(f"GATE FE: solo {len(diff)} series con campaña (< {MIN_SELECTION_SERIES})")
    x = _union_features(diff)
    sel = feature_select.select(x, diff["hold_mase"])
    return {
        "target": "hold-out MASE del campeón por serie (elegido con sel_mase; campaign pool canónico)",
        "n_series": int(len(diff)),
        "n_features_in": int(x.select_dtypes("number").shape[1]),
        "alpha_fdr": ALPHA,
        "n_relevant": len(sel.relevant),
        "n_selected": len(sel.selected),
        "relevant": sel.relevant,
        "selected": sel.selected,
        "dropped_redundant": sel.dropped_redundant,
    }


def build() -> dict:
    df = pd.read_csv(PANEL, parse_dates=["bulletin_date"])
    ledger_fp = ROOT / LEDGER_PATH
    ledger = json.loads(ledger_fp.read_text()) if ledger_fp.exists() else None
    if ledger is None:
        raise SystemExit("GATE FE: falta cleaning_ledger.json — correr python -m pipeline.build_panel")

    lags = HYPERPARAMS["trees"]["lags"]
    facts = {
        "_source": "experiments/build_fe_facts.py — NO editar a mano",
        "vintage": df.bulletin_date.max().strftime("%Y-%m"),
        "fe_version": FE_VERSION,
        "constants": {
            "base_epoch": BASE_EPOCH,
            "days_per_year": DAYS_PER_YEAR,
            "seasonal_period": SEASONAL_PERIOD,
            "max_interpolable_gap": MAX_INTERPOLABLE_GAP,
            "lags": lags,
            "min_train": MIN_TRAIN,
            "holdout": HOLDOUT,
        },
        "covariates": {m: list(c) for m, c in COVARIATES.items()},
        "differenced_models": sorted(DIFFERENCED),
        "scaled_models": sorted(NEEDS_SCALING),
        "realized_example": FeatureBuilder("xgboost").realized(),
        "fe_decisions": [_bilingual(d) for d in FE_DECISIONS],
        "cleaning_decisions": [_bilingual(d) for d in CLEANING_DECISIONS],
        "cleaning_ledger": {k: v for k, v in ledger.items() if not k.startswith("_")},
        "feature_selection": _feature_selection(),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(facts, indent=1, ensure_ascii=False) + "\n")

    # Macros LaTeX (patrón key_facts.tex): la nota de FE del .tex cita la selección
    # sin tipear cifras a mano; una re-campaña actualiza las macros, no la prosa.
    fs = facts["feature_selection"]
    sel_name = (fs["selected"][0] if fs["selected"] else "—").replace("_", r"\_")
    tex = [
        "% Auto-generado por experiments/build_fe_facts.py — \\input este archivo y usa \\feFactXxx.\n",
        f"\\newcommand{{\\feFactVersion}}{{{facts['fe_version']}}}\n",
        f"\\newcommand{{\\feFactSelIn}}{{{fs['n_features_in']}}}\n",
        f"\\newcommand{{\\feFactSelRelevant}}{{{fs['n_relevant']}}}\n",
        f"\\newcommand{{\\feFactSelFinal}}{{{fs['n_selected']}}}\n",
        f"\\newcommand{{\\feFactSelSeries}}{{{fs['n_series']}}}\n",
        f"\\newcommand{{\\feFactSelName}}{{{sel_name}}}\n",
        f"\\newcommand{{\\feFactNCleanDecisions}}{{{len(facts['cleaning_decisions'])}}}\n",
        f"\\newcommand{{\\feFactNFeDecisions}}{{{len(facts['fe_decisions'])}}}\n",
    ]
    (ROOT / "reports" / "latex" / "fe_facts.tex").write_text("".join(tex))
    return facts


if __name__ == "__main__":
    facts = build()
    fs = facts["feature_selection"]
    print(
        f"fe_facts OK — vintage {facts['vintage']} · builder v{facts['fe_version']} · "
        f"{len(facts['fe_decisions'])} decisiones FE + {len(facts['cleaning_decisions'])} de limpieza · "
        f"selección: {fs['n_features_in']} características -> {fs['n_relevant']} relevantes -> "
        f"{fs['n_selected']} finales ({fs['n_series']} series)"
    )
