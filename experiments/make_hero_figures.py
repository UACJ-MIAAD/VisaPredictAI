"""Figura 'hero' de pronóstico, nivel data-viz editorial (The Economist / Financial Times):
fan-chart con bandas anidadas 50/80/95 %, anotaciones y línea de fuente.

Salida: reports/latex/Figures/results_hero_forecast.pdf
Corre en `ante`:  ante/bin/python make_hero_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from vp_model import dataset  # noqa: E402
from vp_model.palette import BLUE, GRID, INK, MID, style  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "reports" / "latex" / "Figures"
GRAY = MID  # gris medio para etiquetas de contexto
style()
plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False})
HOLD_START = pd.Timestamp("2024-08-01")
CTX_START = pd.Timestamp("2019-01-01")


def _yr(days):
    return 1975 + days / 365.25


def fig_hero(country="philippines", category="F3", table="FAD", out="results_hero_forecast.pdf") -> None:
    d = pd.read_csv(ROOT / "reports" / "eval" / f"deep_pi_{table}.csv", parse_dates=["ds"])
    g = d[d.unique_id == f"{country}/family/{category}"].sort_values("ds")
    full = dataset.load_series(country, category, table).astype("float64")
    g = g[g.ds.isin(full.index)]
    ctx = full[(full.index >= CTX_START) & (full.index < HOLD_START)]

    fig, ax = plt.subplots(figsize=(8.0, 4.3))
    # banda de pronóstico anidada (fan chart)
    for lvl, a in [(95, 0.13), (80, 0.20), (50, 0.30)]:
        ax.fill_between(
            g.ds, _yr(g[f"BiTCN-lo-{lvl}"]), _yr(g[f"BiTCN-hi-{lvl}"]), color=BLUE, alpha=a, linewidth=0, zorder=1
        )
    # historia (contexto) y real del hold-out
    ax.plot(ctx.index, _yr(ctx.to_numpy()), color=INK, lw=1.8, zorder=3)
    ax.plot(g.ds, _yr(g.y.to_numpy()), color=INK, lw=1.8, zorder=4)
    # pronóstico
    ax.plot(g.ds, _yr(g.BiTCN.to_numpy()), color=BLUE, lw=1.8, ls=(0, (4, 2)), zorder=5)
    # línea de separación entrenamiento / pronóstico
    ax.axvline(HOLD_START, color=GRAY, lw=1.0, ls=":", zorder=2)
    # franja superior de respiro para los rótulos sin chocar con los datos
    ymin, ymax = ax.get_ylim()
    head = ymax + 0.14 * (ymax - ymin)
    ax.set_ylim(ymin, head)
    ax.text(
        HOLD_START - pd.Timedelta(days=60),
        head,
        "Historia observada",
        ha="right",
        va="top",
        color=GRAY,
        fontsize=9,
        style="italic",
    )
    ax.text(
        HOLD_START + pd.Timedelta(days=60),
        head,
        "Pronóstico a 24 meses",
        ha="left",
        va="top",
        color=BLUE,
        fontsize=9,
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
        fontsize=8.5,
        xytext=(10, 6 + 10 * dy),
        textcoords="offset points",
        fontweight="bold",
        va="center",
    )
    ax.annotate(
        "pronóstico BiTCN",
        (xend, yfc),
        color=BLUE,
        fontsize=8.5,
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
        fontsize=7.5,
        title_fontsize=7.5,
        frameon=False,
        ncol=3,
        handlelength=1.1,
        columnspacing=0.9,
        borderaxespad=0.4,
    )
    # cosmética editorial
    ax.set_ylabel("Fecha de prioridad (año)")
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
    fig.suptitle(
        f"Pronóstico del frente de fecha de prioridad — {area} · {category} · {table}",
        x=0.04,
        ha="left",
        fontsize=13,
        fontweight="bold",
        color=INK,
        y=1.02,
    )
    ax.set_title(
        "La red profunda global sigue el avance de la cola migratoria y acota la incertidumbre con intervalos conformes",
        loc="left",
        fontsize=9.5,
        color=GRAY,
        pad=10,
    )
    fig.text(
        0.04,
        -0.02,
        "Fuente: U.S. Department of State, Visa Bulletin  ·  modelo BiTCN global  ·  bandas: intervalos conformes 50/80/95 %",
        fontsize=7.5,
        color=GRAY,
    )
    fig.savefig(FIG / out)
    plt.close(fig)
    print(out, "OK")


if __name__ == "__main__":
    fig_hero("mexico", "F1", out="results_hero_forecast.pdf")
    fig_hero("philippines", "F3", out="results_hero_philippines.pdf")  # candidato alterno
    print("Hero en", FIG)
