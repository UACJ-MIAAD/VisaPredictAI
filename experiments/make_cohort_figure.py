"""E5 · Figura del router por cohorte: efecto contra el naïve-1 y el margen material.

Se dibuja desde ``reports/governance/e5_facts.json`` y de ningún otro sitio: si una barra
contradice la tabla del `.tex`, es que alguien tecleó una cifra. La línea del margen material
está para que se vea de un golpe por qué una mejora grande a doce meses no basta cuando el corto
plazo no llega: la regla exige los tres horizontes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _figkit import FigureContext, LangCtx, Theme, footer, header, run_variants, save_dual
from make_latinometrics_figures import MES  # sys.path[0] = experiments/

from vp_model import palette as _palette

# El nombre visible de la cohorte sale de la MISMA autoridad que usa la tabla del `.tex`.
# Construirlo aquí por separado es lo que hizo que la tabla dijera «inestable» y la figura
# «no_estable» para la misma celda, dos páginas seguidas (M65).
from vp_model.stability import etiqueta_celda

ROOT = Path(__file__).resolve().parent.parent
FACTS = ROOT / "reports" / "governance" / "e5_facts.json"
PNG_ROOT = ROOT / "reports" / "figures" / "cohorts"
PDF_ROOT = ROOT / "reports" / "latex" / "Figures"
#: Censo de cohortes (E1) que alimenta a `e5_facts.json`: es el que registra sobre qué panel se calculó el análisis.
COHORTES = ROOT / "reports" / "eval" / "series_cohorts.json"


def anada_del_analisis(facts: dict) -> str:
    """Mes del panel sobre el que se calcularon las cifras que dibuja la figura (``AAAA-MM``).

    Antes el pie tomaba `panel_vintage` de `key_facts.json`, la añada VIVA del sitio: al publicarse un boletín nuevo
    el pie cambiaba de mes aunque las barras siguieran siendo las del corte anterior. La añada correcta es la del
    censo de cohortes que `e5_facts.json` sella en su procedencia; se acredita por hash con los MISMOS bytes que se
    leen, y si el censo en disco no es el sellado la figura no se dibuja.
    """
    crudo = COHORTES.read_bytes()
    sellado = facts["provenance"]["cohorts"]
    if hashlib.sha256(crudo).hexdigest() != sellado:
        raise SystemExit(
            f"{COHORTES.name} no es el censo que selló e5_facts.json ({sellado[:12]}…): no se fecha el pie"
        )
    return str(json.loads(crudo)["provenance"]["panel_last_month"])


TXT = {
    "es": {
        "headline": "Ningún enrutador bate al naïve-1 de su cohorte en los tres horizontes",
        "sub": "efecto medio en MASE (positivo = el enrutador mejora); la regla exige los tres",
        "x": "efecto en MASE frente al naïve-1 de la propia cohorte",
        "margin": "margen material (0.005)",
        "sig": "significativo tras Holm",
        "nosig": "no significativo",
        # renglón propio: al ancho impreso (4.68 in) el pie en una sola línea se salía de la figura
        "foot": "\nexploratorio · regla fijada antes de calcular · el enrutador no se despliega",
        "null": "efecto nulo: el enrutador elige el {modelo}",
        "naive": {"naive1": "naïve-1", "naive": "naïve estacional"},
        "footer": "Corte de {mes} de {anio} · panel gobernado",
    },
    "en": {
        "headline": "No router beats its cohort's naive-1 across all three horizons",
        "sub": "mean MASE effect (positive = router improves); the rule demands all three",
        "x": "MASE effect against the cohort's own naive-1",
        "margin": "material margin (0.005)",
        "sig": "significant after Holm",
        "nosig": "not significant",
        "foot": "exploratory · gate frozen before computing · the router is not deployed",
        "null": "null effect: the router picks the {modelo}",
        "naive": {"naive1": "naive-1", "naive": "seasonal naive"},
        "footer": "{mes} {anio} cut · governed panel",
    },
}


MES_EN = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


def context_for(lang: str, theme: str) -> FigureContext:
    pal = _palette.DARK if theme == "dark" else _palette.LIGHT
    return FigureContext(
        theme=Theme(theme, pal, {}),
        lang=LangCtx(code=lang, txt=TXT[lang], months=MES if lang == "es" else MES_EN, countries={}),
    )


def fig_router_effect(facts: dict, ctx: FigureContext) -> plt.Figure:
    """Barras horizontales del efecto por celda × horizonte, con el margen material."""
    filas = facts["router_rows"]
    etiquetas = [f"{etiqueta_celda(f['table'], f['cohort'])}  h={f['h']}" for f in filas]
    efectos = np.array([f["effect"] for f in filas], dtype="float64")
    signif = [f["significant"] for f in filas]
    pal = ctx.theme.colors
    color_ok = pal["BLUE"]
    color_no = pal["GRAY"]

    # Ancho impreso en el `.tex` = 0.78 del ancho de texto = 4.68 in: se dibuja a 4.57 in porque el recorte ``tight``
    # añade 0.1 in de margen por lado y el PDF natural sale a ~4.68 in: letra a tamaño real (antes, 6.25 in al 74 %).
    fig, ax = plt.subplots(figsize=(4.57, 0.21 * len(filas) + 1.2))
    y = np.arange(len(filas))
    # Codificación: barra LLENA azul = significativa tras Holm; barra HUECA (sólo contorno gris) = no significativa.
    # Así lo describe el pie del `.tex`; antes todas eran llenas y sólo el color distinguía.
    ax.barh(
        y,
        efectos,
        color=[color_ok if s else pal["PAPER"] for s in signif],
        edgecolor=[color_ok if s else color_no for s in signif],
        linewidth=0.9,
        height=0.66,
        zorder=3,
    )
    # referencias DETRÁS de las barras (antes las líneas discontinuas se pintaban encima)
    ax.axvline(0.0, lw=0.9, color=pal["INK"], alpha=0.55, zorder=1)
    for signo in (1, -1):
        ax.axvline(signo * facts["RouterMargen"], lw=0.9, ls="--", color=pal["GOLD"], zorder=1)
    ax.set_yticks(y, etiquetas, fontsize=8)
    # filas con efecto exactamente nulo porque el router eligió un naïve: sin barra visible,
    # se rotulan (con el naïve que de verdad eligió) para que no parezcan filas vacías
    for yi, f in zip(y, filas, strict=True):
        modelo = str(f.get("model", ""))
        if f["effect"] == 0 and modelo.startswith("naive"):
            ax.text(
                facts["RouterMargen"] * 2.4,
                float(yi),
                ctx.lang.txt["null"].format(modelo=ctx.lang.txt["naive"].get(modelo, modelo)),
                fontsize=7,
                color=pal["GRAY"],
                va="center",
            )
    ax.invert_yaxis()
    # Banda reservada ARRIBA, derivada del número de filas, para la leyenda del margen. Antes
    # el texto se anclaba en `len(filas) - 0.2`, es decir FUERA del área de trazado, y la línea
    # del eje lo atravesaba (M65: sólo se ve abriendo el PDF).
    ax.set_ylim(len(filas) - 0.5, -1.25)
    ax.set_xlabel(ctx.lang.txt["x"], fontsize=8.5)
    ax.tick_params(axis="x", labelsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    from matplotlib.patches import Patch

    ax.legend(
        handles=[
            Patch(facecolor=color_ok, edgecolor=color_ok, label=ctx.lang.txt["sig"]),
            Patch(facecolor=pal["PAPER"], edgecolor=color_no, linewidth=0.9, label=ctx.lang.txt["nosig"]),
        ],
        loc="upper right",
        fontsize=7,
        frameon=False,
        handlelength=1.4,
        borderaxespad=0.2,
    )
    ax.text(
        facts["RouterMargen"],
        -0.85,
        f"  {ctx.lang.txt['margin']}",
        fontsize=7,
        color=pal["GOLD"],
        va="center",
        ha="left",
    )
    fig.tight_layout()
    header(fig, ctx.lang.txt["headline"], ctx.lang.txt["sub"], ctx, y=None, dy=None)
    footer(fig, anada_del_analisis(facts), ctx, extra=ctx.lang.txt["foot"], y=None, fs=7)
    return save_dual(
        fig,
        "e5_router_effect",
        ctx,
        png_root=PNG_ROOT,
        pdf_root=PDF_ROOT,
        pdf_name=lambda n: n,  # save_dual añade la extensión: pasar "x.pdf" daba x.pdf.pdf
    )


def main() -> None:
    facts = json.loads(FACTS.read_text())
    PNG_ROOT.mkdir(parents=True, exist_ok=True)
    run_variants([fig_router_effect], context_for, facts)
    print(f"figura del router · {len(facts['router_rows'])} barras · desde {FACTS.name}")


if __name__ == "__main__":
    main()
