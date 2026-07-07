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
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from vp_model.palette import BLUE, GRAY, GRID, MID, WINE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "reports" / "latex" / "Figures"
FACTS = ROOT / "reports" / "eval" / "horizon_facts.json"

plt.rcParams.update(
    {
        "font.family": "serif",
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
    ax.plot(x, rw, color=GRAY, lw=1.4, ls="--", marker="s", ms=4, zorder=3, label="Naïve-1 (RW)")
    ax.plot(x, drift, color=BLUE, lw=1.8, marker="o", ms=4.5, zorder=4, label="Deriva (drift)")
    # Anillo hueco donde la ventaja NO es significativa (Holm).
    for xi, di, r in zip(x, drift, rows, strict=True):
        if not r["sig"]:
            ax.plot(xi, di, marker="o", ms=9, mfc="none", mec=WINE, mew=1.3, zorder=5)
    ax.set_xticks(x)
    ax.set_xticklabels([str(v) for v in h])
    ax.set_title(title, color=BLUE)
    ax.set_xlabel("Horizonte (meses)")
    ax.margins(x=0.04)


def build() -> Path:
    d = json.loads(FACTS.read_text())
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.2, 3.5), sharey=True)
    _panel(a1, d["FAD"]["significance"], "FAD")
    _panel(a2, d["DFF"]["significance"], "DFF")
    a1.set_ylabel("MASE (rolling · solo estado F)")
    a1.legend(loc="upper left", fontsize=7.5, frameon=False)
    fig.tight_layout()
    out = FIG / "horizon_mase_curves.pdf"
    fig.savefig(out)
    plt.close(fig)
    print(f"✓ {out}")
    return out


if __name__ == "__main__":
    build()
