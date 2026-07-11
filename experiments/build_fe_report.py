"""Reporte FE standalone -> reports/fe/fe_report.pdf (+ EN en reports/fe/en/), épica AF1.

Empaqueta el catálogo de ingeniería de características y limpieza (fe_facts.json) en
un PDF profesional multi-página: portada con stat tiles, las 12 decisiones magistrales
de limpieza, las 8 de FE, la galería f01–f07 EN VECTOR (la figura viva de
``make_fe_figures`` va directo a PdfPages — cero re-rasterización), el ledger del corte
vigente, la selección FRESH 44→1 y las notas de linaje. TODAS las cifras vienen de
fe_facts.json / el panel (0 a mano); bilingüe vía ``build(lang)`` reusando el TXT de la
galería FE.

Gates: fe_facts debe existir y su vintage debe SER el del panel (SystemExit si no);
presupuesto <3 MB por PDF (hook large-files maxkb=3000).

Uso (ante):  ante/bin/python experiments/build_fe_report.py   (o `make fe-report`)
"""

from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import make_fe_figures as fefig  # noqa: E402  (sys.path[0] = experiments/)
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

from vp_data.decisions_i18n import DECISIONS_EN  # noqa: E402
from vp_model.palette import BLUE, GRAY, INK, MID, STRIPE, YELLOW  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FE_DIR = ROOT / "reports" / "fe"
PANEL = ROOT / "data" / "processed" / "visa_panel_long.csv"
OUT = {"es": FE_DIR / "fe_report.pdf", "en": FE_DIR / "en" / "fe_report.pdf"}
PAGE = (8.5, 11.0)  # carta vertical (páginas editoriales)
COVER_DPI = 200  # solo afecta la miniatura raster de la portada

RTXT: dict[str, dict] = {
    "es": {
        "title": "Ingeniería de características y limpieza de datos",
        "cover_kicker": "VisaPredict AI",
        "cover_org": "UACJ · MIAAD",
        "cover_sub": "Cómo el Visa Bulletin crudo se convierte en un objetivo entrenable,\ndecisión por decisión y con ledger vivo",
        "cover_note": "Reporte automático generado con el boletín de {mes} {anio} —\nse rehace con cada boletín nuevo.",
        "tiles": [
            "decisiones de FE documentadas",
            "selección de características (FDR)",
            "tope de interpolación de huecos",
            "filas F entrenables",
        ],
        "tile_gap": "≤{cap} meses",
        "clean_title": "Decisiones magistrales de limpieza ({i} de {k})",
        "clean_sub": "Las {n} decisiones que blindan el dato crudo; cada una vive en código citado, no en prosa suelta.",
        "fe_title": "Decisiones magistrales de FE ({i} de {k})",
        "fe_sub": "Las {n} transformaciones que convierten fechas administrativas en regresores sin fuga.",
        "ledger_title": "Ledger de limpieza del corte vigente",
        "ledger_sub": "Publicado por pipeline/build_panel.py en cada build; el reporte lo re-lee, no lo re-calcula.",
        "ledger_rows": [
            ("vintage", "corte del panel"),
            ("n_rows", "filas del panel"),
            ("n_series", "series estructurales"),
            ("F", "filas F (fecha publicada)"),
            ("C", "filas C (Current)"),
            ("U", "filas U (Unavailable)"),
            ("UNK", "filas UNK (sin dato)"),
            ("dup_collapsed", "duplicados colapsados (preferencia F>C>U>UNK)"),
            ("bulletin_date_unparseable", "fechas de boletín imparseables"),
            ("f_priority_date_unparseable", "fechas F imparseables"),
            ("epoch_underflow", "desbordes de época (fecha F < t0)"),
            ("big_jumps_gt_8y", "saltos >8 años (anotados, no recortados)"),
        ],
        "ledger_note_title": "Por qué los ceros son ceros",
        "ledger_note": "Los ceros del ledger no son suerte: son inalcanzables por construcción. Una fecha F "
        "imparseable o un desborde de época ABORTAN el build en la causa (build_panel), y el almacén estrella "
        "re-verifica el contrato con CHECKs declarativos (days_iff_F, pdate_iff_F, days_is_datediff). Los "
        "{jumps} saltos >8 años sí existen: son eventos administrativos reales que el modelo debe tolerar — se "
        "anotan en la auditoría, jamás se recortan del dato.",
        "sel_title": "Selección de características: {nin} → {nrel} → {nsel}",
        "sel_sub": "FRESH (relevancia con FDR Benjamini-Yekutieli, α={alpha}) + des-redundancia mRMR "
        "(|Spearman|>0.9) sobre {ns} series con campaña canónica.",
        "sel_steps": ["características de entrada", "relevantes tras FDR", "finales tras des-redundancia"],
        "sel_survivor": "La superviviente",
        "sel_target_title": "Variable objetivo de la selección",
        "sel_reading": "Lectura honesta",
        "sel_reading_body": "Con {ns} series efectivas y {nin} candidatas, la corrección por FDR deja UNA sola "
        "característica de caracterización asociada de forma robusta a la dificultad de pronóstico. El catálogo "
        "completo se conserva como herramienta descriptiva del EDA; NO entra como covariable a los modelos.",
        "methods_title": "Notas metodológicas y linaje",
        "methods": [
            (
                "Fuente única",
                "Todas las cifras de este reporte provienen de reports/fe/fe_facts.json "
                "(generado por experiments/build_fe_facts.py, versión de builder v{ver}) y del panel "
                "data/processed/visa_panel_long.csv. Ninguna cifra visible está escrita a mano.",
            ),
            (
                "Código citado",
                "Cada decisión referencia su módulo vivo: vp_model/feature_builder.py (FE_DECISIONS, "
                "FE_VERSION), vp_data/cleaning.py (CLEANING_DECISIONS + ledger), vp_model/preprocess.py "
                "(huecos, diferenciación, calendario) y vp_data/visa_common.py (parser). El detalle narrativo "
                "vive en docs/CLEANING.md.",
            ),
            (
                "Figuras",
                "Las figuras f01–f07 se insertan como la MISMA figura viva de "
                "experiments/make_fe_figures.py en vector (cero re-rasterización); la galería PNG bilingüe "
                "(claro/oscuro) que consume el web sale del mismo script.",
            ),
            (
                "Reproducibilidad",
                "make fe-all (fe-facts + fe-figures + fe-report) en el pipeline público "
                "github.com/UACJ-MIAAD/VisaPredictAI. Se regenera automáticamente con cada boletín nuevo; el "
                "gate de vintage aborta si el catálogo va detrás del panel.",
            ),
            (
                "Aviso",
                "Documento académico y demostrativo (UACJ · MIAAD). No constituye asesoría migratoria ni "
                "predicción oficial.",
            ),
        ],
        "footer": "VisaPredict AI · FE y limpieza del Visa Bulletin · corte {mes} {anio}",
        "pdf_title": "VisaPredict AI — Ingeniería de características y limpieza (corte {mes} {anio})",
        "pdf_subject": "Catálogo automatizado de ingeniería de características y limpieza del panel multiserie "
        "del U.S. Visa Bulletin",
    },
    "en": {
        "title": "Feature engineering and data cleaning",
        "cover_kicker": "VisaPredict AI",
        "cover_org": "UACJ · MIAAD",
        "cover_sub": "How the raw Visa Bulletin becomes a trainable target,\ndecision by decision, with a living ledger",
        "cover_note": "Automatic report generated with the {mes} {anio} bulletin —\nit is rebuilt with every new bulletin.",
        "tiles": [
            "documented FE decisions",
            "feature selection (FDR)",
            "gap interpolation cap",
            "trainable F rows",
        ],
        "tile_gap": "≤{cap} months",
        "clean_title": "Master cleaning decisions ({i} of {k})",
        "clean_sub": "The {n} decisions that armor the raw data; each one lives in cited code, not loose prose.",
        "fe_title": "Master FE decisions ({i} of {k})",
        "fe_sub": "The {n} transformations that turn administrative dates into leakage-free regressors.",
        "ledger_title": "Cleaning ledger of the current cut",
        "ledger_sub": "Published by pipeline/build_panel.py on every build; the report re-reads it, never recomputes it.",
        "ledger_rows": [
            ("vintage", "panel cut"),
            ("n_rows", "panel rows"),
            ("n_series", "structural series"),
            ("F", "F rows (published date)"),
            ("C", "C rows (Current)"),
            ("U", "U rows (Unavailable)"),
            ("UNK", "UNK rows (no data)"),
            ("dup_collapsed", "duplicates collapsed (preference F>C>U>UNK)"),
            ("bulletin_date_unparseable", "unparseable bulletin dates"),
            ("f_priority_date_unparseable", "unparseable F dates"),
            ("epoch_underflow", "epoch underflows (F date < t0)"),
            ("big_jumps_gt_8y", ">8-year jumps (annotated, never trimmed)"),
        ],
        "ledger_note_title": "Why the zeros are zeros",
        "ledger_note": "The ledger's zeros are not luck: they are unreachable by construction. An unparseable F "
        "date or an epoch underflow ABORTS the build at the cause (build_panel), and the star-schema warehouse "
        "re-verifies the contract with declarative CHECKs (days_iff_F, pdate_iff_F, days_is_datediff). The "
        "{jumps} >8-year jumps do exist: they are real administrative events the model must tolerate — they are "
        "annotated in the audit, never trimmed from the data.",
        "sel_title": "Feature selection: {nin} → {nrel} → {nsel}",
        "sel_sub": "FRESH (relevance with Benjamini-Yekutieli FDR, α={alpha}) + mRMR de-redundancy "
        "(|Spearman|>0.9) over {ns} series with a canonical campaign.",
        "sel_steps": ["input features", "relevant after FDR", "final after de-redundancy"],
        "sel_survivor": "The survivor",
        "sel_target_title": "Selection target variable",
        "sel_reading": "Honest reading",
        "sel_reading_body": "With {ns} effective series and {nin} candidates, the FDR correction leaves a "
        "SINGLE characterization feature robustly associated with forecasting difficulty. The full catalog is "
        "kept as a descriptive EDA tool; it does NOT enter the models as a covariate.",
        "methods_title": "Methodological notes and lineage",
        "methods": [
            (
                "Single source",
                "Every figure in this report comes from reports/fe/fe_facts.json (generated by "
                "experiments/build_fe_facts.py, builder version v{ver}) and the panel "
                "data/processed/visa_panel_long.csv. No visible number is hand-typed.",
            ),
            (
                "Cited code",
                "Each decision references its living module: vp_model/feature_builder.py (FE_DECISIONS, "
                "FE_VERSION), vp_data/cleaning.py (CLEANING_DECISIONS + ledger), vp_model/preprocess.py "
                "(gaps, differencing, calendar) and vp_data/visa_common.py (parser). The narrative detail "
                "lives in docs/CLEANING.md.",
            ),
            (
                "Figures",
                "Figures f01–f07 are inserted as the SAME live figure from "
                "experiments/make_fe_figures.py, in vector (zero re-rasterization); the bilingual PNG gallery "
                "(light/dark) the website consumes comes from the same script.",
            ),
            (
                "Reproducibility",
                "make fe-all (fe-facts + fe-figures + fe-report) in the public pipeline "
                "github.com/UACJ-MIAAD/VisaPredictAI. It regenerates automatically with every new bulletin; "
                "the vintage gate aborts if the catalog falls behind the panel.",
            ),
            (
                "Notice",
                "Academic, demonstrative document (UACJ · MIAAD). It is not immigration advice nor an "
                "official prediction.",
            ),
        ],
        "footer": "VisaPredict AI · Visa Bulletin FE & cleaning · cut {mes} {anio}",
        "pdf_title": "VisaPredict AI — Feature engineering and cleaning ({mes} {anio} cut)",
        "pdf_subject": "Automated feature-engineering and cleaning catalog of the U.S. Visa Bulletin "
        "multi-series panel",
    },
}


def _facts() -> dict:
    fp = FE_DIR / "fe_facts.json"
    if not fp.exists():
        raise SystemExit("GATE FE-REPORT: falta reports/fe/fe_facts.json (corre `make fe-facts`).")
    facts = json.loads(fp.read_text())
    # gate de vintage: el catálogo debe SER el corte del panel (regla #0)
    panel_max = pd.to_datetime(pd.read_csv(PANEL, usecols=["bulletin_date"])["bulletin_date"]).max()
    if facts["vintage"] != panel_max.strftime("%Y-%m"):
        raise SystemExit(
            f"GATE FE-REPORT: fe_facts vintage {facts['vintage']} != panel {panel_max:%Y-%m}; "
            "corre `make fe-facts` antes del reporte."
        )
    return facts


def _decision(entry: dict, lang: str) -> tuple[str, str]:
    """(title, rationale) en el idioma pedido; fallback al español canónico."""
    if lang == "en":
        tr = DECISIONS_EN.get(entry["id"])
        if tr:
            return tr["title"], tr["rationale"]
    return entry["title"], entry["rationale"]


def _blank_page() -> tuple[plt.Figure, plt.Axes]:
    fig = plt.figure(figsize=PAGE)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    return fig, ax


def _wrap(s: str, width: int) -> str:
    return textwrap.fill(s, width=width)


def _page_footer(ax: plt.Axes, rt: dict, mes: str, anio: int, n: int) -> None:
    ax.text(0.06, 0.032, rt["footer"].format(mes=mes, anio=anio), fontsize=7.5, color=GRAY)
    ax.text(0.94, 0.032, f"{n}", fontsize=8.5, color=BLUE, ha="right", fontweight="bold")


def page_cover(pdf: PdfPages, facts: dict, rt: dict, mes: str, anio: int, hero: bytes) -> None:
    """Portada: identidad UACJ + vintage + 4 stat tiles derivados + miniatura del pipeline."""
    led = facts["cleaning_ledger"]
    fs = facts["feature_selection"]
    cap = int(facts["constants"]["max_interpolable_gap"])
    n_rows, n_f = int(led["n_rows"]), int(led["rows_by_status"]["F"])
    fig, ax = _blank_page()
    ax.add_patch(plt.Rectangle((0, 0.86), 1, 0.14, color=BLUE))
    ax.add_patch(plt.Rectangle((0, 0.852), 1, 0.008, color=YELLOW))
    ax.text(0.06, 0.945, rt["cover_kicker"], fontsize=13, color="white", fontweight="bold")
    ax.text(0.94, 0.945, rt["cover_org"], fontsize=10, color="white", ha="right")
    ax.text(0.06, 0.895, rt["title"], fontsize=21, color="white", fontweight="bold")

    ax.text(0.06, 0.79, rt["cover_sub"], fontsize=15, color=INK)
    ax.text(0.06, 0.735, rt["cover_note"].format(mes=mes, anio=anio), fontsize=9.5, color=GRAY, va="top")
    tiles = [
        (str(len(facts["fe_decisions"])), rt["tiles"][0]),
        (f"{fs['n_features_in']}→{fs['n_selected']}", rt["tiles"][1]),
        (rt["tile_gap"].format(cap=cap), rt["tiles"][2]),
        (f"{100 * n_f / n_rows:.0f}%", rt["tiles"][3]),
    ]
    for i, (big, small) in enumerate(tiles):
        x = 0.06 + i * 0.225
        ax.add_patch(plt.Rectangle((x, 0.585), 0.205, 0.10, color=STRIPE))
        ax.text(x + 0.015, 0.648, big, fontsize=16, color=BLUE, fontweight="bold")
        ax.text(x + 0.015, 0.607, _wrap(small, 24), fontsize=7.2, color=GRAY)
    img = plt.imread(io.BytesIO(hero), format="png")
    hax = fig.add_axes((0.08, 0.10, 0.84, 0.42))
    hax.imshow(img)
    hax.set_axis_off()
    pdf.savefig(fig, dpi=COVER_DPI)
    plt.close(fig)


def page_decisions(
    pdf: PdfPages,
    entries: list[dict],
    lang: str,
    rt: dict,
    mes: str,
    anio: int,
    *,
    title_key: str,
    sub_key: str,
    n_total: int,
    part: tuple[int, int],
    start_idx: int,
    page_no: int,
) -> None:
    """Una página de decisiones magistrales: chip numerado + título + módulo + porqué, cebra."""
    fig, ax = _blank_page()
    ax.text(
        0.06,
        0.93,
        rt[title_key].format(i=part[0], k=part[1]),
        fontsize=19,
        color=INK,
        fontweight="bold",
    )
    ax.text(0.06, 0.90, rt[sub_key].format(n=n_total), fontsize=9.5, color=GRAY)
    y = 0.84
    step = 0.132
    for j, entry in enumerate(entries):
        title, rationale = _decision(entry, lang)
        if j % 2 == 1:
            ax.add_patch(plt.Rectangle((0.04, y - step + 0.036), 0.92, step, color=STRIPE, zorder=0))
        ax.add_patch(plt.Rectangle((0.06, y - 0.030), 0.030, 0.030, color=BLUE, zorder=1))
        ax.text(
            0.075,
            y - 0.0145,
            str(start_idx + j),
            fontsize=10,
            color="white",
            fontweight="bold",
            ha="center",
            va="center",
            zorder=2,
        )
        ax.text(0.105, y, title, fontsize=10.5, color=BLUE, fontweight="bold", va="top")
        ax.text(0.105, y - 0.0245, entry["module"], fontsize=7.2, color=MID, va="top", family="monospace")
        ax.text(0.105, y - 0.046, _wrap(rationale, 104), fontsize=8.4, color=INK, va="top", linespacing=1.45)
        y -= step
    _page_footer(ax, rt, mes, anio, page_no)
    pdf.savefig(fig)
    plt.close(fig)


def page_ledger(pdf: PdfPages, facts: dict, rt: dict, mes: str, anio: int, page_no: int) -> None:
    led = facts["cleaning_ledger"]
    flat: dict[str, str] = {k: str(v) for k, v in led.items() if not isinstance(v, dict)}
    flat.update({k: fefig._num(int(v)) for k, v in led["rows_by_status"].items()})
    flat["n_rows"] = fefig._num(int(led["n_rows"]))
    fig, ax = _blank_page()
    ax.text(0.06, 0.93, rt["ledger_title"], fontsize=19, color=INK, fontweight="bold")
    ax.text(0.06, 0.90, rt["ledger_sub"], fontsize=9.5, color=GRAY)
    y = 0.84
    for i, (key, label) in enumerate(rt["ledger_rows"]):
        if i % 2 == 1:
            ax.add_patch(
                plt.Rectangle(
                    (
                        0.04,
                        y - 0.012,
                    ),
                    0.92,
                    0.036,
                    color=STRIPE,
                    zorder=0,
                )
            )
        ax.text(0.07, y, label, fontsize=9.5, color=INK, va="center")
        ax.text(0.90, y, flat[key], fontsize=10, color=BLUE, fontweight="bold", ha="right", va="center")
        y -= 0.036
    y -= 0.05
    ax.text(0.06, y, rt["ledger_note_title"], fontsize=12, color=BLUE, fontweight="bold", va="top")
    ax.text(
        0.06,
        y - 0.032,
        _wrap(rt["ledger_note"].format(jumps=int(led["big_jumps_gt_8y"])), 104),
        fontsize=9.2,
        color=INK,
        va="top",
        linespacing=1.5,
    )
    _page_footer(ax, rt, mes, anio, page_no)
    pdf.savefig(fig)
    plt.close(fig)


def page_selection(pdf: PdfPages, facts: dict, rt: dict, mes: str, anio: int, page_no: int) -> None:
    fs = facts["feature_selection"]
    fig, ax = _blank_page()
    ax.text(
        0.06,
        0.93,
        rt["sel_title"].format(nin=fs["n_features_in"], nrel=fs["n_relevant"], nsel=fs["n_selected"]),
        fontsize=19,
        color=INK,
        fontweight="bold",
    )
    ax.text(
        0.06,
        0.90,
        _wrap(rt["sel_sub"].format(alpha=fs["alpha_fdr"], ns=fs["n_series"]), 110),
        fontsize=9.5,
        color=GRAY,
        va="top",
    )
    # embudo: tres números grandes con flechas
    nums = [fs["n_features_in"], fs["n_relevant"], fs["n_selected"]]
    for i, (n, label) in enumerate(zip(nums, rt["sel_steps"], strict=True)):
        x = 0.10 + i * 0.30
        ax.add_patch(plt.Rectangle((x, 0.68), 0.22, 0.12, color=STRIPE))
        ax.text(x + 0.11, 0.755, str(n), fontsize=26, color=BLUE, fontweight="bold", ha="center")
        ax.text(x + 0.11, 0.695, _wrap(label, 22), fontsize=8, color=GRAY, ha="center")
        if i < 2:
            ax.annotate(
                "",
                xy=(x + 0.30, 0.74),
                xytext=(x + 0.235, 0.74),
                arrowprops={"arrowstyle": "-|>", "color": MID, "lw": 1.4},
            )
    ax.text(0.06, 0.60, rt["sel_survivor"], fontsize=12, color=BLUE, fontweight="bold")
    ax.add_patch(plt.Rectangle((0.06, 0.52), 0.55, 0.05, color=STRIPE))
    ax.text(0.075, 0.545, ", ".join(fs["selected"]), fontsize=11, color=INK, family="monospace", va="center")
    ax.text(0.06, 0.46, rt["sel_target_title"], fontsize=12, color=BLUE, fontweight="bold")
    ax.text(0.06, 0.432, _wrap(fs["target"], 104), fontsize=9.2, color=INK, va="top", linespacing=1.5)
    ax.text(0.06, 0.34, rt["sel_reading"], fontsize=12, color=BLUE, fontweight="bold")
    ax.text(
        0.06,
        0.312,
        _wrap(rt["sel_reading_body"].format(ns=fs["n_series"], nin=fs["n_features_in"]), 104),
        fontsize=9.2,
        color=INK,
        va="top",
        linespacing=1.5,
    )
    _page_footer(ax, rt, mes, anio, page_no)
    pdf.savefig(fig)
    plt.close(fig)


def page_methods(pdf: PdfPages, facts: dict, rt: dict, mes: str, anio: int, page_no: int) -> None:
    fig, ax = _blank_page()
    ax.text(0.06, 0.93, rt["methods_title"], fontsize=19, color=INK, fontweight="bold")
    y = 0.86
    for title, body in rt["methods"]:
        ax.text(0.06, y, title, fontsize=11, color=BLUE, fontweight="bold", va="top")
        ax.text(
            0.06,
            y - 0.028,
            _wrap(body.format(ver=facts["fe_version"]), 108),
            fontsize=8.8,
            color=INK,
            va="top",
            linespacing=1.5,
        )
        y -= 0.132
    _page_footer(ax, rt, mes, anio, page_no)
    pdf.savefig(fig)
    plt.close(fig)


def build(lang: str) -> Path:
    facts = _facts()
    rt = RTXT[lang]
    per = pd.Period(facts["vintage"])
    mes_map = fefig.MES if lang == "es" else fefig.MES_EN
    mes, anio = mes_map[per.month], per.year
    out = OUT[lang]
    out.parent.mkdir(parents=True, exist_ok=True)

    # figuras VIVAS en el idioma del reporte, tema claro (entregable en papel)
    fefig._apply_lang(lang)
    fefig._apply_theme(dark=False)
    df, gfacts = fefig._load()
    # orden narrativo = flujo del dato: pipeline -> parser -> régimen -> huecos -> FE
    makers = (
        fefig.f07_pipeline,
        fefig.f06_parser,
        fefig.f05_regime,
        fefig.f04_gaps,
        fefig.f01_differencing,
        fefig.f02_calendar,
        fefig.f03_importance,
    )
    figs = [mk(df, gfacts) for mk in makers]
    hero_buf = io.BytesIO()
    figs[0].savefig(hero_buf, format="png", dpi=150, bbox_inches="tight")

    clean = list(facts["cleaning_decisions"])
    fe = list(facts["fe_decisions"])
    with PdfPages(out) as pdf:
        page_cover(pdf, facts, rt, mes, anio, hero_buf.getvalue())
        page_no = 2
        # decisiones de limpieza (2 páginas de 6) y de FE (2 páginas de 4)
        for block, title_key, sub_key, size in (
            (clean, "clean_title", "clean_sub", 6),
            (fe, "fe_title", "fe_sub", 4),
        ):
            chunks = [block[i : i + size] for i in range(0, len(block), size)]
            for ci, chunk in enumerate(chunks):
                page_decisions(
                    pdf,
                    chunk,
                    lang,
                    rt,
                    mes,
                    anio,
                    title_key=title_key,
                    sub_key=sub_key,
                    n_total=len(block),
                    part=(ci + 1, len(chunks)),
                    start_idx=ci * size + 1,
                    page_no=page_no,
                )
                page_no += 1
        for fig in figs:
            pdf.savefig(fig, bbox_inches="tight", pad_inches=0.35)
            plt.close(fig)
            page_no += 1
        page_ledger(pdf, facts, rt, mes, anio, page_no)
        page_selection(pdf, facts, rt, mes, anio, page_no + 1)
        page_methods(pdf, facts, rt, mes, anio, page_no + 2)
        n_pages = page_no + 2
        led = facts["cleaning_ledger"]
        fs = facts["feature_selection"]
        meta = pdf.infodict()
        meta["Title"] = rt["pdf_title"].format(mes=mes, anio=anio)
        meta["Author"] = "Javier Rebull"
        meta["Subject"] = rt["pdf_subject"]
        # stats embebidos verificables por tests/test_fe_report.py contra fe_facts (regla #0)
        # H3: provenance machine-readable (aditiva — el test parsea pares k=v).
        from vp_data.tracking import pipeline_run_id
        from vp_model.ledger import git_sha, panel_hash

        meta["Keywords"] = (
            f"vintage={facts['vintage']}; fe_version={facts['fe_version']}; "
            f"n_rows={led['n_rows']}; n_series={led['n_series']}; rows_F={led['rows_by_status']['F']}; "
            f"features_in={fs['n_features_in']}; selected={fs['n_selected']}; "
            f"panel={panel_hash()}; git={git_sha()}; run={pipeline_run_id()}"
        )
    size_mb = out.stat().st_size / 1e6
    if size_mb >= 3.0:
        raise SystemExit(f"GATE FE-REPORT: {size_mb:.1f} MB >= 3 MB (hook large-files).")
    print(
        f"fe_report [{lang}] OK — {n_pages} páginas (figuras en vector) · {size_mb:.2f} MB · "
        f"corte {facts['vintage']} -> {out}"
    )
    return out


if __name__ == "__main__":
    for lang in ("es", "en"):
        build(lang)
    fefig._apply_lang("es")
    fefig._apply_theme(dark=False)
