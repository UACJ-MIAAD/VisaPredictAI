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
import matplotlib.pyplot as plt  # noqa: E402

from vp_model import config, dataset, walkforward  # noqa: E402
from vp_model.plots import OUTDIR, UACJ_BLACK, UACJ_BLUE, _save  # noqa: E402

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
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    ax.plot(real.time_index, real.values().flatten() / config.DAYS_PER_YEAR, color=UACJ_BLACK, lw=1.5, label="real")
    ax.plot(
        fc.time_index,
        fc.values().flatten() / config.DAYS_PER_YEAR,
        color=UACJ_BLUE,
        lw=1.5,
        ls="--",
        label=f"{PRETTY[model_name]} (pronóstico)",
    )
    ax.set_title(f"Pronóstico vs. real en el hold-out — {country}/{category}/{table}")
    ax.set_xlabel("Mes del boletín")
    ax.set_ylabel("Años desde la época base")
    ax.legend(fontsize=8, frameon=False)
    return _save(fig, f"results_holdout_{country}_{category}_{table}.png")


_CC = {"mexico": "MX", "india": "IN", "china": "CN", "philippines": "PH", "all_chargeability": "RoW"}


def feature_tables_latex(table: str = "FAD", block: str = "family") -> str:
    """Dos tablas LaTeX con el catálogo de características de las 25 series piloto.

    Tabla A = estructura temporal; Tabla B = anomalías y forma de la distribución.
    stability/lumpiness se omiten (dependientes de escala; viven en el CSV).
    """
    from vp_model import series_characterization as feat

    ft = feat.feature_table(table=table, block=block).copy()
    ft["cc"] = ft["country"].map(_CC)
    ft = ft.sort_values(["cc", "category"])

    def _row(r, cols, fmts):
        cells = [r.cc, r.category] + [f.format(getattr(r, c)) for c, f in zip(cols, fmts, strict=True)]
        return " & ".join(cells) + r" \\"

    a_cols = ["trend_strength", "seasonal_strength", "spectral_entropy", "acf1", "acf1_diff", "ndiffs"]
    a_fmt = ["{:.3f}", "{:.3f}", "{:.3f}", "{:.3f}", "{:+.3f}", "{:d}"]
    b_cols = ["n_outliers", "ljung_box_p", "step_skew", "step_kurtosis"]
    b_fmt = ["{:d}", "{:.3f}", "{:+.2f}", "{:.1f}"]

    a = [
        r"\begin{table}[H]",
        r"\centering",
        r"\footnotesize",
        r"\caption[Características estructurales de las series]{Estructura temporal de "
        r"las 25 series FAD de preferencia familiar. $F_T$/$F_S$: fuerza de tendencia y "
        r"estacionalidad; $H$: entropía espectral; $d$: orden de diferenciación.}",
        r"\label{tab:features_estructura}",
        r"\begin{tabular}{llcccccc}",
        r"\hline",
        r"\rowcolor{uacjblue!15}",
        r"\textbf{Área} & \textbf{Cat.} & $\mathbf{F_T}$ & $\mathbf{F_S}$ & $\mathbf{H}$ & "
        r"\textbf{ACF1} & $\mathbf{ACF1_\Delta}$ & $\mathbf{d}$ \\ \hline",
    ]
    a += [_row(r, a_cols, a_fmt) for r in ft.itertuples()]
    a += [r"\hline", r"\end{tabular}", r"\end{table}", ""]

    b = [
        r"\begin{table}[H]",
        r"\centering",
        r"\footnotesize",
        r"\caption[Anomalías y forma de las series]{Anomalías y forma de la distribución "
        r"de los avances. Atípicos: residuos STL con $|z|>3$; LB$\,p$: p-valor de "
        r"Ljung-Box (H0 ruido blanco); asimetría y curtosis de los avances mensuales.}",
        r"\label{tab:features_anomalias}",
        r"\begin{tabular}{llcccc}",
        r"\hline",
        r"\rowcolor{uacjblue!15}",
        r"\textbf{Área} & \textbf{Cat.} & \textbf{Atípicos} & \textbf{LB}\,$p$ & "
        r"\textbf{Asim.} & \textbf{Curt.} \\ \hline",
    ]
    b += [_row(r, b_cols, b_fmt) for r in ft.itertuples()]
    b += [r"\hline", r"\end{tabular}", r"\end{table}", ""]
    return "\n".join(a + b)


def main() -> None:
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
