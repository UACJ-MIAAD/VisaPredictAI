"""Figuras publication-ready para el EDA (US-C1), paleta UACJ, tipografía serif.

Genera a ``reports/figures/eda_*.png`` las visualizaciones que referencia el .tex:
serie piloto, heatmap de cobertura, histograma de avances mensuales, y
descomposición STL + ACF/PACF de una serie representativa.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from statsmodels.graphics.tsaplots import plot_acf, plot_pacf  # noqa: E402
from statsmodels.tsa.seasonal import STL  # noqa: E402

from vp_model import config, dataset, eda, features, preprocess  # noqa: E402

log = config.get_logger(__name__)

UACJ_BLUE = "#003CA6"
UACJ_YELLOW = "#FFD600"
UACJ_GRAY = "#555559"
UACJ_BLACK = "#231F20"

OUTDIR = Path(__file__).resolve().parent.parent / "reports" / "figures"

plt.rcParams.update(
    {
        "font.family": "serif",
        "font.size": 10,
        "axes.edgecolor": UACJ_GRAY,
        "axes.labelcolor": UACJ_BLACK,
        "axes.titlecolor": UACJ_BLUE,
        "figure.dpi": 150,
    }
)

# 300 ppi (estándar de impresión) para .png nítidos. Explícito en cada savefig:
# statsmodels (STL/ACF) reescribe rcParams a mitad de proceso, así que no se confía en el rcParam.
SAVE_DPI = 300


def _save(fig: plt.Figure, name: str) -> Path:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    path = OUTDIR / name
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", dpi=SAVE_DPI)
    plt.close(fig)
    return path


def plot_pilot_series(table: str = "FAD", category: str = "F3") -> Path:
    """Una categoría a través de los 5 países piloto (heterogeneidad por país)."""
    fig, ax = plt.subplots(figsize=(8, 4.2))
    # UACJ_YELLOW es ilegible como línea fina sobre blanco; se reserva para chips/rellenos.
    colors = [UACJ_BLUE, "#C8A200", UACJ_GRAY, UACJ_BLACK, "#8A1538"]
    for country, color in zip(dataset.PILOT_COUNTRIES, colors, strict=True):
        try:
            s = dataset.load_series(country, category, table)
        except KeyError:
            continue
        ax.plot(s.index, s.to_numpy() / 365.25, label=country, color=color, lw=1.3)
    ax.set_title(f"Frente de fecha de prioridad — categoría {category}, tabla {table}")
    ax.set_xlabel("Mes del boletín")
    ax.set_ylabel("Años desde la época base (1975)")
    ax.legend(fontsize=8, frameon=False, ncol=3)
    return _save(fig, f"eda_pilot_{category}_{table}.png")


def plot_coverage_heatmap(table: str = "FAD", block: str = "family") -> Path:
    """Continuidad (% de meses con observación F) por país × categoría."""
    df = eda.profile_all(table=table, block=block)
    pivot = df.pivot(index="country", columns="category", values="continuity")
    fig, ax = plt.subplots(figsize=(7, 3.2))
    im = ax.imshow(pivot.to_numpy(), cmap="YlGnBu", vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(pivot.columns)), pivot.columns)
    ax.set_yticks(range(len(pivot.index)), pivot.index)
    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            v = pivot.to_numpy()[i, j]
            ax.text(j, i, f"{v:.0%}", ha="center", va="center", color="white" if v > 0.65 else UACJ_BLACK, fontsize=8)
    ax.set_title(f"Continuidad de las series — tabla {table}, bloque {block}")
    fig.colorbar(im, ax=ax, fraction=0.025, label="fracción de meses con fecha")
    return _save(fig, f"eda_coverage_{table}_{block}.png")


def plot_step_distribution(table: str = "FAD", block: str = "family") -> Path:
    """Histograma de avances mensuales (días/mes); la cola izquierda son retrogresiones."""
    steps = []
    for r in dataset.list_series(table=table, block=block).itertuples():
        s = dataset.load_series(r.country, r.category, r.table)
        steps.append(s.diff().dropna())
    allsteps = pd.concat(steps)
    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.hist(np.clip(allsteps, -200, 400), bins=60, color=UACJ_BLUE, alpha=0.85)
    ax.axvline(0, color="#8A1538", lw=1.2, ls="--", label="sin avance")
    ax.set_title(f"Distribución del avance mensual — tabla {table}, bloque {block}")
    ax.set_xlabel("Avance de la fecha de prioridad (días por mes, recortado a [-200, 400])")
    ax.set_ylabel("Frecuencia")
    ax.legend(fontsize=8, frameon=False)
    return _save(fig, f"eda_steps_{table}_{block}.png")


def plot_decomposition(country: str = "mexico", category: str = "F3", table: str = "FAD") -> Path:
    """STL de una serie representativa (tendencia + estacionalidad + residuo)."""
    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table)).dropna()
    res = STL(s, period=12, robust=True).fit()
    fig, axes = plt.subplots(4, 1, figsize=(8, 6.5), sharex=True)
    for ax, (comp, color) in zip(
        axes,
        [(s, UACJ_BLUE), (res.trend, UACJ_BLACK), (res.seasonal, UACJ_GRAY), (res.resid, "#8A1538")],
        strict=True,
    ):
        ax.plot(comp.index, np.asarray(comp) / 365.25, color=color, lw=1.0)
    for ax, lbl in zip(axes, ["observado", "tendencia", "estacional", "residuo"], strict=True):
        ax.set_ylabel(lbl, fontsize=8)
    axes[0].set_title(f"Descomposición STL — {country}/{category}/{table} (años)")
    axes[-1].set_xlabel("Mes del boletín")
    return _save(fig, f"eda_stl_{country}_{category}_{table}.png")


def plot_acf_pacf(country: str = "mexico", category: str = "F3", table: str = "FAD") -> Path:
    """ACF/PACF de la serie diferenciada (orienta el orden de ARIMA/SARIMA)."""
    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table)).dropna()
    d = s.diff().dropna()
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8, 3.2))
    plot_acf(d, ax=ax1, lags=24, color=UACJ_BLUE)
    plot_pacf(d, ax=ax2, lags=24, method="ywm", color=UACJ_BLUE)
    ax1.set_title("ACF (serie diferenciada)")
    ax2.set_title("PACF (serie diferenciada)")
    return _save(fig, f"eda_acfpacf_{country}_{category}_{table}.png")


def plot_feature_space(table: str = "FAD", block: str = "family") -> Path:
    """Espacio de características FPP3: fuerza de tendencia vs. estacionalidad por serie.

    Cada punto es una serie; la nube revela qué tan homogéneo es el panel (todas con
    tendencia fuerte y estacionalidad casi nula -> familia de modelos común).
    """
    ft = features.feature_table(table=table, block=block)
    fig, ax = plt.subplots(figsize=(7, 4.2))
    colors = {
        c: col
        for c, col in zip(
            dataset.PILOT_COUNTRIES, [UACJ_BLUE, "#C8A200", UACJ_GRAY, UACJ_BLACK, "#8A1538"], strict=True
        )
    }
    for country, grp in ft.groupby("country"):
        ax.scatter(
            grp["trend_strength"],
            grp["seasonal_strength"],
            label=country,
            color=colors.get(country, UACJ_BLUE),
            s=40,
            alpha=0.8,
        )
    ax.set_xlabel("Fuerza de tendencia $F_T$")
    ax.set_ylabel("Fuerza de estacionalidad $F_S$")
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.02, 1)
    ax.set_title(f"Espacio de características de las series — tabla {table}, bloque {block}")
    ax.legend(fontsize=8, frameon=False, ncol=3)
    return _save(fig, f"eda_features_{table}_{block}.png")


def plot_seasonal_subseries(country: str = "mexico", category: str = "F3", table: str = "FAD") -> Path:
    """Subseries estacional (FPP3 §2.4): avance medio por mes del calendario.

    Sobre el avance mensual (no el nivel, que tiene tendencia); revela si las fechas
    se mueven más en ciertos meses del año fiscal (cuotas que reinician en octubre).
    """
    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table))
    adv = s.diff().dropna()
    by_month = adv.groupby(adv.index.month).mean()
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.bar(by_month.index, by_month.to_numpy(), color=UACJ_BLUE, alpha=0.85)
    ax.axhline(adv.mean(), color="#8A1538", lw=1.2, ls="--", label="media global")
    ax.set_xticks(range(1, 13))
    ax.set_xlabel("Mes del calendario (año fiscal de visas inicia en octubre)")
    ax.set_ylabel("Avance medio (días)")
    ax.set_title(f"Subseries estacional del avance mensual — {country}/{category}/{table}")
    ax.legend(fontsize=8, frameon=False)
    return _save(fig, f"eda_subseries_{country}_{category}_{table}.png")


def plot_cross_correlation(category: str = "F3", table: str = "FAD") -> Path:
    """Co-movimiento entre áreas (estructura del panel): correlación de los avances."""
    cols = {}
    for country in dataset.PILOT_COUNTRIES:
        try:
            s = preprocess.to_regular_monthly(dataset.load_series(country, category, table))
            cols[country] = s.diff()
        except KeyError:
            continue
    corr = pd.DataFrame(cols).corr()
    fig, ax = plt.subplots(figsize=(5.2, 4.4))
    im = ax.imshow(corr.to_numpy(), cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(corr)), corr.columns, rotation=45, ha="right")
    ax.set_yticks(range(len(corr)), corr.index)
    for i in range(len(corr)):
        for j in range(len(corr)):
            ax.text(j, i, f"{corr.to_numpy()[i, j]:.2f}", ha="center", va="center", fontsize=8)
    ax.set_title(f"Co-movimiento de los avances entre áreas — {category}/{table}")
    fig.colorbar(im, ax=ax, fraction=0.046, label="correlación de Pearson")
    return _save(fig, f"eda_crosscorr_{category}_{table}.png")


def plot_rolling_volatility(country: str = "mexico", category: str = "F3", table: str = "FAD") -> Path:
    """Volatilidad móvil del avance (heteroscedasticidad a lo largo del tiempo)."""
    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table))
    adv = s.diff()
    roll = adv.rolling(12, min_periods=6).std()
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    ax.plot(roll.index, roll.to_numpy(), color=UACJ_BLUE, lw=1.3)
    ax.set_title(f"Volatilidad móvil (12 m) del avance mensual — {country}/{category}/{table}")
    ax.set_xlabel("Mes del boletín")
    ax.set_ylabel("Desv. estándar del avance (días)")
    return _save(fig, f"eda_volatility_{country}_{category}_{table}.png")


def plot_changepoints(country: str = "mexico", category: str = "F3", table: str = "FAD") -> Path:
    """Cambios de régimen (PELT) vs. anomalías puntuales (Hampel) sobre la serie.

    Distingue visualmente los desplazamientos estructurales (pocos, líneas verticales)
    de las anomalías puntuales (marcadores), lo que un z-score por punto confunde.
    """
    import ruptures as rpt

    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table)).dropna()
    x = ((s - s.mean()) / (s.std(ddof=0) or 1.0)).to_numpy()
    bkps = rpt.Pelt(model="rbf", min_size=6).fit(x).predict(pen=3.0 * np.log(len(x)))
    resid = STL(s, period=12, robust=True).fit().resid
    med = resid.rolling(13, center=True, min_periods=6).median()
    mad = (resid - med).abs().rolling(13, center=True, min_periods=6).median()
    anom = s[((resid - med).abs() > 3 * 1.4826 * mad) & (mad > 0)]

    fig, ax = plt.subplots(figsize=(8, 3.8))
    ax.plot(s.index, s.to_numpy() / 365.25, color=UACJ_BLUE, lw=1.2, label="serie")
    for b in bkps[:-1]:
        ax.axvline(s.index[min(b, len(s) - 1)], color="#8A1538", lw=1.1, ls="--")
    ax.scatter(
        anom.index,
        anom.to_numpy() / 365.25,
        color="#C8A200",
        s=18,
        zorder=3,
        label=f"anomalías puntuales ({len(anom)})",
    )
    ax.plot([], [], color="#8A1538", lw=1.1, ls="--", label=f"cambios de régimen ({len(bkps) - 1})")
    ax.set_title(f"Cambios de régimen y anomalías puntuales — {country}/{category}/{table}")
    ax.set_xlabel("Mes del boletín")
    ax.set_ylabel("Años desde la época base")
    ax.legend(fontsize=8, frameon=False)
    return _save(fig, f"eda_changepoints_{country}_{category}_{table}.png")


def plot_kalman_imputation(country: str = "china", category: str = "F1", table: str = "FAD") -> Path:
    """Manejo MNAR de huecos: observaciones reales vs. relleno por suavizado de Kalman."""
    from vp_model import missingness as miss

    raw = miss._raw_monthly(dataset.load_series(country, category, table))
    imp = miss.kalman_impute(raw)
    gaps = raw.isna()
    fig, ax = plt.subplots(figsize=(8, 3.4))
    ax.plot(imp.index, imp.to_numpy() / 365.25, color=UACJ_GRAY, lw=1.0, ls=":", label="Kalman (relleno EDA)")
    ax.scatter(raw.index[~gaps], raw[~gaps].to_numpy() / 365.25, color=UACJ_BLUE, s=8, label="observado (F)")
    ax.scatter(
        imp.index[gaps],
        imp[gaps].to_numpy() / 365.25,
        color="#8A1538",
        s=26,
        marker="x",
        label=f"hueco MNAR imputado ({int(gaps.sum())})",
    )
    ax.set_title(f"Huecos MNAR e imputación por Kalman — {country}/{category}/{table}")
    ax.set_xlabel("Mes del boletín")
    ax.set_ylabel("Años desde la época base")
    ax.legend(fontsize=8, frameon=False)
    return _save(fig, f"eda_kalman_{country}_{category}_{table}.png")


def generate_all() -> list[Path]:
    paths = [
        plot_pilot_series(),
        plot_coverage_heatmap("FAD", "family"),
        plot_coverage_heatmap("DFF", "family"),
        plot_step_distribution(),
        plot_decomposition(),
        plot_acf_pacf(),
        plot_feature_space("FAD", "family"),
        plot_seasonal_subseries(),
        plot_cross_correlation(),
        plot_rolling_volatility(),
        plot_changepoints(),
        plot_kalman_imputation(),
    ]
    for p in paths:
        log.info("figura -> %s", p.relative_to(OUTDIR.parent.parent))
    return paths


if __name__ == "__main__":
    generate_all()
