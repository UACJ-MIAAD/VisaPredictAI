"""Figura 'hero' de pronóstico, nivel data-viz editorial (The Economist / Financial Times):
fan-chart con bandas anidadas 50/80/95 %, anotaciones y línea de fuente.

Salida: reports/latex/Figures/results_hero_forecast.pdf
Corre en `ante`:  ante/bin/python make_hero_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from vp_model import dataset
from vp_model.config import days_to_year
from vp_model.palette import BLUE, GRID, INK, MID, style
from vp_model.palette import GRAY as TEXT_GRAY  # gris de texto legible (bajada y fuente)

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "reports" / "latex" / "Figures"
GRAY = MID  # gris medio para etiquetas de contexto
style()
plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False})
CTX_START = pd.Timestamp("2019-01-01")


def _yr(days):
    return days_to_year(days)  # AD3: epoch single-sourced (vp_model.config)


def fig_hero(country="philippines", category="F3", table="FAD", out="results_hero_forecast.pdf") -> None:
    d = pd.read_csv(ROOT / "reports" / "eval" / f"deep_pi_{table}.csv", parse_dates=["ds"])
    from vp_model import holdout_window
    from vp_model.config import HOLDOUT

    g = d[d.unique_id == f"{country}/family/{category}"].sort_values("ds")
    # G7 · puerta 2: separa historia y pronóstico donde empieza el hold-out del protocolo (antes, un literal)
    hold_start, _ = holdout_window.window_of(g)
    full = dataset.load_series(country, category, table).astype("float64")
    g = g[g.ds.isin(full.index)]
    ctx = full[(full.index >= CTX_START) & (full.index < hold_start)]

    # Ancho impreso en el `.tex` = 0.85 del ancho de texto = 5.10 in; el recorte ``tight`` quita ~0.12 in, así que
    # se dibuja a 5.20 in y el PDF natural sale a ~5.10 in: la letra se imprime a su tamaño real (antes, al 84 %).
    fig, ax = plt.subplots(figsize=(5.20, 3.0))
    # banda de pronóstico anidada (fan chart)
    for lvl, a in [(95, 0.13), (80, 0.20), (50, 0.30)]:
        ax.fill_between(
            g.ds, _yr(g[f"BiTCN-lo-{lvl}"]), _yr(g[f"BiTCN-hi-{lvl}"]), color=BLUE, alpha=a, linewidth=0, zorder=1
        )
    # historia (contexto) y real del hold-out
    ax.plot(ctx.index, _yr(ctx.to_numpy()), color=INK, lw=1.4, zorder=3)
    ax.plot(g.ds, _yr(g.y.to_numpy()), color=INK, lw=1.4, zorder=4)
    # pronóstico
    ax.plot(g.ds, _yr(g.BiTCN.to_numpy()), color=BLUE, lw=1.4, ls=(0, (4, 2)), zorder=5)
    # línea de separación entrenamiento / pronóstico
    ax.axvline(hold_start, color=GRAY, lw=1.0, ls=":", zorder=2)
    # franja superior de respiro para los rótulos sin chocar con los datos
    ymin, ymax = ax.get_ylim()
    head = ymax + 0.22 * (ymax - ymin)
    ax.set_ylim(ymin, head)
    ax.text(
        hold_start - pd.Timedelta(days=60),
        head,
        "Historia observada",
        ha="right",
        va="top",
        color=TEXT_GRAY,  # el gris medio de las líneas de contexto era casi invisible como texto
        fontsize=8,
        style="italic",
    )
    ax.text(
        hold_start + pd.Timedelta(days=60),
        head,
        # El lapso reservado son HOLDOUT meses de pronósticos A UN PASO (no un pronóstico a 24 meses).
        f"Pronósticos a un paso\nen el lapso reservado ({HOLDOUT} meses)",
        ha="left",
        va="top",
        color=BLUE,
        fontsize=8,
        linespacing=1.2,
        style="italic",
        fontweight="bold",
    )
    # etiquetas directas (sin leyenda) al borde derecho, separadas en vertical
    xend = g.ds.iloc[-1]
    yreal, yfc = _yr(g.y.iloc[-1]), _yr(g.BiTCN.iloc[-1])
    dy = 0.5 if abs(yreal - yfc) < 0.9 else 0.0  # separa si quedan encimadas
    ax.annotate(
        "real (estado F)",
        (xend, yreal),
        color=INK,
        fontsize=8,
        xytext=(10, 6 + 10 * dy),
        textcoords="offset points",
        fontweight="bold",
        va="center",
    )
    ax.annotate(
        "pronóstico BiTCN",
        (xend, yfc),
        color=BLUE,
        fontsize=8,
        xytext=(10, -6 - 10 * dy),
        textcoords="offset points",
        fontweight="bold",
        va="center",
    )
    # leyenda de bandas (proxy) en la esquina inferior derecha
    from matplotlib.patches import Patch

    bands = [Patch(facecolor=BLUE, alpha=a, label=f"{lvl} %") for lvl, a in [(50, 0.30), (80, 0.20), (95, 0.13)]]
    ax.legend(
        handles=bands,
        title="Intervalo de predicción",
        loc="lower right",
        fontsize=7,
        title_fontsize=7,
        frameon=False,
        ncol=3,
        handlelength=1.1,
        columnspacing=0.9,
        borderaxespad=0.4,
    )
    # cosmética editorial
    ax.set_ylabel("Fecha de prioridad (año)", fontsize=8)
    ax.tick_params(labelsize=8)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.margins(x=0.02)
    ax.set_xlim(CTX_START, xend + pd.Timedelta(days=470))  # espacio para las etiquetas
    area = {
        "philippines": "Filipinas",
        "mexico": "México",
        "india": "India",
        "china": "China",
        "all_chargeability": "Resto del mundo",
    }[country]
    # Titular, bajada y fuente alineados a la misma sangría izquierda que el eje.
    fig.tight_layout()
    x0 = ax.get_position().x0 - 0.075
    fig.text(
        x0,
        1.095,
        f"Pronóstico del frente de fecha de prioridad, {area} · {category} · {table}",
        ha="left",
        va="bottom",
        fontsize=11,
        fontweight="bold",
        color=INK,
    )
    fig.text(
        x0,
        1.0,
        "La red profunda global sigue el avance de la cola migratoria mes a mes y acota la\n"
        "incertidumbre con intervalos conformes",
        ha="left",
        va="bottom",
        fontsize=8,
        color=TEXT_GRAY,
        linespacing=1.2,
    )
    fig.text(
        x0,
        -0.03,
        "Fuente: U.S. Department of State, Visa Bulletin  ·  modelo BiTCN global  ·  bandas: intervalos conformes 50/80/95 %",
        fontsize=7,
        color=TEXT_GRAY,
        va="top",
    )
    FIG.mkdir(parents=True, exist_ok=True)  # R21: salida local ignorada por git, ausente en un checkout limpio
    fig.savefig(FIG / out)
    plt.close(fig)
    print(out, "OK")


if __name__ == "__main__":
    fig_hero("mexico", "F1", out="results_hero_forecast.pdf")
    fig_hero("philippines", "F3", out="results_hero_philippines.pdf")  # candidato alterno
    print("Hero en", FIG)
