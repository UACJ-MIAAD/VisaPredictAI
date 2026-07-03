"""Emite las filas LaTeX de las tablas de 21 modelos del entregable desde los pools.

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
        name = DISPLAY.get(str(model), str(model))
        if table == "DFF" and r.n == 0:
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


if __name__ == "__main__":
    for t in ("FAD", "DFF"):
        print(rows(t))
        print()
