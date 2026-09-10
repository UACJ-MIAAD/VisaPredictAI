"""E5 · Figura del router por cohorte: efecto contra el naïve-1 y el margen material.

Se dibuja desde ``reports/governance/e5_facts.json`` y de ningún otro sitio: si una barra
contradice la tabla del `.tex`, es que alguien tecleó una cifra. La línea del margen material
está para que se vea de un golpe por qué una mejora grande a doce meses no basta cuando el corto
plazo no llega: la regla exige los tres horizontes.
"""

from __future__ import annotations

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
#: Añada del corte gobernado — derivada del censo, nunca tecleada.
VINTAGE = json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())["panel_vintage"]

TXT = {
    "es": {
        "headline": "Ningún router bate al naïve-1 de su cohorte en los tres horizontes",
        "sub": "efecto medio en MASE (positivo = el router mejora); la regla exige los tres",
        "x": "efecto en MASE frente al naïve-1 de la propia cohorte",
        "margin": "margen material (0.005)",
        "sig": "significativo tras Holm",
        "foot": "exploratorio · gate congelado antes de calcular · el router no se despliega",
        "footer": "Corte de {mes} de {anio} · panel gobernado",
    },
    "en": {
        "headline": "No router beats its cohort's naive-1 across all three horizons",
        "sub": "mean MASE effect (positive = router improves); the rule demands all three",
        "x": "MASE effect against the cohort's own naive-1",
        "margin": "material margin (0.005)",
        "sig": "significant after Holm",
        "foot": "exploratory · gate frozen before computing · the router is not deployed",
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

    fig, ax = plt.subplots(figsize=(9.2, 0.42 * len(filas) + 2.1))
    y = np.arange(len(filas))
    ax.barh(
        y,
        efectos,
        color=[color_ok if s else color_no for s in signif],
        edgecolor="none",
        height=0.66,
    )
    ax.axvline(0.0, lw=0.9, color=pal["INK"], alpha=0.55)
    for signo in (1, -1):
        ax.axvline(signo * facts["RouterMargen"], lw=0.9, ls="--", color=pal["GOLD"])
    ax.set_yticks(y, etiquetas, fontsize=8)
    ax.invert_yaxis()
    # Banda reservada ARRIBA, derivada del número de filas, para la leyenda del margen. Antes
    # el texto se anclaba en `len(filas) - 0.2`, es decir FUERA del área de trazado, y la línea
    # del eje lo atravesaba (M65: sólo se ve abriendo el PDF).
    ax.set_ylim(len(filas) - 0.5, -1.25)
    ax.set_xlabel(ctx.lang.txt["x"], fontsize=9)
    ax.tick_params(axis="x", labelsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.text(
        facts["RouterMargen"],
        -0.85,
        f"  {ctx.lang.txt['margin']}",
        fontsize=7.5,
        color=pal["GOLD"],
        va="center",
        ha="left",
    )
    header(fig, ctx.lang.txt["headline"], ctx.lang.txt["sub"], ctx)
    footer(fig, VINTAGE, ctx, extra=ctx.lang.txt["foot"])
    fig.tight_layout()
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
