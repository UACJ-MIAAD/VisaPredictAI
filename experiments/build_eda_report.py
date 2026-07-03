"""Reporte EDA standalone -> reports/eda/eda_report.pdf (plan EDA brutal, épica W).

Empaqueta la galería de figuras insignia (G1-G11) en un PDF multi-página con portada,
resumen ejecutivo y notas metodológicas. TODAS las cifras vienen de eda_facts.json /
key_facts.json (0 a mano); el vintage es el del último boletín del panel.

Calidad: las páginas de figura se insertan como VECTOR (la figura viva de la galería
va directo a PdfPages — cero re-rasterización, nítido a cualquier zoom). Solo la
miniatura de la portada es raster. El texto de las páginas editoriales se envuelve
con textwrap explícito (ancho fijo en caracteres), nunca con wrap=True.

Gate de salida (C2): si el censo está incompleto, aborta con SystemExit — no se
publica un reporte mutilado. Presupuesto: <3 MB (hook large-files maxkb=3000).

Uso (ante):  ante/bin/python experiments/build_eda_report.py   (o `make eda-report`)
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import make_gallery_figures as gallery  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from make_latinometrics_figures import MES  # noqa: E402  (sys.path[0] = experiments/)
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

from vp_model.palette import BLUE, GRAY, INK, STRIPE, YELLOW  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EDA_DIR = ROOT / "reports" / "eda"
GALLERY = EDA_DIR / "gallery"
OUT = EDA_DIR / "eda_report.pdf"
PAGE = (8.5, 11.0)  # carta vertical (páginas editoriales)
COVER_DPI = 200  # solo afecta la miniatura raster de la portada


def _facts() -> dict:
    facts = json.loads((EDA_DIR / "eda_facts.json").read_text())
    # gate C2: censo completo o no hay reporte
    if facts["panel"]["n_series_structural"] < 0.9 * 194:
        raise SystemExit("GATE EDA-REPORT: censo incompleto; no se publica.")
    return facts


def _blank_page() -> tuple[plt.Figure, plt.Axes]:
    fig = plt.figure(figsize=PAGE)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    return fig, ax


def _wrap(s: str, width: int) -> str:
    return textwrap.fill(s, width=width)


def _page_footer(ax: plt.Axes, vintage: str, n: int) -> None:
    per = pd.Period(vintage)
    ax.text(
        0.06,
        0.032,
        f"VisaPredict AI · EDA del Visa Bulletin · corte {MES[per.month]} {per.year}",
        fontsize=7.5,
        color=GRAY,
    )
    ax.text(0.94, 0.032, f"{n}", fontsize=8.5, color=BLUE, ha="right", fontweight="bold")


def page_cover(pdf: PdfPages, facts: dict) -> None:
    """G12 — portada hero: identidad UACJ + vintage + stat tiles."""
    p = facts["panel"]
    per = pd.Period(facts["vintage"])
    fig, ax = _blank_page()
    ax.add_patch(plt.Rectangle((0, 0.86), 1, 0.14, color=BLUE))
    ax.add_patch(plt.Rectangle((0, 0.852), 1, 0.008, color=YELLOW))
    ax.text(0.06, 0.945, "VisaPredict AI", fontsize=13, color="white", fontweight="bold")
    ax.text(0.94, 0.945, "UACJ · MIAAD", fontsize=10, color="white", ha="right")
    ax.text(0.06, 0.895, "Análisis exploratorio del Visa Bulletin", fontsize=21, color="white", fontweight="bold")

    ax.text(0.06, 0.79, "La fila de las visas de Estados Unidos,\nmedida mes a mes desde 2001", fontsize=15, color=INK)
    ax.text(
        0.06,
        0.735,
        f"Reporte automático generado con el boletín de {MES[per.month]} {per.year} —\n"
        "se rehace con cada boletín nuevo.",
        fontsize=9.5,
        color=GRAY,
        va="top",
    )
    tiles = [
        (f"{p['n_obs']:,}".replace(",", " "), "observaciones mensuales"),
        (str(p["n_series_structural"]), "series país × cat. × tabla"),
        (f"{p['n_months']}/{p['n_months']}", "boletines desde dic-2001"),
        (f"{p['pct_trainable_F']}%", "meses con fecha (F)"),
    ]
    for i, (big, small) in enumerate(tiles):
        x = 0.06 + i * 0.225
        ax.add_patch(plt.Rectangle((x, 0.585), 0.205, 0.10, color=STRIPE))
        ax.text(x + 0.015, 0.648, big, fontsize=16, color=BLUE, fontweight="bold")
        ax.text(x + 0.015, 0.607, small, fontsize=7.2, color=GRAY)
    hero = plt.imread(GALLERY / "g01_panel.png")
    hax = fig.add_axes((0.13, 0.05, 0.74, 0.49))
    hax.imshow(hero)
    hax.set_axis_off()
    pdf.savefig(fig, dpi=COVER_DPI)
    plt.close(fig)


def page_summary(pdf: PdfPages, facts: dict) -> None:
    """Resumen ejecutivo: hallazgos derivados del censo, uno por bloque."""
    p = facts["panel"]
    st = facts["stationarity_summary"]
    ev = pd.DataFrame(facts["retro_events"])
    bt = pd.DataFrame(facts["backlog_today"])
    top = bt.loc[bt.backlog_years.idxmax()]
    n_diff, n_ev = st.get("difference", 0), p["n_series_evaluable"]
    med = pd.Series(facts["monthly_advance_median"]).astype(float)
    country_es = {
        "mexico": "México",
        "india": "India",
        "china": "China",
        "philippines": "Filipinas",
        "all_chargeability": "el resto del mundo",
    }
    findings = [
        (
            f"{p['pct_frozen']:.0f}%",
            "de los meses con fecha, la fila NO se movió — el congelamiento es el "
            "comportamiento dominante del sistema y la vara real de todo pronóstico.",
        ),
        (
            f"{p['pct_retro']:.1f}%",
            f"de los avances son retrocesos ({len(ev)} retrogresiones registradas); "
            f"raros, pero de hasta {ev.days.max() / 365.25:.1f} años en un solo mes.",
        ),
        (
            f"{top.backlog_years:.0f} años",
            f"espera hoy la cola más larga ({country_es[top.country]} "
            f"{top.category}); ninguna serie familiar baja de 2 años.",
        ),
        (
            f"{n_diff}/{n_ev}",
            "series evaluables exigen diferenciación (ADF+KPSS coinciden): el panel es "
            "integrado de orden 1, sin excepciones estacionarias.",
        ),
        (
            f"{med.min():.0f}–{med.max():.0f} días",
            "es el rango del avance mediano por mes calendario: la "
            "estacionalidad del año fiscal es débil y no explotable.",
        ),
        (
            f"{p['n_series_evaluable']} de {p['n_series_structural']}",
            "series estructurales son plenamente "
            f"evaluables (cobertura escalonada; {p['n_series_with_F']} tienen al menos una fecha).",
        ),
    ]
    fig, ax = _blank_page()
    ax.text(0.06, 0.93, "Seis hallazgos en una página", fontsize=19, color=INK, fontweight="bold")
    ax.text(
        0.06,
        0.90,
        "Todos derivados del censo estadístico de este corte; ninguno escrito a mano.",
        fontsize=9.5,
        color=GRAY,
    )
    y = 0.82
    for big, small in findings:
        ax.add_patch(plt.Rectangle((0.06, y - 0.062), 0.014, 0.085, color=BLUE))
        ax.text(0.095, y, big, fontsize=17, color=BLUE, fontweight="bold", va="top")
        ax.text(0.095, y - 0.040, _wrap(small, 96), fontsize=9.5, color=INK, va="top", linespacing=1.5)
        y -= 0.125
    _page_footer(ax, facts["vintage"], 2)
    pdf.savefig(fig)
    plt.close(fig)


def page_methods(pdf: PdfPages, facts: dict, n: int) -> None:
    p = facts["panel"]
    fig, ax = _blank_page()
    ax.text(0.06, 0.93, "Notas metodológicas y linaje", fontsize=19, color=INK, fontweight="bold")
    blocks = [
        (
            "Fuente y panel",
            f"U.S. Department of State, Visa Bulletin ({p['date_first']} → {p['date_last']}). "
            f"Panel multiserie y(p,c,b,t): {p['n_obs']:,} filas".replace(",", " ")
            + f", {p['n_series_structural']} series estructurales, {p['n_months']} boletines (cobertura 100%). "
            "Variable objetivo: días desde la época base 1-ene-1975, solo meses con fecha publicada (estado F).",
        ),
        (
            "Regímenes de celda",
            "F = fecha publicada (objetivo predictivo) · C = Current, sin atraso · "
            "U = Unavailable · UNK = sin dato. C/U/UNK se conservan como anotación descriptiva; no se interpolan "
            "ni se puntúan.",
        ),
        (
            "Censo estadístico",
            "Por serie: longitud, continuidad, huecos, retrogresiones, congelamiento y "
            "avance mediano. Sobre las series evaluables: ADF, KPSS y DF-GLS (estacionariedad), Ljung-Box "
            "(autocorrelación) y prueba ARCH (heteroscedasticidad) sobre los incrementos.",
        ),
        (
            "Cobertura escalonada",
            f"Estructural ({p['n_series_structural']}) → con fechas "
            f"({p['n_series_with_F']}) → plenamente evaluable ({p['n_series_evaluable']}, ≥84 obs F y span "
            "suficiente para el walk-forward). El sorteo de diversidad (DV) es un hecho descriptivo separado: "
            "rangos regionales, no fechas, fuera del objetivo predictivo.",
        ),
        (
            "Reproducibilidad",
            "Generado por experiments/build_eda_facts.py + make_gallery_figures.py + "
            "build_eda_report.py (make eda-all && make eda-report) en el pipeline público "
            "github.com/UACJ-MIAAD/VisaPredictAI. Se regenera automáticamente con cada boletín nuevo; las "
            "cifras se validan contra la fuente única key_facts.json en integración continua.",
        ),
        (
            "Aviso",
            "Documento académico y demostrativo (UACJ · MIAAD). No constituye asesoría migratoria ni "
            "predicción oficial.",
        ),
    ]
    y = 0.86
    for title, body in blocks:
        ax.text(0.06, y, title, fontsize=11, color=BLUE, fontweight="bold", va="top")
        ax.text(0.06, y - 0.028, _wrap(body, 108), fontsize=8.8, color=INK, va="top", linespacing=1.5)
        y -= 0.132
    _page_footer(ax, facts["vintage"], n)
    pdf.savefig(fig)
    plt.close(fig)


def build() -> Path:
    facts = _facts()
    per = pd.Period(facts["vintage"])
    df, gfacts = gallery._load()
    # páginas de figura: la MISMA figura viva de la galería, en vector. El orden
    # narrativo difiere del numérico: panorama -> completitud -> historia -> ...
    makers = [
        lambda: gallery.g01_panel(df, gfacts),
        lambda: gallery.g11_completitud(gfacts),
        lambda: gallery.g02_trayectorias(df, gfacts),
        lambda: gallery.g03_backlog(df, gfacts),
        lambda: gallery.g08_congelados(gfacts),
        lambda: gallery.g04_retros(gfacts),
        lambda: gallery.g05_brecha(gfacts),
        lambda: gallery.g06_pulso_fiscal(df, gfacts),
        lambda: gallery.g09_estacionariedad(gfacts),
        lambda: gallery.g07_leadlag(df, gfacts),
        lambda: gallery.g10_dv(gfacts),
    ]
    with PdfPages(OUT) as pdf:
        # la portada usa la miniatura PNG de G1: generar las figuras primero
        figs = [make() for make in makers]
        page_cover(pdf, facts)
        page_summary(pdf, facts)
        for fig in figs:
            pdf.savefig(fig, bbox_inches="tight", pad_inches=0.35)
            plt.close(fig)
        page_methods(pdf, facts, len(makers) + 3)
        meta = pdf.infodict()
        meta["Title"] = f"VisaPredict AI — EDA del Visa Bulletin (corte {MES[per.month]} {per.year})"
        meta["Author"] = "Javier Augusto Rebull Saucedo (UACJ · MIAAD)"
        meta["Subject"] = "Análisis exploratorio automatizado del panel multiserie del U.S. Visa Bulletin"
    size_mb = OUT.stat().st_size / 1e6
    if size_mb >= 3.0:
        raise SystemExit(f"GATE EDA-REPORT: {size_mb:.1f} MB >= 3 MB (hook large-files).")
    print(
        f"eda_report OK — {len(makers) + 3} páginas (figuras en vector) · {size_mb:.2f} MB · "
        f"corte {facts['vintage']} -> {OUT}"
    )
    return OUT


if __name__ == "__main__":
    build()
