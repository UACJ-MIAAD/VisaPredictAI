"""Gráfico comparativo por país en gramática 'Latinometrics' adaptada a la identidad UACJ:
barras horizontales ordenadas, mini-bandera + nombre a la izquierda, valor al final de la barra,
una barra resaltada, fila 'globo' para la agrupación residual, titular-frase + remate + fuente.
Paleta UACJ + tipografía serif (no editorial sans), para encajar en el .tex académico.

Salida: reports/latex/Figures/latam_backlog_f4.pdf
Corre en `ante`:  ante/bin/python make_latinometrics_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.offsetbox import AnnotationBbox, OffsetImage  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from vp_model.palette import BLUE, GRAY, INK, MID, MUTE, style  # noqa: E402

ROOT = Path(__file__).resolve().parent
FIG = ROOT / "reports" / "latex" / "Figures"
style()

W, H = 150, 100  # lienzo de cada mini-bandera


def _flag(country: str) -> np.ndarray:
    """Mini-bandera simplificada (PIL) -> arreglo RGBA. Reconocible, no oficial."""
    im = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    if country == "mexico":
        d.rectangle([0, 0, W // 3, H], fill=(0, 104, 71))
        d.rectangle([W // 3, 0, 2 * W // 3, H], fill=(255, 255, 255))
        d.rectangle([2 * W // 3, 0, W, H], fill=(206, 17, 53))
        d.ellipse([W // 2 - 8, H // 2 - 8, W // 2 + 8, H // 2 + 8], outline=(108, 70, 40), width=2)
    elif country == "india":
        d.rectangle([0, 0, W, H // 3], fill=(255, 153, 51))
        d.rectangle([0, H // 3, W, 2 * H // 3], fill=(255, 255, 255))
        d.rectangle([0, 2 * H // 3, W, H], fill=(19, 136, 8))
        d.ellipse([W // 2 - 11, H // 2 - 11, W // 2 + 11, H // 2 + 11], outline=(0, 0, 128), width=2)
    elif country == "china":
        d.rectangle([0, 0, W, H], fill=(222, 42, 16))
        _star(d, 38, 42, 20, (255, 222, 0))
        for sx, sy in [(72, 20), (88, 36), (88, 58), (72, 74)]:
            _star(d, sx, sy, 7, (255, 222, 0))
    elif country == "philippines":
        d.rectangle([0, 0, W, H // 2], fill=(0, 56, 168))
        d.rectangle([0, H // 2, W, H], fill=(206, 17, 38))
        d.polygon([(0, 0), (0, H), (78, H // 2)], fill=(255, 255, 255))
        _star(d, 26, H // 2, 9, (252, 209, 22))
    else:  # globo: agrupación residual "All Chargeability"
        cx, cy, r = W // 2, H // 2, 44
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(220, 230, 245), outline=(0, 60, 166), width=3)
        d.line([cx, cy - r, cx, cy + r], fill=(0, 60, 166), width=2)
        d.arc([cx - 18, cy - r, cx + 18, cy + r], 0, 360, fill=(0, 60, 166), width=2)
        d.line([cx - r, cy, cx + r, cy], fill=(0, 60, 166), width=2)
    return np.asarray(im)


def _star(d: ImageDraw.ImageDraw, cx: int, cy: int, r: float, fill) -> None:
    pts = []
    for k in range(10):
        ang = np.pi / 2 + k * np.pi / 5
        rad = r if k % 2 == 0 else r * 0.4
        pts.append((cx + rad * np.cos(ang), cy - rad * np.sin(ang)))
    d.polygon(pts, fill=fill)


NAME = {
    "mexico": "México",
    "philippines": "Filipinas",
    "india": "India",
    "china": "China",
    "all_chargeability": "Resto del mundo*",
}


def fig_backlog(category="F4", table="FAD", highlight="mexico", out="latam_backlog_f4.pdf") -> None:
    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv", parse_dates=["bulletin_date", "priority_date"])
    g = d[(d.status == "F") & (d.block == "family") & (d.table == table) & (d.category == category)]
    last = g.bulletin_date.max()
    row = g[g.bulletin_date == last]
    bl = {
        c: (row[row.country == c].bulletin_date.iloc[0] - row[row.country == c].priority_date.iloc[0]).days / 365.25
        for c in NAME
        if len(row[row.country == c])
    }
    order = sorted(bl, key=lambda c: bl[c])  # menor abajo, mayor arriba (barh)
    vmax = max(bl.values())

    fig, ax = plt.subplots(figsize=(8.4, 4.7))
    for i, c in enumerate(order):
        col = BLUE if c == highlight else MUTE
        ax.barh(i, bl[c], height=0.62, color=col, zorder=2)
        ab = AnnotationBbox(OffsetImage(_flag(c), zoom=0.26), (-9.4, i), frameon=False, box_alignment=(0.5, 0.5), pad=0)
        ax.add_artist(ab)
        ax.text(
            -8.2,
            i,
            NAME[c],
            va="center",
            ha="left",
            fontsize=11,
            color=INK,
            fontweight="bold" if c == highlight else "normal",
        )
        lab = f"{bl[c]:.1f} años"
        ax.text(
            bl[c] + 0.35,
            i,
            lab,
            va="center",
            ha="left",
            fontsize=10.5,
            color=BLUE if c == highlight else MID,
            fontweight="bold" if c == highlight else "normal",
        )

    ax.set_xlim(-11, vmax * 1.16)
    ax.set_ylim(-0.7, len(order) - 0.3)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])

    fig.text(
        0.012,
        1.05,
        "Una petición de hermano en México espera 25 años para una visa",
        fontsize=15.5,
        fontweight="bold",
        color=INK,
        ha="left",
    )
    fig.text(
        0.012,
        0.99,
        "Atraso vigente de la cola familiar F4 (hermanos de ciudadanos) por país o área de "
        "cargabilidad — ninguna de las cinco baja de 17 años",
        fontsize=10.5,
        color=GRAY,
        ha="left",
    )
    fig.text(
        0.012,
        -0.04,
        "Fuente: U.S. Department of State, Visa Bulletin (Final Action Dates, julio 2026).  "
        "Atraso = mes del boletín − fecha de prioridad vigente.  *Agrupación residual, no un país.",
        fontsize=7.6,
        color=GRAY,
        ha="left",
    )
    fig.text(0.988, -0.105, "VisaPredict AI", fontsize=9, color=BLUE, ha="right", va="top", fontweight="bold")
    fig.savefig(FIG / out)
    plt.close(fig)
    print(out, "OK")


if __name__ == "__main__":
    fig_backlog()
    print("Latinometrics-UACJ en", FIG)
