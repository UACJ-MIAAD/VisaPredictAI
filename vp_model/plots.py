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
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
from statsmodels.graphics.tsaplots import plot_acf, plot_pacf
from statsmodels.tsa.seasonal import STL

from vp_model import config, dataset, eda, preprocess
from vp_model import series_characterization as features
from vp_model.config import BASE_EPOCH, DAYS_PER_YEAR
from vp_model.palette import (
    BLUE,
    COUNTRY,
    COUNTRY_NAME,
    DIV,
    FONT_RC,
    GOLD,
    GRAY,
    INK,
    MID,
    SEQ,
    WINE,
    YELLOW,
    register_fonts,
    series_label,
)

_BLOQUE = {"family": "familiar", "employment": "empleo"}

log = config.get_logger(__name__)

# Aliases: el cuerpo histórico usa los nombres UACJ_*; apuntan a la paleta canónica.
UACJ_BLUE, UACJ_YELLOW, UACJ_GRAY, UACJ_BLACK = BLUE, YELLOW, GRAY, INK  # AE3: sin hex suelto

OUTDIR = Path(__file__).resolve().parent.parent / "reports" / "figures"

register_fonts()
plt.rcParams.update(
    {
        **FONT_RC,
        "font.size": 10,
        "axes.edgecolor": MID,
        "axes.labelcolor": INK,
        "axes.titlecolor": BLUE,
        "figure.dpi": 150,
    }
)

# 300 ppi (estándar de impresión) para .png nítidos. Explícito en cada savefig:
# statsmodels (STL/ACF) reescribe rcParams a mitad de proceso, así que no se confía en el rcParam.
SAVE_DPI = 300

# Los «años» de estas figuras son años transcurridos desde la época base t0 del panel
# (config.BASE_EPOCH), no años calendario: el rótulo lo dice y se deriva de la constante.
_MES_ABR = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_t0 = pd.Timestamp(BASE_EPOCH)
T0_TXT = f"$t_0$ = {_t0.day}-{_MES_ABR[_t0.month - 1]}-{_t0.year}"
Y_EPOCH = f"Años desde la época base\n({T0_TXT})"


def _save(fig: plt.Figure, name: str) -> Path:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    path = OUTDIR / name
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", dpi=SAVE_DPI)
    # Audit 3-jul (H2): el .tex referencia estas figuras desde reports/latex/Figures;
    # antes se copiaban a mano y el cron refrescaba solo eda2_/eda3_ — el capítulo
    # mezclaba vintages. Doble escritura: la copia local (gitignored) y la del .tex.
    tex_dir = OUTDIR.parent / "latex" / "Figures"
    tex_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(tex_dir / name, bbox_inches="tight", dpi=SAVE_DPI)
    plt.close(fig)
    return path


def plot_pilot_series(table: str = "FAD", category: str = "F3") -> Path:
    """Una categoría a través de los 5 países piloto (heterogeneidad por país)."""
    fig, ax = plt.subplots(figsize=(5.75, 3.1))  # 0.92 del ancho de texto: tipografía a tamaño real
    # UACJ_YELLOW es ilegible como línea fina sobre blanco; se reserva para chips/rellenos.
    # India, China y Resto del mundo comparten casi el mismo frente en F3: el Resto va ancho y
    # translúcido DEBAJO y India/China encima con trazo propio, para que las tres se distingan.
    styles = {
        "all_chargeability": {"lw": 3.2, "alpha": 0.45, "ls": "-", "zorder": 1},
        "india": {"lw": 1.3, "alpha": 1.0, "ls": (0, (5, 2)), "zorder": 3},
        "china": {"lw": 1.5, "alpha": 1.0, "ls": (0, (1, 1.4)), "zorder": 4},
    }
    for country in dataset.PILOT_COUNTRIES:
        try:
            s = dataset.load_series(country, category, table)
        except KeyError:
            continue
        st = styles.get(country, {"lw": 1.2, "alpha": 1.0, "ls": "-", "zorder": 2})
        ax.plot(
            s.index,
            s.to_numpy() / DAYS_PER_YEAR,
            label=COUNTRY_NAME[country],
            color=COUNTRY[country],
            lw=st["lw"],
            alpha=st["alpha"],
            ls=st["ls"],
            zorder=st["zorder"],
        )
    ax.set_title(f"Frente de fecha de prioridad, categoría {category} · tabla {table}", fontsize=10)
    ax.set_xlabel("Mes del boletín", fontsize=9)
    ax.set_ylabel("Años desde la época base (1975)", fontsize=9)
    ax.tick_params(labelsize=8.5)
    ax.legend(fontsize=8, frameon=False, ncol=3, handlelength=2.6, loc="upper left")
    return _save(fig, f"eda_pilot_{category}_{table}.png")


def plot_coverage_heatmap(table: str = "FAD", block: str = "family") -> Path:
    """Continuidad (% de meses con observación F) por país × categoría."""
    df = eda.profile_all(table=table, block=block)
    pivot = df.pivot(index="country", columns="category", values="continuity")
    fig, ax = plt.subplots(figsize=(7, 3.2))
    im = ax.imshow(pivot.to_numpy(), cmap=SEQ, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(pivot.columns)), pivot.columns)
    ax.set_yticks(range(len(pivot.index)), [COUNTRY_NAME.get(c, c) for c in pivot.index])
    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            v = pivot.to_numpy()[i, j]
            ax.text(j, i, f"{v:.0%}", ha="center", va="center", color="white" if v > 0.65 else UACJ_BLACK, fontsize=8)
    ax.set_title(f"Continuidad de las series, tabla {table} · bloque {_BLOQUE.get(block, block)}")
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
    # AC1: nada de np.clip — recortar apilaba los saltos extremos (retrogresiones
    # >8 años REALES) en los bins de borde, winsorizando visualmente el fenómeno
    # que la tesis argumenta. Se grafica el rango central y se ANOTA lo excluido.
    lo, hi = -200, 400
    n_lo = int((allsteps < lo).sum())
    n_hi = int((allsteps > hi).sum())
    ax.hist(allsteps[(allsteps >= lo) & (allsteps <= hi)], bins=60, color=UACJ_BLUE, alpha=0.85)
    ax.axvline(0, color=WINE, lw=1.2, ls="--", label="sin avance")
    if n_lo:
        ax.text(
            0.02,
            0.94,
            f"← {n_lo} retrocesos < {lo} d\n(máx {-allsteps.min():,.0f} d)",
            transform=ax.transAxes,
            fontsize=7.5,
            color=WINE,
            va="top",
        )
    if n_hi:
        ax.text(
            0.98,
            0.94,
            f"{n_hi} avances > {hi} d →\n(máx {allsteps.max():,.0f} d)",
            transform=ax.transAxes,
            fontsize=7.5,
            color=UACJ_GRAY,
            va="top",
            ha="right",
        )
    ax.set_title(f"Distribución del avance mensual, tabla {table} · bloque {_BLOQUE.get(block, block)}")
    ax.set_xlabel(f"Avance de la fecha de prioridad (días/mes; rango [{lo}, {hi}], extremos anotados)")
    ax.set_ylabel("Frecuencia")
    ax.legend(fontsize=8, frameon=False)
    return _save(fig, f"eda_steps_{table}_{block}.png")


def plot_decomposition(country: str = "mexico", category: str = "F3", table: str = "FAD") -> Path:
    """STL de una serie representativa (tendencia + estacionalidad + residuo)."""
    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table)).dropna()
    res = STL(s, period=12, robust=True).fit()
    # Se imprime a 0.72 del ancho de texto (4.32 in): se dibuja a ese ancho para que el
    # texto salga 1:1 y no a 4 pt.
    fig, axes = plt.subplots(4, 1, figsize=(4.32, 5.0), sharex=True)
    for ax, (comp, color) in zip(
        axes,
        [(s, UACJ_BLUE), (res.trend, UACJ_BLACK), (res.seasonal, UACJ_GRAY), (res.resid, WINE)],
        strict=True,
    ):
        ax.plot(comp.index, np.asarray(comp) / DAYS_PER_YEAR, color=color, lw=0.9)
        ax.tick_params(labelsize=7.5)
    # observado y tendencia son NIVELES (años desde t0); estacional y residuo, desviaciones en años
    for ax, lbl in zip(
        axes,
        ["observado\n(años desde $t_0$)", "tendencia\n(años desde $t_0$)", "estacional\n(años)", "residuo\n(años)"],
        strict=True,
    ):
        ax.set_ylabel(lbl, fontsize=8)
    fig.align_ylabels(axes)
    axes[0].set_title(
        f"Descomposición STL, {series_label(f'{country}/{category}/{table}')}\n({T0_TXT}, época base)",
        fontsize=9,
    )
    axes[-1].set_xlabel("Mes del boletín", fontsize=8.5)
    return _save(fig, f"eda_stl_{country}_{category}_{table}.png")


def plot_acf_pacf(country: str = "mexico", category: str = "F3", table: str = "FAD") -> Path:
    """ACF/PACF de la serie diferenciada (orienta el orden de ARIMA/SARIMA)."""
    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table)).dropna()
    d = s.diff().dropna()
    # Impresa a 0.85 del ancho de texto (5.10 in): se dibuja a ese ancho (texto 1:1).
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(5.10, 2.5))
    stems = {"colors": UACJ_BLUE, "linewidth": 1.0}
    plot_acf(d, ax=ax1, lags=24, color=UACJ_BLUE, markersize=3.5, vlines_kwargs=stems)
    plot_pacf(d, ax=ax2, lags=24, method="ywm", color=UACJ_BLUE, markersize=3.5, vlines_kwargs=stems)
    ax1.set_title("ACF (serie diferenciada)", fontsize=9)
    ax2.set_title("PACF (serie diferenciada)", fontsize=9)
    ax1.set_ylabel("correlación", fontsize=8.5)
    ax2.set_ylabel("correlación parcial", fontsize=8.5)
    for ax in (ax1, ax2):
        ax.set_xlabel("rezago (meses)", fontsize=8.5)
        ax.set_yticks([-1.0, -0.5, 0.0, 0.5, 1.0])
        ax.tick_params(labelsize=7.5)
    return _save(fig, f"eda_acfpacf_{country}_{category}_{table}.png")


def plot_feature_space(table: str = "FAD", block: str = "family") -> Path:
    """Espacio de características FPP3: fuerza de tendencia vs. estacionalidad por serie.

    Cada punto es una serie; la nube revela qué tan homogéneo es el panel (todas con
    tendencia fuerte y estacionalidad casi nula -> familia de modelos común).
    Los ejes se acercan a la región ocupada y un recuadro amplía el cúmulo junto a F_T = 1
    (antes 25 puntos se encimaban en una esquina de un lienzo 0-1 vacío).
    """
    ft = features.feature_table(table=table, block=block)
    fig, ax = plt.subplots(figsize=(4.92, 3.75))  # 0.82 del ancho de texto (4.92 in): texto 1:1
    colors = {
        c: col for c, col in zip(dataset.PILOT_COUNTRIES, [COUNTRY[c] for c in dataset.PILOT_COUNTRIES], strict=True)
    }
    x, y = ft["trend_strength"], ft["seasonal_strength"]
    span_y = max(float(y.max()), 0.05)
    lo_x = min(float(x.min()), 0.9)
    ax.set_xlim(lo_x - 0.02, 1.004)
    ax.set_ylim(-0.06 * span_y, span_y * 1.12)
    ins = ax.inset_axes((0.06, 0.42, 0.44, 0.52))
    cx0 = float(x[x > 0.98].min()) - 0.0008 if (x > 0.98).any() else 0.98
    ins.set_xlim(cx0, 0.9992)
    ins.set_ylim(-0.006, max(float(y[x > 0.98].max()) if (x > 0.98).any() else 0.05, 0.03) * 1.15)
    for country, grp in ft.groupby("country"):
        color = colors.get(country, UACJ_BLUE)
        # «Resto del mundo» cae casi encima de India: se dibuja como anillo hueco por ENCIMA,
        # así ambos puntos se ven sin desplazar ningún valor.
        ring = country == "all_chargeability"
        for target, size, label in ((ax, 26, COUNTRY_NAME.get(country, country)), (ins, 22, None)):
            target.scatter(
                grp["trend_strength"],
                grp["seasonal_strength"],
                label=label,
                s=size * 1.9 if ring else size,
                facecolor="none" if ring else color,
                edgecolor=color if ring else "white",
                alpha=1.0 if ring else 0.85,
                linewidths=1.1 if ring else 0.4,
                zorder=4 if ring else 3,
            )
    # rótulo de las series fuera del cúmulo (las que el recuadro no amplía)
    # rótulos a la izquierda en la mitad derecha del lienzo: a la derecha chocaban con el
    # recuadro del cúmulo y con su conector
    mid_x = (lo_x - 0.02 + 1.004) / 2
    for _, r in ft[x < cx0].iterrows():
        left = r.trend_strength > mid_x
        ax.annotate(
            f"{COUNTRY_NAME.get(r.country, r.country)} {r.category}",
            (r.trend_strength, r.seasonal_strength),
            xytext=(-4, 4) if left else (4, 4),
            ha="right" if left else "left",
            textcoords="offset points",
            fontsize=7,
            color=INK,
        )
    ins.xaxis.set_major_locator(MultipleLocator(0.005))
    ins.tick_params(labelsize=7, length=2, pad=1)
    ins.set_title("ampliación del cúmulo", fontsize=7.5, color=GRAY, pad=2)
    zoom = ax.indicate_inset_zoom(ins, edgecolor=MID, alpha=0.9)
    # solo los conectores del lado derecho del recuadro: los del izquierdo cruzaban el
    # rótulo «México F4» de camino al cúmulo
    # (orden de Matplotlib: inferior izq., superior izq., inferior der., superior der.)
    for k, conn in enumerate(zoom.connectors or ()):
        conn.set_visible(k >= 2)
    ax.set_xlabel("Fuerza de tendencia $F_T$", fontsize=8.5)
    ax.set_ylabel("Fuerza de estacionalidad $F_S$", fontsize=8.5)
    ax.tick_params(labelsize=7.5)
    bloque = _BLOQUE.get(block, block)
    ax.set_title(f"Espacio de características de las series,\ntabla {table} · bloque {bloque}", fontsize=9)
    ax.legend(
        fontsize=7.5,
        frameon=False,
        ncol=5,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.17),
        handletextpad=0.1,
        columnspacing=0.9,
    )
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
    ax.axhline(adv.mean(), color=WINE, lw=1.2, ls="--", label="media global")
    ax.set_xticks(range(1, 13))
    ax.set_xlabel("Mes del calendario (año fiscal de visas inicia en octubre)")
    ax.set_ylabel("Avance medio (días)")
    ax.set_title(f"Subseries estacional del avance mensual, {series_label(f'{country}/{category}/{table}')}")
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
    im = ax.imshow(corr.to_numpy(), cmap=DIV, vmin=-1, vmax=1)
    ax.set_xticks(range(len(corr)), [COUNTRY_NAME.get(c, c) for c in corr.columns], rotation=45, ha="right")
    ax.set_yticks(range(len(corr)), [COUNTRY_NAME.get(c, c) for c in corr.index])
    for i in range(len(corr)):
        for j in range(len(corr)):
            ax.text(j, i, f"{corr.to_numpy()[i, j]:.2f}", ha="center", va="center", fontsize=8)
    ax.set_title(f"Co-movimiento de los avances entre áreas, {category} · {table}")
    fig.colorbar(im, ax=ax, fraction=0.046, label="correlación de Pearson")
    return _save(fig, f"eda_crosscorr_{category}_{table}.png")


def plot_rolling_volatility(country: str = "mexico", category: str = "F3", table: str = "FAD") -> Path:
    """Volatilidad móvil del avance (heteroscedasticidad a lo largo del tiempo)."""
    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table))
    adv = s.diff()
    roll = adv.rolling(12, min_periods=6).std()
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    ax.plot(roll.index, roll.to_numpy(), color=UACJ_BLUE, lw=1.3)
    ax.set_title(
        f"Volatilidad en ventana móvil (12 m) del avance mensual, {series_label(f'{country}/{category}/{table}')}"
    )
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

    fig, ax = plt.subplots(figsize=(5.16, 2.9))  # 0.86 del ancho de texto (5.16 in): texto 1:1
    ax.plot(s.index, s.to_numpy() / DAYS_PER_YEAR, color=UACJ_BLUE, lw=1.0, label="serie")
    for b in bkps[:-1]:
        ax.axvline(s.index[min(b, len(s) - 1)], color=WINE, lw=1.1, ls="--")
    ax.scatter(
        anom.index,
        anom.to_numpy() / DAYS_PER_YEAR,
        color=GOLD,
        s=10,
        zorder=3,
        label=f"anomalías puntuales ({len(anom)})",
    )
    ax.plot([], [], color=WINE, lw=1.1, ls="--", label=f"cambios de régimen ({len(bkps) - 1})")
    ax.set_title(
        f"Cambios de régimen y anomalías puntuales,\n{series_label(f'{country}/{category}/{table}')}",
        fontsize=9,
    )
    ax.set_xlabel("Mes del boletín", fontsize=8.5)
    ax.set_ylabel(Y_EPOCH, fontsize=8.5)
    ax.tick_params(labelsize=7.5)
    # arriba a la izquierda: antes de 2016 la serie no pasa de ~20 años y ninguna línea de
    # cambio cruza esa esquina
    ax.legend(fontsize=7.5, frameon=False, loc="upper left")
    return _save(fig, f"eda_changepoints_{country}_{category}_{table}.png")


def plot_kalman_imputation(country: str = "mexico", category: str = "EB4_RW", table: str = "FAD") -> Path:
    """Manejo MNAR de huecos: observaciones reales vs. relleno por suavizado de Kalman.

    Default mexico/EB4_RW: la resurrección I1 dejó a china/F1 SIN huecos y la
    figura mostraba "imputado (0)" — un ejemplar stale que nadie cazó (AE4).
    """
    from vp_model import missingness as miss

    raw = miss._raw_monthly(dataset.load_series(country, category, table))
    imp = miss.kalman_impute(raw)
    gaps = raw.isna()
    fig, ax = plt.subplots(figsize=(8, 3.4))
    ax.plot(imp.index, imp.to_numpy() / DAYS_PER_YEAR, color=UACJ_GRAY, lw=1.0, ls=":", label="Kalman (relleno EDA)")
    ax.scatter(raw.index[~gaps], raw[~gaps].to_numpy() / DAYS_PER_YEAR, color=UACJ_BLUE, s=8, label="observado (F)")
    ax.scatter(
        imp.index[gaps],
        imp[gaps].to_numpy() / DAYS_PER_YEAR,
        color=WINE,
        s=26,
        marker="x",
        label=f"hueco MNAR imputado ({int(gaps.sum())})",
    )
    ax.set_title(f"Huecos MNAR e imputación por Kalman, {series_label(f'{country}/{category}/{table}')}")
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
