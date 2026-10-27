"""Suite EDA de calidad publicación para el panel multiserie (PI-I §1.2).

Genera figuras distribucionales que un EDA serio exige (violines, ridgeline, estacionalidad,
composición de régimen, spaghetti, distribuciones, correlación, longitud, retrogresiones).
Salida en reports/latex/Figures/ como eda2_*.pdf (paleta UACJ + serif).
Corre en `ante`:  ante/bin/python make_eda_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from vp_model.config import DAYS_PER_YEAR, days_to_year
from vp_model.palette import BLUE, COUNTRY, DIV, FONT_RC, GOLD, GRID, INK, MID, REGIME, SEQ, WINE, register_fonts

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "reports" / "latex" / "Figures"
CCOL = COUNTRY
CNAME = {
    "mexico": "México",
    "india": "India",
    "china": "China",
    "philippines": "Filipinas",
    "all_chargeability": "Resto",
}
CATS = ["F1", "F2A", "F2B", "F3", "F4"]
AREAS = ["mexico", "india", "china", "philippines", "all_chargeability"]
sns.set_theme(style="whitegrid", font="serif", rc={"axes.edgecolor": MID, "grid.color": GRID})
register_fonts()
plt.rcParams.update({**FONT_RC, "savefig.bbox": "tight", "savefig.dpi": 300, "font.size": 9})


def _save(fig, name: str) -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG / name)


def _panel(table="FAD"):
    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv", parse_dates=["bulletin_date"])
    f = d[(d.status == "F") & (d.block == "family") & (d.table == table)].copy().sort_values("bulletin_date")
    f["delta"] = f.groupby(["country", "category"])["days_since_base"].diff()
    f["año"] = f.bulletin_date.dt.year
    f["mes"] = f.bulletin_date.dt.month
    f["pais"] = f.country.map(CNAME)
    f["years"] = days_to_year(f.days_since_base)  # AD3
    return f


def fig_violin_country() -> None:
    f = _panel()
    g = f[(f.delta.between(-120, 280))]  # recorte para ver la forma; retrogresiones extremas aparte
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    order = AREAS
    sns.violinplot(
        data=g,
        x="country",
        y="delta",
        order=order,
        hue="country",
        hue_order=order,
        palette=CCOL,
        legend=False,
        cut=0,
        inner="quartile",
        linewidth=0.9,
        ax=ax,
    )
    ax.set_xticks(range(len(order)), [CNAME[a] for a in order])
    ax.axhline(0, color=MID, lw=0.8, ls="--")
    ax.set_xlabel("")
    ax.set_ylabel("Avance mensual de la fecha (días)")
    ax.set_title("Distribución del avance mensual por área de cargabilidad (FAD)", fontsize=10, color=BLUE)
    _save(fig, "eda2_violin_country.pdf")
    plt.close(fig)
    print("eda2_violin_country OK")


def fig_violin_category() -> None:
    f = _panel()
    g = f[f.delta.between(-120, 280)]
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    sns.violinplot(data=g, x="category", y="delta", order=CATS, color=BLUE, cut=0, inner="box", linewidth=0.9, ax=ax)
    ax.axhline(0, color=MID, lw=0.8, ls="--")
    ax.set_xlabel("Categoría de preferencia familiar")
    ax.set_ylabel("Avance mensual (días)")
    ax.set_title("Distribución del avance mensual por categoría (FAD)", fontsize=10, color=BLUE)
    _save(fig, "eda2_violin_category.pdf")
    plt.close(fig)
    print("eda2_violin_category OK")


def fig_ridgeline() -> None:
    """Ridgeline (joyplot) de la distribución del avance por categoría, normalizado por pico."""
    from scipy.stats import gaussian_kde

    f = _panel()
    g = f[f.delta.between(-90, 200)]
    pal = [SEQ(v) for v in np.linspace(0.40, 0.92, len(CATS))]
    xs = np.linspace(-90, 200, 400)
    fig, ax = plt.subplots(figsize=(5.4, 3.2))  # 0.86 del ancho de texto
    for i, cat in enumerate(reversed(CATS)):  # F4 abajo, F1 arriba
        vals = g[g.category == cat].delta.dropna().to_numpy()
        if len(vals) < 5:
            continue
        dens = gaussian_kde(vals, bw_method=0.35)(xs)
        dens = dens / dens.max() * 0.92
        col = pal[CATS.index(cat)]
        y0 = i * 0.7
        ax.fill_between(xs, y0, y0 + dens, color=col, alpha=0.85, zorder=10 - i, lw=0)
        ax.plot(xs, y0 + dens, color="white", lw=1.1, zorder=10 - i)
        # rótulo en tinta (el tono de la propia cresta era demasiado claro para leerse)
        ax.text(-95, y0 + 0.04, cat, ha="right", va="bottom", fontweight="bold", color=INK, fontsize=9)
    ax.axvline(0, color=MID, ls="--", lw=0.8, zorder=0)
    ax.set_yticks([])
    ax.set_xlim(-105, 200)
    ax.set_xlabel("Avance mensual de la fecha (días)", fontsize=8.5)
    ax.tick_params(labelsize=8)
    ax.set_title("Distribución del avance mensual por categoría (FAD)", fontsize=9.5, color=BLUE)
    for sp in ("left", "right", "top"):
        ax.spines[sp].set_visible(False)
    _save(fig, "eda2_ridgeline.pdf")
    plt.close(fig)
    print("eda2_ridgeline OK")


def fig_regime() -> None:
    """Composición de régimen C/F/U/UNK por área x categoría (familiar, ambas tablas)."""
    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv")
    fam = d[d.block == "family"].copy()
    fam["serie"] = fam.country.map(CNAME) + "/" + fam.category
    order_status = ["F", "C", "U", "UNK"]
    cmap = {s: REGIME[s]["line"] for s in order_status}
    comp = fam.groupby(["serie", "status"]).size().unstack(fill_value=0)
    comp = comp.reindex(columns=order_status, fill_value=0)
    comp = comp.div(comp.sum(axis=1), axis=0).sort_values("F")
    fig, ax = plt.subplots(figsize=(7.0, 6.6))
    left = np.zeros(len(comp))
    for s in order_status:
        ax.barh(comp.index, comp[s], left=left, color=cmap[s], label=s, height=0.8, edgecolor="white", linewidth=0.3)
        left += comp[s].to_numpy()
    ax.set_xlim(0, 1)
    ax.set_xlabel("Fracción de meses en cada régimen")
    ax.set_title("Composición de régimen por serie (preferencia familiar)", fontsize=10, color=BLUE, pad=10)
    # leyenda al pie (evita encimarse con el título arriba)
    fig.legend(ncol=4, loc="lower center", bbox_to_anchor=(0.5, -0.015), fontsize=8, frameon=False)
    ax.tick_params(axis="y", labelsize=6.5)
    _save(fig, "eda2_regime.pdf")
    plt.close(fig)
    print("eda2_regime OK")


def fig_seasonality() -> None:
    f = _panel()
    g = f[f.delta.between(-120, 280)]
    fig, ax = plt.subplots(figsize=(5.4, 2.9))  # 0.86 del ancho de texto: letra a tamaño real
    sns.boxplot(data=g, x="mes", y="delta", color=BLUE, fliersize=1.0, linewidth=0.7, boxprops={"alpha": 0.55}, ax=ax)
    ax.axvline(8.5, color=GOLD, ls="--", lw=1.2)
    # rótulo en una franja libre sobre los datos (antes cruzaba la línea y los atípicos)
    y_top = float(g.delta.max())
    ax.set_ylim(top=y_top + 70)
    ax.text(8.38, y_top + 58, "inicio del año fiscal (oct)", color=GOLD, fontsize=7.5, va="top", ha="right")
    ax.set_xticks(range(12), ["E", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"])
    ax.axhline(0, color=MID, lw=0.8, ls="--")
    ax.set_xlabel("Mes del boletín", fontsize=8.5)
    ax.set_ylabel("Avance mensual (días)", fontsize=8.5)
    ax.tick_params(labelsize=8)
    ax.set_title("Estacionalidad del avance por mes calendario (FAD)", fontsize=9.5, color=BLUE)
    _save(fig, "eda2_seasonality.pdf")
    plt.close(fig)
    print("eda2_seasonality OK")


def fig_spaghetti() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.6, 3.4), sharex=True)
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        ff = _panel(table)
        for (country, _cat), s in ff.groupby(["country", "category"]):
            ax.plot(s.bulletin_date, s.years, color=CCOL[country], lw=0.8, alpha=0.75)
        ax.set_title(f"{table}", fontsize=10, color=BLUE)
        ax.set_ylabel("Fecha de prioridad (año)") if table == "FAD" else None
        ax.tick_params(axis="x", labelrotation=30, labelsize=7)
    from matplotlib.lines import Line2D

    handles = [Line2D([0], [0], color=CCOL[a], lw=2, label=CNAME[a]) for a in AREAS]
    fig.legend(handles=handles, loc="lower center", ncol=5, fontsize=8, frameon=False, bbox_to_anchor=(0.5, -0.06))
    fig.suptitle("Trayectoria de las 25 series familiares por área", y=1.0, fontsize=10, color=BLUE)
    fig.tight_layout()
    _save(fig, "eda2_spaghetti.pdf")
    plt.close(fig)
    print("eda2_spaghetti OK")


def fig_distributions() -> None:
    f = _panel()
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.6, 3.2))
    sns.histplot(f.years, bins=40, color=BLUE, ax=a1, edgecolor="white")
    a1.set_xlabel("Fecha de prioridad (año)")
    a1.set_ylabel("Observaciones (F)")
    a1.set_title("(a) Variable objetivo", fontsize=9.5, color=BLUE)
    d = f.delta.dropna()
    a2.hist(np.sign(d) * np.log1p(np.abs(d)), bins=60, color=GOLD, edgecolor="white")
    a2.axvline(0, color=MID, lw=0.8, ls="--")
    a2.set_xlabel("Avance mensual  (escala signo·log)")
    a2.set_title("(b) Incrementos: cola de retrogresión", fontsize=9.5, color=BLUE)
    a2.text(
        0.03,
        0.95,
        f"retrogresiones: {(d < 0).mean() * 100:.1f}%",
        transform=a2.transAxes,
        fontsize=7.5,
        va="top",
        color=WINE,
    )
    fig.suptitle("Distribución del objetivo y de sus incrementos (FAD)", fontsize=10, color=BLUE, y=1.02)
    fig.tight_layout()
    _save(fig, "eda2_distributions.pdf")
    plt.close(fig)
    print("eda2_distributions OK")


def fig_corr() -> None:
    f = _panel()
    f["serie"] = f.pais + "/" + f.category
    wide = f.pivot_table(index="bulletin_date", columns="serie", values="delta")
    corr = wide.corr().fillna(0)
    # Tamaño real de impresión (0.82 del ancho de texto): rótulos de 7 pt sin reducción.
    # La barra de color va horizontal en la esquina libre entre dendrogramas (antes pisaba
    # el dendrograma de filas) y se retira el rótulo «serie» duplicado de ambos ejes.
    g = sns.clustermap(
        corr,
        cmap=DIV,
        center=0,
        figsize=(5.2, 5.4),
        cbar_pos=(0.015, 0.935, 0.10, 0.018),
        cbar_kws={"orientation": "horizontal", "ticks": [0, 0.5, 1]},
        dendrogram_ratio=0.12,
        xticklabels=True,
        yticklabels=True,
    )
    g.ax_heatmap.tick_params(labelsize=6.5, length=2, pad=1.5)
    g.ax_heatmap.set_xlabel("")
    g.ax_heatmap.set_ylabel("")
    g.ax_cbar.tick_params(labelsize=6.5, length=2, pad=1)
    g.ax_cbar.set_title("r de Pearson", fontsize=6.5, pad=2, loc="left")
    g.figure.suptitle("Correlación entre incrementos de las 25 series (FAD)", y=1.0, fontsize=9.5, color=BLUE)
    _save(g.figure, "eda2_corr.pdf")
    plt.close(g.figure)
    print("eda2_corr OK")


def fig_length_retro() -> None:
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(6.25, 2.9), gridspec_kw={"wspace": 0.3})  # ancho de texto
    # longitud por serie (FAD+DFF familiar)
    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv")
    fam = d[(d.block == "family") & (d.status == "F")]
    lens = fam.groupby(["country", "category", "table"]).size()
    sns.histplot(lens.values, bins=18, color=BLUE, ax=a1, edgecolor="white")
    a1.set_xlabel("Observaciones con fecha (F) por serie", fontsize=8.5)
    a1.set_ylabel("Número de series", fontsize=8.5)
    a1.set_title("(a) Longitud de las series", fontsize=9, color=BLUE)
    from matplotlib.ticker import MaxNLocator

    a1.yaxis.set_major_locator(MaxNLocator(integer=True))
    # retrogresiones por serie (FAD)
    f = _panel()
    retro = (
        f[f.delta < 0].groupby(["country", "category"]).agg(n=("delta", "size"), peor=("delta", "min")).reset_index()
    )
    retro["serie"] = retro.country.map(CNAME) + " " + retro.category
    retro["yr"] = -retro.peor / DAYS_PER_YEAR
    retro = retro.sort_values("peor")
    a2.scatter(retro.n, retro.yr, c=[CCOL[c] for c in retro.country], s=26, edgecolor="white", zorder=3)
    # etiquetar solo las series más extremas: el cúmulo se distingue por color (evita encimamiento);
    # los rótulos van hacia DENTRO del eje (antes el de la derecha se salía del marco)
    x_mid = (retro.n.min() + retro.n.max()) / 2
    for _, r in retro[retro.yr > 5.2].iterrows():
        right = r.n > x_mid
        a2.annotate(
            r.serie,
            (r.n, r.yr),
            fontsize=7,
            xytext=(-5, 0) if right else (5, 0),
            ha="right" if right else "left",
            va="center",
            textcoords="offset points",
            color=INK,
        )
    a2.set_xlim(retro.n.min() - 0.8, retro.n.max() + 0.8)
    a2.set_ylim(0, retro.yr.max() * 1.08)
    from matplotlib.lines import Line2D

    handles = [Line2D([0], [0], marker="o", ls="", mfc=CCOL[a], mec="white", ms=6, label=CNAME[a]) for a in AREAS]
    # leyenda en la esquina superior izquierda, libre de puntos (antes tapaba el cúmulo inferior)
    a2.legend(handles=handles, fontsize=7, loc="upper left", frameon=False, handletextpad=0.2, borderaxespad=0.3)
    a2.set_xlabel("Número de retrogresiones", fontsize=8.5)
    a2.set_ylabel("Mayor retroceso (años)", fontsize=8.5)
    a2.set_title("(b) Retrogresiones por serie\n(solo se rotulan las extremas)", fontsize=9, color=BLUE)
    for a in (a1, a2):
        a.tick_params(labelsize=8)
    fig.tight_layout()
    _save(fig, "eda2_length_retro.pdf")
    plt.close(fig)
    print("eda2_length_retro OK")


if __name__ == "__main__":
    fig_violin_country()
    fig_violin_category()
    fig_ridgeline()
    fig_regime()
    fig_seasonality()
    fig_spaghetti()
    fig_distributions()
    fig_corr()
    fig_length_retro()
    print("Suite EDA en", FIG)
