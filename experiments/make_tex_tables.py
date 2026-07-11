"""Emite las filas LaTeX de las tablas de 24 modelos del entregable desde los pools.

Fuente: ``reports/campaign/campaign_pool_{FAD,DFF}_family.csv`` (última corrida por
``run_id``). Reproduce la agregación del texto: media de MASE/sMAPE de selección y de
hold-out sobre las 25 series piloto de familia; en DFF se reporta además ``n`` (series
donde el modelo ajustó) y se omite ARIMA-LSTM si no convergió en ninguna.

Uso:  ante/bin/python experiments/make_tex_tables.py
La salida son las filas listas para pegar entre ``\\midrule`` y ``\\bottomrule`` de
``tab:comparacion_modelos`` (FAD) y ``tab:comparacion_dff`` (DFF) en el ``.tex`` —
el encabezado/caption no cambian, por eso no se emiten.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

REPORTS = Path(__file__).resolve().parents[1] / "reports"

DISPLAY = {
    "theta": "Theta",
    "ets": "ETS (damped)",
    "catboost": "CatBoost",
    "sarima": "SARIMA",
    "arima": "ARIMA",
    "nlinear": "NLinear",
    "xgboost": "XGBoost",
    "kalman": "Kalman",
    "lightgbm": "LightGBM",
    "chronos": "Chronos (zero-shot)",
    "arima_lstm": "ARIMA-LSTM",
    "dlinear": "DLinear",
    "tide": "TiDE",
    "nhits": "N-HiTS",
    "rlinear": "RLinear (ridge)",
    "prophet": "Prophet",
    "nbeats": "N-BEATS",
    "naive": "Naïve estacional",
    "naive1": "Naïve-1 (paseo aleatorio)",
    "drift": "Deriva (\\textit{drift})",
    "llt": "Tendencia lineal local (LLT)",
    "lstm": "LSTM",
    "deepar": "DeepAR",
    "tft": "TFT",
}


def latest_pool(table: str) -> pd.DataFrame:
    df = pd.read_csv(REPORTS / "campaign" / f"campaign_pool_{table}_family.csv")
    return df[df.run_id == df.run_id.max()]


def rows(table: str) -> str:
    pool = latest_pool(table)
    n_series = pool.groupby("model").country.count().max()
    agg = pool.groupby("model").agg(
        sel_mase=("sel_mase", "mean"),
        hold_mase=("hold_mase", "mean"),
        sel_smape=("sel_smape", "mean"),
        hold_smape=("hold_smape", "mean"),
        n=("hold_mase", "count"),
    )
    agg = agg.sort_values("sel_mase")
    out: list[str] = []
    best = True  # la primera fila (mejor MASE sel.) va en negritas, como en el texto
    for model, r in agg.iterrows():
        name = DISPLAY.get(str(model), str(model).replace("_", r"\_"))
        if r.n == 0:
            continue  # no convergió en ninguna serie: se omite (nota en el caption)
        sel, hold = f"{r.sel_mase:.3f}", f"{r.hold_mase:.3f}"
        if best:
            sel, hold = f"\\textbf{{{sel}}}", f"\\textbf{{{hold}}}"
            best = False
        cells = [name, sel, hold, f"{r.sel_smape:.2f}", f"{r.hold_smape:.2f}"]
        if table == "DFF":
            cells.append(f"{int(r.n)}")
        out.append(" & ".join(cells) + " \\\\")
    header = f"% --- {table}: {n_series} series familia, run {pool.run_id.iloc[0]} ---"
    return "\n".join([header, *out])


def _eda_facts() -> dict:
    import json

    return json.loads((REPORTS / "eda" / "eda_facts.json").read_text())


def eda_desc_rows() -> str:
    """Filas de la tabla descriptiva del panel (tab:eda_descriptiva) desde el censo.

    Una fila por bloque×tabla: series, obs. F, % F, avance mensual (mediana y P10-P90),
    % congelado y % retrogresión. Derivado 100% de eda_facts.json (0 a mano).
    """
    facts = _eda_facts()
    census = pd.DataFrame(facts["series"])
    ev = pd.DataFrame(facts["retro_events"])
    blk = {"family": "Familiar", "employment": "Empleo"}
    out = [f"% --- tab:eda_descriptiva: censo vintage {facts['vintage']} (make_tex_tables.py) ---"]
    for block in ("family", "employment"):
        for table in ("FAD", "DFF"):
            g = census[(census.block == block) & (census.table == table)]
            n_f = int(g.n_F.sum())
            pct_f = 100 * n_f / int(g.n_total.sum())
            med_step = g[g.n_F > 0].median_step_days.median()
            frozen = 100 * g[g.n_F > 0].pct_frozen.mean()
            n_retro = int(len(ev[(ev.block == block) & (ev.table == table)]))
            cells = [
                f"{blk[block]} & {table}",
                str(len(g)),
                f"{n_f:,}".replace(",", r"\,"),
                f"{pct_f:.0f}\\,\\%",
                f"{med_step:.0f}",
                f"{frozen:.0f}\\,\\%",
                str(n_retro),
            ]
            out.append(" & ".join(cells) + " \\\\")
    return "\n".join(out)


def eda_stationarity_rows() -> str:
    """Filas del censo de estacionariedad (tab:eda_estacionariedad): 25 FAD familiares.

    ADF/KPSS/DF-GLS + veredicto + Ljung-Box, desde eda_facts.json. Las 49 evaluables
    restantes comparten el veredicto (la prosa lo dice); la tabla muestra el piloto.
    """
    facts = _eda_facts()
    census = pd.DataFrame(facts["series"])
    g = census[(census.block == "family") & (census.table == "FAD") & census.verdict.notna()]
    name_es = {
        "mexico": "México",
        "india": "India",
        "china": "China",
        "philippines": "Filipinas",
        "all_chargeability": "Resto del mundo",
    }
    verdict_es = {"difference": "diferenciar", "stationary": "estacionaria", "mixed": "mixto", "failed": "n/d"}
    out = [f"% --- tab:eda_estacionariedad: censo vintage {facts['vintage']} (make_tex_tables.py) ---"]
    order = {c: i for i, c in enumerate(name_es)}
    g = g.sort_values(["country", "category"], key=lambda s: s.map(order) if s.name == "country" else s)
    for r in g.itertuples():
        kpss = "$<$0.01" if r.kpss_p <= 0.01 else f"{r.kpss_p:.2f}"
        lb = "$<$0.001" if r.lb_p < 0.001 else f"{r.lb_p:.3f}"
        cells = [
            f"{name_es[r.country]} {r.category}",
            f"{r.adf_p:.2f}",
            kpss,
            f"{r.dfgls_p:.2f}",
            lb,
            verdict_es[str(r.verdict)],
        ]
        out.append(" & ".join(cells) + " \\\\")
    return "\n".join(out)


# --- E2/#27: tablas de caracterización (antes hand-built; fuente de 2 de los 6
# errores del audit ciego 7-jul). Derivadas de vp_model.series_characterization
# sobre las 25 series piloto FAD familiares — equivalencia al decimal verificada
# contra los valores corregidos del deliverable antes del reemplazo. ---
_AREA = {"china": "CN", "india": "IN", "mexico": "MX", "philippines": "PH", "all_chargeability": "RoW"}
_AREA_ORDER = ["CN", "IN", "MX", "PH", "RoW"]
_CAT_ORDER = ["F1", "F2A", "F2B", "F3", "F4"]


def _pilot_features() -> pd.DataFrame:
    from vp_model.series_characterization import feature_table

    df = feature_table("FAD", "family").copy()
    df["area"] = df["country"].map(_AREA)
    df = df[df["area"].notna()]
    df["_a"] = df["area"].map(_AREA_ORDER.index)
    df["_c"] = df["category"].map(_CAT_ORDER.index)
    return df.sort_values(["_a", "_c"])


def features_estructura_rows() -> str:
    """Filas de tab:features_estructura (F_T, F_S, H, ACF1, ACF1_Δ, d)."""
    out = ["% --- tab:features_estructura: vp_model.series_characterization (make_tex_tables.py) ---"]
    for r in _pilot_features().itertuples():
        out.append(
            f"{r.area} & {r.category} & {r.trend_strength:.3f} & {r.seasonal_strength:.3f} "
            f"& {r.spectral_entropy:.3f} & {r.acf1:.3f} & ${r.acf1_diff:+.3f}$ & {r.ndiffs} \\\\"
        )
    return "\n".join(out)


def features_anomalias_rows() -> str:
    """Filas de tab:features_anomalias (atípicos STL, Ljung-Box p, asimetría, curtosis)."""
    out = ["% --- tab:features_anomalias: vp_model.series_characterization (make_tex_tables.py) ---"]
    for r in _pilot_features().itertuples():
        out.append(
            f"{r.area} & {r.category} & {r.n_outliers} & {r.ljung_box_p:.3f} "
            f"& ${r.step_skew:+.2f}$ & {r.step_kurtosis:.1f} \\\\"
        )
    return "\n".join(out)


if __name__ == "__main__":
    for t in ("FAD", "DFF"):
        print(rows(t))
        print()
    print(eda_desc_rows())
    print()
    print(eda_stationarity_rows())
    print()
    print(features_estructura_rows())
    print()
    print(features_anomalias_rows())
