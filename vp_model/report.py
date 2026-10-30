"""Artefactos de resultados para el .tex de Proyecto I (US-F1, US-F2, US-F5).

Lee ``reports/eval/model_comparison.csv`` y produce: (1) una tabla LaTeX con el ranking de
modelos, (2) la figura predicho-vs-real sobre el hold-out del modelo ganador, y (3)
el mapa de ganador por serie (evidencia de H2, heterogeneidad por país-categoría).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from vp_model import config, dataset, walkforward
from vp_model.palette import FONT_RC, GRAY, GRID, INK, LIGHT, MID, register_fonts, series_label
from vp_model.plots import OUTDIR, UACJ_BLACK, UACJ_BLUE, _save

log = config.get_logger(__name__)

CSV = Path(__file__).resolve().parent.parent / "reports" / "eval" / "model_comparison.csv"
TEX = Path(__file__).resolve().parent.parent / "reports" / "eval" / "results_table.tex"

# Nombres legibles para el .tex.
PRETTY = {
    "naive": "Naïve estacional",
    "arima": "ARIMA",
    "sarima": "SARIMA",
    "prophet": "Prophet",
    "ets": "ETS (damped)",
    "theta": "Theta",
    "kalman": "Kalman",
    "lstm": "LSTM",
    "deepar": "DeepAR",
    "arima_lstm": "ARIMA-LSTM",
    "dlinear": "DLinear",
    "nlinear": "NLinear",
    "nbeats": "N-BEATS",
    "nhits": "N-HiTS",
    "tide": "TiDE",
    "rlinear": "RLinear (ridge)",
    "xgboost": "XGBoost",
    "lightgbm": "LightGBM",
    "catboost": "CatBoost",
    "tft": "TFT",
    "chronos": "Chronos (zero-shot)",
}


#: Rótulo del modelo DENTRO de la figura (español). La tabla conserva `PRETTY`, que comparte con `make_tex_tables`.
FIG_LABEL = {**PRETTY, "ets": "ETS amortiguado"}

#: Ancho impreso en el `.tex` (0.6 del ancho de texto = 3.60 in): con `tight_layout` + recorte ``tight`` el PNG natural
#: sale al mismo ancho que la figura, así que la letra se imprime a su tamaño real (antes, 7.5 in impresos al 49 %).
HOLDOUT_FIG_IN = (3.60, 2.45)

_MES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

#: Marco de imprenta de la figura: tipografía serif del proyecto y lienzo blanco de la paleta, aplicado de forma
#: reversible (no se toca el rcParams global del proceso).
_HOLDOUT_RC = {
    **FONT_RC,
    "font.size": 8,
    "figure.facecolor": LIGHT["PAPER"],
    "axes.facecolor": LIGHT["PAPER"],
    "savefig.facecolor": LIGHT["PAPER"],
    "axes.edgecolor": MID,
    "axes.labelcolor": INK,
    "axes.titlecolor": UACJ_BLUE,
    "xtick.color": GRAY,
    "ytick.color": GRAY,
    "text.color": INK,
    "axes.spines.top": False,
    "axes.spines.right": False,
}


def ranking(df: pd.DataFrame) -> pd.DataFrame:
    """Promedio de métricas por modelo, ordenado por MASE de selección.

    B2: pseudo-réplicas del corte mundial colapsadas antes de promediar.
    """
    if {"country", "category"} <= set(df.columns):
        from vp_model import significance

        df, n_raw, n_eff = significance.dedup_series(df, value="hold_mase")
        if n_eff < n_raw:
            print(f"[ranking] dedup pseudo-réplicas: {n_raw} series -> {n_eff} efectivas")
    agg = (
        df.groupby("model")[["sel_mase", "hold_mase", "sel_smape", "hold_smape"]]
        .mean()
        .sort_values("sel_mase")
        .round(3)
    )
    agg.index = [PRETTY.get(m, m) for m in agg.index]
    return agg


def winner_per_series(df: pd.DataFrame) -> pd.DataFrame:
    """Modelo ganador (menor MASE de selección) por serie — evidencia de H2."""
    idx = df.groupby(["country", "category", "table"])["sel_mase"].idxmin()
    w = df.loc[idx, ["country", "category", "table", "model", "sel_mase", "hold_mase"]]
    return w.reset_index(drop=True)


def latex_table(df: pd.DataFrame) -> str:
    """Tabla LaTeX del ranking, en el estilo del proyecto (sin paquetes nuevos)."""
    rank = ranking(df)
    lines = [
        r"\begin{table}[H]",
        r"\centering",
        r"\caption[Comparación de modelos]{Desempeño promedio de los modelos por "
        r"validación walk-forward sobre las series piloto. MASE escalada por el "
        r"naïve estacional; menor es mejor.}",
        r"\label{tab:comparacion_modelos}",
        r"\begin{tabular}{lcccc}",
        r"\hline",
        r"\rowcolor{uacjblue!15}",
        r"\textbf{Modelo} & \textbf{MASE sel.} & \textbf{MASE hold-out} & "
        r"\textbf{sMAPE sel.} & \textbf{sMAPE hold-out} \\ \hline",
    ]
    for name, row in rank.iterrows():
        lines.append(
            f"{name} & {row.sel_mase:.3f} & {row.hold_mase:.3f} & {row.sel_smape:.2f} & {row.hold_smape:.2f} \\\\"
        )
    lines += [r"\hline", r"\end{tabular}", r"\end{table}", ""]
    return "\n".join(lines)


def plot_winner_holdout(country: str, category: str, table: str, model_name: str) -> Path:
    """Predicho vs. real sobre el hold-out de 24 meses del modelo ganador (US-F2)."""
    from vp_model import models
    from vp_model.feature_builder import FeatureBuilder

    raw = dataset.load_series(country, category, table)
    ts = models.to_timeseries(raw)
    split = ts.time_index[-walkforward.HOLDOUT]
    m = models.build_model(model_name, table=table)  # tuned per-table params for GBMs (Wave-1)
    extra = {}
    # F1: política de covariables por modelo vía FeatureBuilder (calendario + máscaras
    # MNAR desde la serie F cruda) — el calendario inline previo no llevaba máscaras.
    cov = FeatureBuilder(model_name).covariates(ts, raw)
    if cov is not None:
        extra["future_covariates"] = cov
    fc = m.historical_forecasts(  # type: ignore[attr-defined]
        ts,
        start=split,
        forecast_horizon=1,
        stride=1,
        retrain=(model_name in walkforward.RETRAIN_EACH_STEP),
        last_points_only=True,
        verbose=False,
        **extra,
    )
    real = ts.slice_intersect(fc)
    return _draw_holdout(real, fc, country, category, table, model_name)


def _draw_holdout(real, fc, country: str, category: str, table: str, model_name: str) -> Path:
    """Dibuja (sin recalcular nada) las dos curvas del hold-out a su ancho impreso, en español y con la paleta."""
    import matplotlib.dates as mdates
    import matplotlib.ticker as mticker

    register_fonts()
    with plt.rc_context(_HOLDOUT_RC):
        fig, ax = plt.subplots(figsize=HOLDOUT_FIG_IN)
        ax.plot(
            real.time_index,
            real.values().flatten() / config.DAYS_PER_YEAR,
            color=UACJ_BLACK,
            lw=1.3,
            label="valor real",
        )
        ax.plot(
            fc.time_index,
            fc.values().flatten() / config.DAYS_PER_YEAR,
            color=UACJ_BLUE,
            lw=1.3,
            ls="--",
            label=f"{FIG_LABEL[model_name]} (pronóstico)",
        )
        ax.set_title(
            f"Pronóstico frente a valor real en el conjunto reservado\n{series_label(f'{country}/{category}/{table}')}",
            fontsize=9,
            linespacing=1.25,
        )
        ax.set_xlabel("Mes del boletín", fontsize=8)
        ax.set_ylabel("Años desde la época base", fontsize=8)
        # Un solo formato de mes en español ('ene 2025', 'jul 2025'), no '2025-01'.
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
        ax.xaxis.set_major_formatter(
            mticker.FuncFormatter(lambda v, _: f"{_MES[mdates.num2date(v).month - 1]} {mdates.num2date(v).year}")
        )
        ax.tick_params(labelsize=7, length=2.5)
        ax.grid(axis="y", color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        ax.legend(fontsize=7, frameon=False, loc="lower right")
        return _save(fig, f"results_holdout_{country}_{category}_{table}.png")


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--figure-only", metavar="MODELO", help="sólo la figura de hold-out (México/F3/FAD) de ese modelo")
    args = ap.parse_args(argv)
    if args.figure_only:  # G7 · B2: la reconciliación regenera la figura sin reescribir la tabla `.tex`
        p = plot_winner_holdout("mexico", "F3", "FAD", args.figure_only)
        log.info("Figura hold-out -> %s", p)
        return
    df = pd.read_csv(CSV)
    log.info("Ranking de modelos (promedio sobre series):\n%s", ranking(df).to_string())
    log.info("Ganador por serie (H2):\n%s", winner_per_series(df).to_string(index=False))
    TEX.write_text(latex_table(df))
    log.info("Tabla LaTeX -> %s", TEX.relative_to(TEX.parent.parent))
    # Figura del ganador global sobre una serie representativa.
    best = ranking(df).index[0]
    best_key = next(k for k, v in PRETTY.items() if v == best)
    p = plot_winner_holdout("mexico", "F3", "FAD", best_key)
    log.info("Figura hold-out -> %s", p.relative_to(OUTDIR.parent.parent))


if __name__ == "__main__":
    main()
