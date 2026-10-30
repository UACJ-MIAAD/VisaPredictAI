"""Figura del Apéndice A.10: MASE rolling vs horizonte — deriva (drift) contra naïve-1 (RW).

Derivada del anexo multi-horizonte reproducible (reports/eval/horizon_facts.json, generado por
experiments/build_horizon_facts.py). Backtest ROLLING sobre todo el histórico, F-only, escala
MASE canónica; NO son las cifras del hold-out fijo canónico (MCS={naive1}). La figura muestra
dos hechos: el error crece con el horizonte, y la deriva domina al paseo aleatorio con un margen
que se ensancha (significativo salvo los anillos huecos).

    ante/bin/python experiments/make_horizon_figure.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.ticker import FormatStrFormatter, MultipleLocator

from vp_model.palette import BLUE, FONT_RC, GRAY, GRID, MID, WINE, register_fonts

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "reports" / "latex" / "Figures"
FACTS = ROOT / "reports" / "eval" / "horizon_facts.json"

register_fonts()
plt.rcParams.update(
    {
        **FONT_RC,
        "font.size": 9,
        "axes.titlesize": 10,
        "axes.labelsize": 9,
        "axes.edgecolor": MID,
        "axes.linewidth": 0.8,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "savefig.bbox": "tight",
        "savefig.dpi": 300,
    }
)


def _panel(ax, rows: list[dict], title: str) -> None:
    h = [r["h"] for r in rows]
    x = np.arange(len(h))
    drift = [r["drift"] for r in rows]
    rw = [r["naive1"] for r in rows]
    # Región de ventaja: el naïve-1 queda por encima de la deriva (más error).
    ax.fill_between(x, drift, rw, color=BLUE, alpha=0.10, zorder=1, label="Ventaja de la deriva")
    ax.plot(x, rw, color=GRAY, lw=1.1, ls="--", marker="s", ms=3, zorder=3, label="Naïve-1 (RW)")
    ax.plot(x, drift, color=BLUE, lw=1.4, marker="o", ms=3.5, zorder=4, label="Deriva (drift)")
    # Anillo hueco donde la ventaja NO es significativa (Holm).
    for xi, di, r in zip(x, drift, rows, strict=True):
        if not r["sig"]:
            ax.plot(xi, di, marker="o", ms=7, mfc="none", mec=WINE, mew=1.1, zorder=5)
    ax.set_xticks(x)
    ax.set_xticklabels([str(v) for v in h])
    # un solo formato en el eje: medios puntos de MASE con un decimal (0.0, 0.5, ..., 4.0)
    ax.yaxis.set_major_locator(MultipleLocator(0.5))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.1f"))
    ax.tick_params(labelsize=7.5)
    ax.set_title(title, color=BLUE, fontsize=9.5)
    ax.set_xlabel("Horizonte (meses)", fontsize=8.5)
    ax.margins(x=0.04)


def build() -> Path:
    d = json.loads(FACTS.read_text())
    # Impresa a 0.96 del ancho de texto (5.76 in): se dibuja a ese ancho para que la letra salga 1:1
    # (antes 9.1 in reducidos al 63 %: la leyenda se imprimía a 4.7 pt).
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(5.83, 2.9), sharey=True)
    _panel(a1, d["FAD"]["significance"], "FAD")
    _panel(a2, d["DFF"]["significance"], "DFF")
    a1.set_ylabel("MASE (ventana móvil · solo estado F)", fontsize=8.5)
    # el anillo hueco sólo aparece en DFF: sin esta entrada la leyenda (en FAD) no lo explicaba
    anillo = Line2D(
        [], [], ls="", marker="o", ms=7, mfc="none", mec=WINE, mew=1.1, label="Ventaja no significativa (Holm)"
    )
    a1.legend(handles=[*a1.get_legend_handles_labels()[0], anillo], loc="upper left", fontsize=7.5, frameon=False)
    fig.tight_layout()
    out = FIG / "horizon_mase_curves.pdf"
    fig.savefig(out)
    plt.close(fig)
    print(f"✓ {out}")
    return out


if __name__ == "__main__":
    build()
