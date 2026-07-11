"""Reporte EDA standalone -> reports/eda/eda_report.pdf (+ variante EN en eda/en/).

Empaqueta la galería de figuras insignia (G1-G11) en un PDF multi-página con portada,
resumen ejecutivo y notas metodológicas. TODAS las cifras vienen de eda_facts.json /
key_facts.json (0 a mano); el vintage es el del último boletín del panel.

Bilingüe (PENDIENTES #14): ``build(lang)`` emite el reporte en español (entregable
académico) y en inglés (lo sirve la página EN del sitio, que antes descargaba el PDF
en español). Las figuras usan la MISMA maquinaria de idioma de la galería
(``gallery._apply_lang``), así que texto editorial y figuras van siempre en el mismo
idioma.

Calidad: las páginas de figura se insertan como VECTOR (la figura viva de la galería
va directo a PdfPages — cero re-rasterización, nítido a cualquier zoom). Solo la
miniatura de la portada es raster. El texto de las páginas editoriales se envuelve
con textwrap explícito (ancho fijo en caracteres), nunca con wrap=True.

Gate de salida (C2): si el censo está incompleto, aborta con SystemExit — no se
publica un reporte mutilado. Presupuesto: <3 MB por PDF (hook large-files maxkb=3000).

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

from vp_data.config import DAYS_PER_YEAR  # noqa: E402
from vp_model.palette import BLUE, GRAY, INK, STRIPE, YELLOW  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EDA_DIR = ROOT / "reports" / "eda"
GALLERY = EDA_DIR / "gallery"
OUTS = {"es": EDA_DIR / "eda_report.pdf", "en": EDA_DIR / "en" / "eda_report.pdf"}
PAGE = (8.5, 11.0)  # carta vertical (páginas editoriales)
COVER_DPI = 200  # solo afecta la miniatura raster de la portada

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
COUNTRY = {
    "es": {
        "mexico": "México",
        "india": "India",
        "china": "China",
        "philippines": "Filipinas",
        "all_chargeability": "el resto del mundo",
    },
    "en": {
        "mexico": "Mexico",
        "india": "India",
        "china": "China",
        "philippines": "the Philippines",
        "all_chargeability": "the rest of the world",
    },
}

# Todo texto editorial visible vive aquí; los números se interpolan de los facts.
TXT: dict[str, dict] = {
    "es": {
        "brand": "VisaPredict AI",
        "org": "UACJ · MIAAD",
        "cover_title": "Análisis exploratorio del Visa Bulletin",
        "cover_sub": "La fila de las visas de Estados Unidos,\nmedida mes a mes desde 2001",
        "cover_auto": "Reporte automático generado con el boletín de {mes} {anio} —\nse rehace con cada boletín nuevo.",
        "tile_obs": "observaciones mensuales",
        "tile_series": "series país × cat. × tabla",
        "tile_months": "boletines desde dic-2001",
        "tile_f": "meses con fecha (F)",
        "sum_title": "Seis hallazgos en una página",
        "sum_sub": "Todos derivados del censo estadístico de este corte; ninguno escrito a mano.",
        "f_frozen": (
            "de los meses con fecha, la fila NO se movió — el congelamiento es el "
            "comportamiento dominante del sistema y la vara real de todo pronóstico."
        ),
        "f_retro": (
            "de los avances son retrocesos ({n} retrogresiones registradas); "
            "raros, pero de hasta {yrs:.1f} años en un solo mes."
        ),
        "f_backlog_big": "{yrs:.0f} años",
        "f_backlog": ("espera hoy la cola más larga ({pais} {cat}); ninguna serie familiar baja de {minyrs:.0f}."),
        "f_diff": (
            "series evaluables exigen diferenciación (ADF+KPSS coinciden): el panel es "
            "integrado de orden 1, sin excepciones estacionarias."
        ),
        "f_month_big": "{lo:.0f}–{hi:.0f} días",
        "f_month": (
            "es el rango del avance mediano por mes calendario: la "
            "estacionalidad del año fiscal es débil y no explotable."
        ),
        "f_cover_big": "{ne} de {ns}",
        "f_cover": (
            "series estructurales son plenamente evaluables (cobertura escalonada; {nf} tienen al menos una fecha)."
        ),
        "met_title": "Notas metodológicas y linaje",
        "met_source_t": "Fuente y panel",
        "met_source": (
            "U.S. Department of State, Visa Bulletin ({first} → {last}). "
            "Panel multiserie y(p,c,b,t): {nobs:,} filas, {ns} series estructurales, {nm} boletines "
            "(cobertura 100%). Variable objetivo: días desde la época base 1-ene-1975, solo meses con "
            "fecha publicada (estado F)."
        ),
        "met_regime_t": "Regímenes de celda",
        "met_regime": (
            "F = fecha publicada (objetivo predictivo) · C = Current, sin atraso · U = Unavailable · "
            "UNK = sin dato. C/U/UNK se conservan como anotación descriptiva; no se interpolan ni se puntúan."
        ),
        "met_census_t": "Censo estadístico",
        "met_census": (
            "Por serie: longitud, continuidad, huecos, retrogresiones, congelamiento y avance mediano. "
            "Sobre las series evaluables: ADF, KPSS y DF-GLS (estacionariedad), Ljung-Box (autocorrelación) "
            "y prueba ARCH (heteroscedasticidad) sobre los incrementos."
        ),
        "met_cov_t": "Cobertura escalonada",
        "met_cov": (
            "Estructural ({ns}) → con fechas ({nf}) → plenamente evaluable ({ne}, ≥84 obs F y span "
            "suficiente para el walk-forward). El sorteo de diversidad (DV) es un hecho descriptivo separado: "
            "rangos regionales, no fechas, fuera del objetivo predictivo."
        ),
        "met_repro_t": "Reproducibilidad",
        "met_repro": (
            "Generado por experiments/build_eda_facts.py + make_gallery_figures.py + build_eda_report.py "
            "(make eda-all && make eda-report) en el pipeline público github.com/UACJ-MIAAD/VisaPredictAI. "
            "Se regenera automáticamente con cada boletín nuevo; las cifras se validan contra la fuente "
            "única key_facts.json en integración continua."
        ),
        "met_notice_t": "Aviso",
        "met_notice": (
            "Documento académico y demostrativo (UACJ · MIAAD). No constituye asesoría migratoria ni "
            "predicción oficial."
        ),
        "footer": "VisaPredict AI · EDA del Visa Bulletin · corte {mes} {anio}",
        "meta_title": "VisaPredict AI — EDA del Visa Bulletin (corte {mes} {anio})",
        "meta_subject": "Análisis exploratorio automatizado del panel multiserie del U.S. Visa Bulletin",
    },
    "en": {
        "brand": "VisaPredict AI",
        "org": "UACJ · MIAAD",
        "cover_title": "Exploratory analysis of the Visa Bulletin",
        "cover_sub": "The United States visa queue,\nmeasured month by month since 2001",
        "cover_auto": "Automated report generated with the {mes} {anio} bulletin —\nrebuilt with every new bulletin.",
        "tile_obs": "monthly observations",
        "tile_series": "country × cat. × table series",
        "tile_months": "bulletins since Dec 2001",
        "tile_f": "months with a date (F)",
        "sum_title": "Six findings on one page",
        "sum_sub": "All derived from this cut's statistical census; none written by hand.",
        "f_frozen": (
            "of the months with a date, the queue did NOT move — freezing is the system's "
            "dominant behavior and the real yardstick for any forecast."
        ),
        "f_retro": (
            "of the movements are setbacks ({n} recorded retrogressions); rare, but of up to "
            "{yrs:.1f} years in a single month."
        ),
        "f_backlog_big": "{yrs:.0f} years",
        "f_backlog": ("is today's longest wait ({pais} {cat}); no family series waits less than {minyrs:.0f}."),
        "f_diff": (
            "evaluable series demand differencing (ADF and KPSS agree): the panel is "
            "integrated of order 1, with no stationary exceptions."
        ),
        "f_month_big": "{lo:.0f}–{hi:.0f} days",
        "f_month": (
            "is the range of the median advance per calendar month: fiscal-year seasonality is weak and not exploitable."
        ),
        "f_cover_big": "{ne} of {ns}",
        "f_cover": ("structural series are fully evaluable (tiered coverage; {nf} have at least one date)."),
        "met_title": "Methodological notes and lineage",
        "met_source_t": "Source and panel",
        "met_source": (
            "U.S. Department of State, Visa Bulletin ({first} → {last}). Multiseries panel y(p,c,b,t): "
            "{nobs:,} rows, {ns} structural series, {nm} bulletins (100% coverage). Target variable: days "
            "since the 1-Jan-1975 base epoch, only months with a published date (state F)."
        ),
        "met_regime_t": "Cell regimes",
        "met_regime": (
            "F = published date (the predictive target) · C = Current, no backlog · U = Unavailable · "
            "UNK = no data. C/U/UNK are kept as descriptive annotation; they are never interpolated or scored."
        ),
        "met_census_t": "Statistical census",
        "met_census": (
            "Per series: length, continuity, gaps, retrogressions, freezing and median advance. On the "
            "evaluable series: ADF, KPSS and DF-GLS (stationarity), Ljung-Box (autocorrelation) and the "
            "ARCH test (heteroskedasticity) on the increments."
        ),
        "met_cov_t": "Tiered coverage",
        "met_cov": (
            "Structural ({ns}) → with dates ({nf}) → fully evaluable ({ne}, ≥84 F obs and enough span for "
            "the walk-forward). The diversity lottery (DV) is a separate descriptive fact: regional ranks, "
            "not dates, outside the predictive target."
        ),
        "met_repro_t": "Reproducibility",
        "met_repro": (
            "Generated by experiments/build_eda_facts.py + make_gallery_figures.py + build_eda_report.py "
            "(make eda-all && make eda-report) in the public pipeline github.com/UACJ-MIAAD/VisaPredictAI. "
            "It is rebuilt automatically with every new bulletin; the figures are validated against the "
            "single source of truth key_facts.json in continuous integration."
        ),
        "met_notice_t": "Notice",
        "met_notice": (
            "Academic, demonstrative document (UACJ · MIAAD). It is not immigration advice nor an official prediction."
        ),
        "footer": "VisaPredict AI · Visa Bulletin EDA · {mes} {anio} cut",
        "meta_title": "VisaPredict AI — Visa Bulletin EDA ({mes} {anio} cut)",
        "meta_subject": "Automated exploratory analysis of the U.S. Visa Bulletin multiseries panel",
    },
}


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


def _month(lang: str, month: int) -> str:
    return (MES if lang == "es" else MES_EN)[month]


def _page_footer(ax: plt.Axes, vintage: str, n: int, t: dict, lang: str) -> None:
    per = pd.Period(vintage)
    ax.text(0.06, 0.032, t["footer"].format(mes=_month(lang, per.month), anio=per.year), fontsize=7.5, color=GRAY)
    ax.text(0.94, 0.032, f"{n}", fontsize=8.5, color=BLUE, ha="right", fontweight="bold")


def page_cover(pdf: PdfPages, facts: dict, t: dict, lang: str) -> None:
    """G12 — portada hero: identidad UACJ + vintage + stat tiles."""
    p = facts["panel"]
    per = pd.Period(facts["vintage"])
    fig, ax = _blank_page()
    ax.add_patch(plt.Rectangle((0, 0.86), 1, 0.14, color=BLUE))
    ax.add_patch(plt.Rectangle((0, 0.852), 1, 0.008, color=YELLOW))
    ax.text(0.06, 0.945, t["brand"], fontsize=13, color="white", fontweight="bold")
    ax.text(0.94, 0.945, t["org"], fontsize=10, color="white", ha="right")
    ax.text(0.06, 0.895, t["cover_title"], fontsize=21, color="white", fontweight="bold")

    ax.text(0.06, 0.79, t["cover_sub"], fontsize=15, color=INK)
    ax.text(
        0.06,
        0.735,
        t["cover_auto"].format(mes=_month(lang, per.month), anio=per.year),
        fontsize=9.5,
        color=GRAY,
        va="top",
    )
    tiles = [
        (f"{p['n_obs']:,}", t["tile_obs"]),
        (str(p["n_series_structural"]), t["tile_series"]),
        (f"{p['n_months']}/{p['n_months']}", t["tile_months"]),
        (f"{p['pct_trainable_F']}%", t["tile_f"]),
    ]
    for i, (big, small) in enumerate(tiles):
        x = 0.06 + i * 0.225
        ax.add_patch(plt.Rectangle((x, 0.585), 0.205, 0.10, color=STRIPE))
        ax.text(x + 0.015, 0.648, big, fontsize=16, color=BLUE, fontweight="bold")
        ax.text(x + 0.015, 0.607, small, fontsize=7.2, color=GRAY)
    # miniatura del idioma que corresponde (la galería EN ya existe en gallery/en/)
    hero_fp = (GALLERY / "en" / "g01_panel.png") if lang == "en" else (GALLERY / "g01_panel.png")
    hero = plt.imread(hero_fp if hero_fp.exists() else GALLERY / "g01_panel.png")
    hax = fig.add_axes((0.13, 0.05, 0.74, 0.49))
    hax.imshow(hero)
    hax.set_axis_off()
    pdf.savefig(fig, dpi=COVER_DPI)
    plt.close(fig)


def page_summary(pdf: PdfPages, facts: dict, t: dict, lang: str) -> None:
    """Resumen ejecutivo: hallazgos derivados del censo, uno por bloque."""
    p = facts["panel"]
    st = facts["stationarity_summary"]
    ev = pd.DataFrame(facts["retro_events"])
    bt = pd.DataFrame(facts["backlog_today"])
    top = bt.loc[bt.backlog_years.idxmax()]
    n_diff, n_ev = st.get("difference", 0), p["n_series_evaluable"]
    med = pd.Series(facts["monthly_advance_median"]).astype(float)
    findings = [
        (f"{p['pct_frozen']:.0f}%", t["f_frozen"]),
        (f"{p['pct_retro']:.1f}%", t["f_retro"].format(n=len(ev), yrs=ev.days.max() / DAYS_PER_YEAR)),
        (
            t["f_backlog_big"].format(yrs=top.backlog_years),
            t["f_backlog"].format(
                pais=COUNTRY[lang][top.country],
                cat=top.category,
                minyrs=bt[bt.block == "family"].backlog_years.min(),
            ),
        ),
        (f"{n_diff}/{n_ev}", t["f_diff"]),
        (t["f_month_big"].format(lo=med.min(), hi=med.max()), t["f_month"]),
        (
            t["f_cover_big"].format(ne=p["n_series_evaluable"], ns=p["n_series_structural"]),
            t["f_cover"].format(nf=p["n_series_with_F"]),
        ),
    ]
    fig, ax = _blank_page()
    ax.text(0.06, 0.93, t["sum_title"], fontsize=19, color=INK, fontweight="bold")
    ax.text(0.06, 0.90, t["sum_sub"], fontsize=9.5, color=GRAY)
    y = 0.82
    for big, small in findings:
        ax.add_patch(plt.Rectangle((0.06, y - 0.062), 0.014, 0.085, color=BLUE))
        ax.text(0.095, y, big, fontsize=17, color=BLUE, fontweight="bold", va="top")
        ax.text(0.095, y - 0.040, _wrap(small, 96), fontsize=9.5, color=INK, va="top", linespacing=1.5)
        y -= 0.125
    _page_footer(ax, facts["vintage"], 2, t, lang)
    pdf.savefig(fig)
    plt.close(fig)


def page_methods(pdf: PdfPages, facts: dict, n: int, t: dict, lang: str) -> None:
    p = facts["panel"]
    fig, ax = _blank_page()
    ax.text(0.06, 0.93, t["met_title"], fontsize=19, color=INK, fontweight="bold")
    fmt = dict(
        first=p["date_first"],
        last=p["date_last"],
        nobs=p["n_obs"],
        ns=p["n_series_structural"],
        nm=p["n_months"],
        nf=p["n_series_with_F"],
        ne=p["n_series_evaluable"],
    )
    blocks = [
        (t["met_source_t"], t["met_source"].format(**fmt)),
        (t["met_regime_t"], t["met_regime"]),
        (t["met_census_t"], t["met_census"]),
        (t["met_cov_t"], t["met_cov"].format(**fmt)),
        (t["met_repro_t"], t["met_repro"]),
        (t["met_notice_t"], t["met_notice"]),
    ]
    y = 0.86
    for title, body in blocks:
        ax.text(0.06, y, title, fontsize=11, color=BLUE, fontweight="bold", va="top")
        ax.text(0.06, y - 0.028, _wrap(body, 108), fontsize=8.8, color=INK, va="top", linespacing=1.5)
        y -= 0.132
    _page_footer(ax, facts["vintage"], n, t, lang)
    pdf.savefig(fig)
    plt.close(fig)


def build(lang: str = "es") -> Path:
    t = TXT[lang]
    out = OUTS[lang]
    out.parent.mkdir(parents=True, exist_ok=True)
    facts = _facts()
    per = pd.Period(facts["vintage"])
    df, gfacts = gallery._load()
    # figuras en el idioma del reporte, SIEMPRE tema claro (documento impreso);
    # restaurar es-claro al final para no contaminar a otros consumidores.
    gallery._apply_lang(lang)
    gallery._apply_theme(dark=False)
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
    try:
        with PdfPages(out) as pdf:
            # la portada usa la miniatura PNG de G1: generar las figuras primero
            figs = [make() for make in makers]
            page_cover(pdf, facts, t, lang)
            page_summary(pdf, facts, t, lang)
            for fig in figs:
                pdf.savefig(fig, bbox_inches="tight", pad_inches=0.35)
                plt.close(fig)
            page_methods(pdf, facts, len(makers) + 3, t, lang)
            meta = pdf.infodict()
            meta["Title"] = t["meta_title"].format(mes=_month(lang, per.month), anio=per.year)
            meta["Author"] = "Javier Augusto Rebull Saucedo (UACJ · MIAAD)"
            meta["Subject"] = t["meta_subject"]
            # H3: provenance machine-readable — el corte exacto que produjo este PDF.
            from vp_data.tracking import pipeline_run_id
            from vp_model.ledger import git_sha, panel_hash

            meta["Keywords"] = (
                f"vintage={facts.get('vintage', 'n/d')}; panel={panel_hash()}; git={git_sha()}; run={pipeline_run_id()}"
            )
    finally:
        gallery._apply_lang("es")
        gallery._apply_theme(dark=False)
    size_mb = out.stat().st_size / 1e6
    if size_mb >= 3.0:
        raise SystemExit(f"GATE EDA-REPORT: {size_mb:.1f} MB >= 3 MB (hook large-files).")
    print(
        f"eda_report[{lang}] OK — {len(makers) + 3} páginas (figuras en vector) · {size_mb:.2f} MB "
        f"· corte {facts['vintage']} -> {out}"
    )
    return out


if __name__ == "__main__":
    build("es")
    build("en")
