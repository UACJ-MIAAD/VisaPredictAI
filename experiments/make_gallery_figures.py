"""Galería de figuras insignia del EDA (plan EDA brutal, épica W).

Once figuras G1-G11 con gramática editorial (titular-frase con el HALLAZGO, anotación
directa sobre los datos, banderas, paleta canónica) que alimentan el reporte PDF
(``build_eda_report.py``), el .tex y la web. Los titulares se DERIVAN de los datos
en cada corrida (regla #0: nada congelado que pueda desalinearse del panel).

Nota de accesibilidad: la paleta COUNTRY tiene un par débil bajo deuteranopia
(Filipinas teal vs Resto pizarra), por eso NINGUNA figura identifica series solo
por color — siempre hay etiqueta directa, bandera o valor al lado del dato.

Salida cuádruple (idioma × tema): la pasada ES-clara escribe reports/latex/Figures/
eda3_g*.pdf (vector, .tex) + reports/eda/gallery/g*.png (300 dpi, reporte/web); las
otras tres solo PNG — gallery/dark/ (web ES oscuro), gallery/en/ y gallery/en/dark/
(web EN; las rutas /en/* servían PNGs con texto español rasterizado). El PDF del
reporte y el .tex viven SOLO en español (entregable académico).
Corre en `ante`:  ante/bin/python experiments/make_gallery_figures.py  (o `make eda-all`)
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from make_latinometrics_figures import MES, _flag  # noqa: E402  (sys.path[0] = experiments/)
from matplotlib.colors import ListedColormap  # noqa: E402
from matplotlib.offsetbox import AnnotationBbox, OffsetImage  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from vp_model import palette as _palette  # noqa: E402
from vp_model.config import DAYS_PER_YEAR, days_to_year  # noqa: E402
from vp_model.palette import (  # noqa: E402
    BLUE,
    COUNTRY,
    COUNTRY_NAME,
    DIV,
    GOLD,
    GRAY,
    GRID,
    INK,
    MID,
    MUTE,
    SEQ,
    SLATE,
    TEAL,
    WINE,
    style,
)

ROOT = Path(__file__).resolve().parents[1]
FIG_TEX = ROOT / "reports" / "latex" / "Figures"
FIG_PNG = ROOT / "reports" / "eda" / "gallery"
PILOT = ("mexico", "india", "china", "philippines", "all_chargeability")
FAM = ["F1", "F2A", "F2B", "F3", "F4"]
EB = ["EB1", "EB2", "EB3", "EB4", "EB5"]
style()
plt.rcParams.update({"font.size": 9, "axes.grid": False})

# --- Tema claro/oscuro -----------------------------------------------------------------
# La galeria se emite DOS veces: clara (LaTeX/PDF/web claro) y oscura (web dark mode).
# El tema oscuro canonico vive en vp_model.palette.DARK (fuente unica); aqui solo se
# re-vinculan los nombres de color del modulo antes de cada pasada.
# AE3: los neutros claros viven en palette.LIGHT (fuente única, espejo de DARK);
# aquí solo se materializan los vínculos iniciales del módulo.
_LIGHT = _palette.LIGHT
PAPER = _LIGHT["PAPER"]
UNK_FILL = _LIGHT["UNK_FILL"]  # celdas U/sin dato (G1) — reconciliado con REGIME["UNK"]
NODATA = _LIGHT["NODATA"]  # barras "sin dato" (G11)
QUAD_BLUE = _LIGHT["QUAD_BLUE"]  # cuadrante sombreado (G9)
DARK_MODE = False


def _apply_theme(dark: bool) -> None:
    """Re-vincula los colores del modulo y los rcParams al tema pedido."""
    global PAPER, INK, GRAY, MID, MUTE, GRID, BLUE, TEAL, WINE, GOLD, SLATE
    global UNK_FILL, NODATA, QUAD_BLUE, COUNTRY, SEQ, DIV, DARK_MODE
    src: dict = _palette.DARK if dark else _LIGHT
    DARK_MODE = dark
    PAPER, INK, GRAY, MID, MUTE, GRID = src["PAPER"], src["INK"], src["GRAY"], src["MID"], src["MUTE"], src["GRID"]
    BLUE, TEAL, WINE, GOLD, SLATE = src["BLUE"], src["TEAL"], src["WINE"], src["GOLD"], src["SLATE"]
    UNK_FILL, NODATA, QUAD_BLUE = src["UNK_FILL"], src["NODATA"], src["QUAD_BLUE"]
    COUNTRY, SEQ, DIV = src["COUNTRY"], src["SEQ"], src["DIV"]
    plt.rcParams.update(
        {
            "figure.facecolor": PAPER,
            "axes.facecolor": PAPER,
            "savefig.facecolor": PAPER,
            "axes.edgecolor": MID,
            "axes.labelcolor": INK,
            "axes.titlecolor": BLUE,
            "xtick.color": GRAY,
            "ytick.color": GRAY,
            "text.color": INK,
            "grid.color": GRID,
        }
    )


# --- Idioma ----------------------------------------------------------------------------
# La galeria se emite en DOS idiomas (× dos temas = 4 pasadas). Todo texto visible vive
# en TXT; el codigo consume el vinculo global T/CNAME/MESL que _apply_lang() re-apunta.
# La terminologia EN es la MISMA que los captions del web (eda-gallery.tsx) — regla #0.
LANG = "es"
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
COUNTRY_NAME_EN = {
    **COUNTRY_NAME,
    "mexico": "Mexico",
    "philippines": "Philippines",
    "all_chargeability": "Rest of world",
}
TXT: dict[str, dict] = {
    "es": {
        "footer": "Fuente: U.S. Department of State, Visa Bulletin (dic 2001 – {mes} {anio}).",
        "blk_family": "Familiar",
        "blk_employment": "Empleo",
        "g01_head": "{n} meses de fila en una sola imagen",
        "g01_sub": "Cada fila es una serie país × categoría × tabla ({n} series); cada celda, "
        "un boletín mensual. El {pct}% de los meses con fecha la fila no se movió.",
        "leg_adv": "la fecha avanza",
        "leg_frozen": "congelada",
        "leg_retro": "retrocede",
        "leg_current": "Current (sin atraso)",
        "leg_nodata": "U / sin dato",
        "g01_foot": "Blanco = la serie no existe ese mes (p. ej. DFF antes de 2015).",
        "g02_ylabel": "Fecha de prioridad que se atiende (año)",
        "g02_quake": "{mes} {anio}: {n} series\nretroceden {yrs} años acumulados",
        "g02_pand": "pandemia:\n{pct} congelado",
        "g02_head": "Un cuarto de siglo de fila, serie por serie",
        "g02_sub": "Trayectoria de las 25 series familiares (FAD). El avance mediano es de {med} días por mes: "
        "la cola casi nunca corre, y a veces retrocede.",
        "g03_current": "todas las áreas\nen Current",
        "g03_head": "{cf} {catf} espera {yf} años; {ce} {cate}, {ye}",
        "g03_sub": "Años de atraso vigentes (FAD) por categoría y área de cargabilidad; México resaltado. "
        "Las áreas en Current (sin atraso) no aparecen.",
        "g03_foot": "Atraso = mes del boletín − fecha de prioridad vigente.",
        "g04_note": "{name} ({table})\n{mes} {anio}: −{yrs} años",
        "g04_ylabel": "Retroceso del mes (años)",
        "g04_head": "{n} veces la fila retrocedió — y unas pocas fueron terremotos",
        "g04_sub": "Cada punto es un mes de retrogresión en alguna de las series (el {pct}% de los avances "
        "observados); el tamaño es proporcional al retroceso.",
        "g05_gap": "la brecha más ancha: {yrs} años",
        "g05_fad": "FAD (acción final)",
        "g05_dff": "DFF (presentación)",
        "g05_xlabel": "Años de atraso vigentes",
        "g05_head": "Presentar el trámite {n} meses antes: eso vale la tabla DFF",
        "g05_sub": "Atraso vigente por serie según la tabla que se mire: la acción final (FAD) siempre va detrás "
        "del calendario de presentación (DFF).",
        "g05_foot": "Series con ambas tablas publicadas hoy.",
        "g06_cb": "avance mediano (días)",
        "g06_era": "la era congelada: FY{a}–FY{b}",
        "g06_months": ["oct", "nov", "dic", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep"],
        "g06_ylabel": "mediana\n(días)",
        "g06_kick": "arranque del año fiscal",
        "g06_head": "El año fiscal apenas late: el mes típico avanza entre {lo} y {hi} días",
        "g06_sub": "Avance mediano del panel completo por mes (columnas en orden del año fiscal, octubre primero) "
        "y por año fiscal (filas). No hay estacionalidad explotable — un hallazgo, no una carencia.",
        "g06_foot": "Escala de color recortada al percentil 98.",
        "g07_cb": "mejor correlación (±6 meses)",
        "g07_lag_some": "Se anota el retardo cuando no es cero.",
        "g07_lag_zero": "En TODAS las parejas el mejor retardo es 0: el co-movimiento es contemporáneo "
        "— nadie anticipa a nadie.",
        "g07_head": "Solo {n} pareja{s} de áreas se mueven de la mano",
        "g07_sub": "Mejor correlación cruzada de los avances (familiar FAD, retardos de ±6 meses).\n"
        "{lag_note} La heterogeneidad justifica modelar cada serie.",
        "g08_panel": "panel completo\n(con empleo): {n}%",
        "g08_xlabel": "Meses sin movimiento (% de los meses con fecha)",
        "g08_head": "La serie familiar típica pasa {n}% de los meses congelada",
        "g08_sub": "Porcentaje de meses en que la fecha no se movió (series familiares FAD). "
        "Es la razón de que el pronóstico ingenuo sea tan difícil de vencer.",
        "g09_diff": "diferenciar: {n} series",
        "g09_mixed": "mixtas: {n} (ADF y KPSS discrepan)",
        "g09_level": "estacionarias en nivel: {n}",
        "g09_xlabel": "ADF p-value  (H$_0$: raíz unitaria)",
        "g09_ylabel": "KPSS p-value\n(H$_0$: estacionaria)",
        "g09_head": "{a} de {b} series evaluables exigen diferenciación",
        "g09_sub": "ADF y KPSS coinciden: el panel es integrado de orden 1. Ninguna serie es estacionaria en nivel; "
        "el eje KPSS se recorta a su zona de saturación (p≤0.05).",
        "g09_foot": "Jitter leve: los p-values saturan en los bordes.",
        "g10_regions": {
            "africa": "África",
            "asia": "Asia",
            "europe": "Europa",
            "north_america": "Norteamérica",
            "oceania": "Oceanía",
            "south_america_caribbean": "Sudamérica y Caribe",
        },
        "g10_ylabel": "Rango de corte publicado",
        "g10_head": "La lotería también hace fila: {region} corta en {n} mil",
        "g10_sub": "Rango de corte del sorteo de diversidad por región ({n} observaciones). Es un NÚMERO de "
        "sorteo, no una fecha: hecho descriptivo separado, fuera del objetivo predictivo.",
        "g10_foot": "El diente de sierra es el ciclo del año fiscal: el corte sube y se reinicia.",
        "g11_xlabel": "Fracción de los meses de la serie",
        "g11_F": "F (fecha publicada — entrenable)",
        "g11_C": "C (Current)",
        "g11_U": "U (Unavailable)",
        "g11_nodata": "sin dato",
        "g11_cont_x": "Continuidad del tramo F",
        "g11_cont_y": "Series",
        "g11_title": "{nf} series con fechas;\n{ne} plenamente evaluables",
        "g11_head": "El {p}% del panel es entrenable — y está censado serie por serie",
        "g11_sub": "Composición de régimen de las {n} series estructurales (izquierda, ordenadas "
        "por % de fechas dentro de cada bloque) y continuidad del tramo con fechas (derecha).",
        "g11_foot": "Cobertura escalonada: estructural → con fechas → evaluable.",
    },
    "en": {
        "footer": "Source: U.S. Department of State, Visa Bulletin (Dec 2001 – {mes} {anio}).",
        "blk_family": "Family",
        "blk_employment": "Employment",
        "g01_head": "{n} months in line, in a single image",
        "g01_sub": "Each row is one country × category × table series ({n} series); each cell, "
        "a monthly bulletin. In {pct}% of months with a date the line did not move.",
        "leg_adv": "date advances",
        "leg_frozen": "frozen",
        "leg_retro": "retrogresses",
        "leg_current": "Current (no backlog)",
        "leg_nodata": "U / no data",
        "g01_foot": "White = the series does not exist that month (e.g., DFF before 2015).",
        "g02_ylabel": "Priority date being served (year)",
        "g02_quake": "{mes} {anio}: {n} series\nretrogress {yrs} accumulated years",
        "g02_pand": "pandemic:\n{pct} frozen",
        "g02_head": "A quarter century in line, series by series",
        "g02_sub": "Trajectories of the 25 family series (FAD). The median advance is {med} days per month: "
        "the line almost never runs, and sometimes moves backwards.",
        "g03_current": "all areas\nin Current",
        "g03_head": "{cf} {catf} waits {yf} years; {ce} {cate}, {ye}",
        "g03_sub": "Current backlog in years (FAD) by category and chargeability area; Mexico highlighted. "
        "Areas in Current (no backlog) do not appear.",
        "g03_foot": "Backlog = bulletin month − current priority date.",
        "g04_note": "{name} ({table})\n{mes} {anio}: −{yrs} years",
        "g04_ylabel": "Month's setback (years)",
        "g04_head": "{n} times the line moved backwards — and a few were earthquakes",
        "g04_sub": "Each dot is a month of retrogression in one of the series ({pct}% of observed movements); "
        "size is proportional to the setback.",
        "g05_gap": "the widest gap: {yrs} years",
        "g05_fad": "FAD (final action)",
        "g05_dff": "DFF (filing)",
        "g05_xlabel": "Current backlog (years)",
        "g05_head": "Filing {n} months earlier: that is what the DFF table is worth",
        "g05_sub": "Current backlog per series depending on the table you look at: final action (FAD) always "
        "trails the filing calendar (DFF).",
        "g05_foot": "Series with both tables published today.",
        "g06_cb": "median advance (days)",
        "g06_era": "the frozen era: FY{a}–FY{b}",
        "g06_months": ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"],
        "g06_ylabel": "median\n(days)",
        "g06_kick": "fiscal-year kickoff",
        "g06_head": "The fiscal year barely has a pulse: the typical month advances between {lo} and {hi} days",
        "g06_sub": "Median advance of the full panel by month (columns in fiscal-year order, October first) "
        "and by fiscal year (rows). There is no exploitable seasonality — a finding, not a shortcoming.",
        "g06_foot": "Color scale clipped at the 98th percentile.",
        "g07_cb": "best correlation (±6 months)",
        "g07_lag_some": "The lag is annotated when it is not zero.",
        "g07_lag_zero": "In ALL pairs the best lag is 0: co-movement is contemporaneous — no one leads anyone.",
        "g07_head": "Only {n} pair{s} of areas move hand in hand",
        "g07_sub": "Best cross-correlation of the advances (family FAD, lags of ±6 months).\n"
        "{lag_note} The heterogeneity justifies modeling each series.",
        "g08_panel": "full panel\n(incl. employment): {n}%",
        "g08_xlabel": "Months without movement (% of months with a date)",
        "g08_head": "The typical family series spends {n}% of its months frozen",
        "g08_sub": "Percentage of months in which the date did not move (family FAD series). "
        "It is the reason the naïve forecast is so hard to beat.",
        "g09_diff": "differencing: {n} series",
        "g09_mixed": "mixed: {n} (ADF and KPSS disagree)",
        "g09_level": "stationary in level: {n}",
        "g09_xlabel": "ADF p-value  (H$_0$: unit root)",
        "g09_ylabel": "KPSS p-value\n(H$_0$: stationary)",
        "g09_head": "{a} of {b} evaluable series demand differencing",
        "g09_sub": "ADF and KPSS agree: the panel is integrated of order 1. No series is stationary in level; "
        "the KPSS axis is clipped to its saturation zone (p≤0.05).",
        "g09_foot": "Slight jitter: the p-values saturate at the edges.",
        "g10_regions": {
            "africa": "Africa",
            "asia": "Asia",
            "europe": "Europe",
            "north_america": "North America",
            "oceania": "Oceania",
            "south_america_caribbean": "South America & Caribbean",
        },
        "g10_ylabel": "Published cutoff rank",
        "g10_head": "The lottery waits in line too: {region} cuts at {n}k",
        "g10_sub": "Diversity-lottery cutoff rank by region ({n} observations). It is a lottery NUMBER, not a "
        "date: a separate descriptive fact, outside the predictive target.",
        "g10_foot": "The sawtooth is the fiscal-year cycle: the cutoff climbs and resets.",
        "g11_xlabel": "Fraction of the series' months",
        "g11_F": "F (published date — trainable)",
        "g11_C": "C (Current)",
        "g11_U": "U (Unavailable)",
        "g11_nodata": "no data",
        "g11_cont_x": "Continuity of the F span",
        "g11_cont_y": "Series",
        "g11_title": "{nf} series with dates;\n{ne} fully evaluable",
        "g11_head": "{p}% of the panel is trainable — and censused series by series",
        "g11_sub": "Regime composition of the {n} structural series (left, sorted by % of dates within "
        "each block) and continuity of the dated span (right).",
        "g11_foot": "Tiered coverage: structural → with dates → evaluable.",
    },
}
T: dict = TXT["es"]
CNAME: dict[str, str] = COUNTRY_NAME
MESL: dict[int, str] = MES


def _apply_lang(lang: str) -> None:
    """Re-vincula los textos visibles (T), nombres de país (CNAME) y meses (MESL)."""
    global LANG, T, CNAME, MESL
    LANG = lang
    T = TXT[lang]
    CNAME = COUNTRY_NAME if lang == "es" else COUNTRY_NAME_EN
    MESL = MES if lang == "es" else MES_EN


def _num(v: int) -> str:
    """Separador de miles con coma (27,611) — convención es-MX del proyecto, igual
    en ambos idiomas; el espacio fino previo desalineaba figura vs caption/.tex."""
    return f"{v:,}"


def _spread(vals: list[float], min_gap: float) -> list[float]:
    """Des-colisiona posiciones de etiquetas (preserva el orden, separa >= min_gap)."""
    idx = np.argsort(vals)
    out = np.asarray(vals, dtype=float).copy()
    for a, b in zip(idx[:-1], idx[1:], strict=True):
        out[b] = max(out[b], out[a] + min_gap)
    return out.tolist()


def _load() -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(
        ROOT / "data" / "processed" / "visa_panel_long.csv", parse_dates=["bulletin_date", "priority_date"]
    )
    facts = json.loads((ROOT / "reports" / "eda" / "eda_facts.json").read_text())
    return df, facts


# Figuras cuya version PDF consume el .tex (las 5 restantes solo existen como PNG web/reporte)
TEX_PDFS = {"g01_panel", "g04_retros", "g05_brecha", "g07_leadlag", "g10_dv", "g11_completitud"}


def _save(fig: plt.Figure, name: str) -> plt.Figure:
    """Guarda el par PDF-vector + PNG-300dpi y DEVUELVE la figura viva.

    El caller cierra (``plt.close``); ``build_eda_report`` la re-usa tal cual como
    página vectorial del reporte (cero re-rasterización = cero pérdida de nitidez).
    """
    if LANG == "en" or DARK_MODE:
        # variantes solo-web (EN y/o oscura): solo PNG; el .tex y el reporte PDF
        # viven en ES-claro (entregable académico en español)
        sub = FIG_PNG
        if LANG == "en":
            sub = sub / "en"
        if DARK_MODE:
            sub = sub / "dark"
        sub.mkdir(parents=True, exist_ok=True)
        fig.savefig(sub / f"{name}.png", bbox_inches="tight", dpi=300, facecolor=PAPER)
        print(f"{sub.relative_to(FIG_PNG)}/{name} OK")
        return fig
    FIG_PNG.mkdir(parents=True, exist_ok=True)
    # PENDIENTES #13: el .tex solo referencia 6 de las 11 figuras; emitir PDF únicamente
    # para esas (las demás vivían huérfanas en Figures/ y el sync de Overleaf las arrastraba).
    if name in TEX_PDFS:
        fig.savefig(FIG_TEX / f"eda3_{name}.pdf", bbox_inches="tight")
    fig.savefig(FIG_PNG / f"{name}.png", bbox_inches="tight", dpi=300)
    print(f"eda3_{name} OK")
    return fig


def _header(fig: plt.Figure, headline: str, sub: str, y: float = 1.02, dy: float = 0.055) -> None:
    """Titular-frase (el hallazgo) + bajada explicativa, estilo editorial.

    Envuelve titular y bajada al ancho de la figura (sin corte de carro, una bajada
    larga estiraba el bbox y encogia el plot). Las lineas extra crecen hacia ARRIBA
    (va="bottom" + lift del titular) para no invadir los ejes.
    """
    w_in, h_in = fig.get_size_inches()
    head = textwrap.fill(headline, width=max(28, int(w_in * 7.2)))
    body = textwrap.fill(sub, width=max(50, int(w_in * 12.5)))
    lift = body.count(chr(10)) * (0.175 / h_in)
    fig.text(0.01, y + dy + lift, head, fontsize=14, fontweight="bold", color=INK, ha="left", va="bottom")
    fig.text(0.01, y, body, fontsize=9.5, color=GRAY, ha="left", va="bottom")


def _footer(fig: plt.Figure, vintage: str, extra: str = "", y: float = -0.045) -> None:
    per = pd.Period(vintage)
    text = T["footer"].format(mes=MESL[per.month], anio=per.year) + (f"  {extra}" if extra else "")
    fig.text(0.01, y, text, fontsize=7.4, color=GRAY, ha="left")
    # con pie largo la marca baja un renglón para no encimarse con el texto
    brand_y = y - 0.035 if len(text) > 80 else y
    fig.text(0.99, brand_y, "VisaPredict AI", fontsize=8.5, color=BLUE, ha="right", fontweight="bold")


# ---------------------------------------------------------------------------- G1
def g01_panel(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """El panel completo en una sola imagen: 194 series × 296 meses."""
    months = pd.period_range(df.bulletin_date.min(), df.bulletin_date.max(), freq="M")
    m_idx = {p: i for i, p in enumerate(months)}
    # clases: 0 ausente · 1 F avanza · 2 F congelada · 3 F retrocede · 4 Current · 5 U/UNK
    colors = [PAPER, BLUE, MUTE, WINE, TEAL, UNK_FILL]
    df = df.sort_values("bulletin_date").copy()
    df["delta"] = df.groupby(["country", "block", "category", "table"])["days_since_base"].diff()

    order: list[tuple[str, str, str, str]] = []
    for block in ("family", "employment"):
        for table in ("FAD", "DFF"):
            for c in PILOT:
                sub = df[(df.block == block) & (df.table == table) & (df.country == c)]
                order.extend((c, block, cat, table) for cat in sorted(sub.category.unique()))
    mat = np.zeros((len(order), len(months)), dtype=np.int_)
    row_of = {k: i for i, k in enumerate(order)}
    per = df.bulletin_date.dt.to_period("M").map(m_idx)
    cls = np.select(
        [
            (df.status == "F") & (df.delta < 0),
            (df.status == "F") & (df.delta == 0),
            (df.status == "F"),
            (df.status == "C"),
        ],
        [3, 2, 1, 4],
        default=5,
    )
    rows = df.set_index(["country", "block", "category", "table"]).index.map(row_of)
    mat[rows, per] = cls

    fig, ax = plt.subplots(figsize=(8.6, 11.0))
    ax.imshow(mat, aspect="auto", cmap=ListedColormap(colors), vmin=0, vmax=5, interpolation="nearest")
    # separadores + etiquetas de grupo país (bloque×tabla a la derecha)
    bounds, labels_y = [], []
    prev = None
    for i, (c, block, _cat, table) in enumerate(order):
        key = (c, block, table)
        if key != prev:
            if prev is not None:
                ax.axhline(i - 0.5, color=PAPER, lw=1.4)
            bounds.append(i)
            prev = key
    bounds.append(len(order))
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        c, block, _cat, table = order[a]
        ax.text(-4, (a + b - 1) / 2, CNAME[c], ha="right", va="center", fontsize=6.4, color=INK)
        labels_y.append(((a + b - 1) / 2, block, table))
    # bandas bloque×tabla a la derecha
    seen = set()
    for i, (_c, block, _cat, table) in enumerate(order):
        if (block, table) not in seen:
            seen.add((block, table))
            n_rows = len([o for o in order if (o[1], o[3]) == (block, table)])
            name = T[f"blk_{block}"]
            ax.text(
                len(months) + 3,
                i + n_rows / 2 - 0.5,
                f"{name}\n{table}",
                ha="left",
                va="center",
                fontsize=7.5,
                color=BLUE,
                fontweight="bold",
            )
            if i:
                ax.axhline(i - 0.5, color=INK, lw=0.9)
    years = pd.period_range(months[0], months[-1], freq="Y")
    xt = [m_idx[pd.Period(f"{y.year}-01", freq="M")] for y in years if pd.Period(f"{y.year}-01", freq="M") in m_idx]
    ax.set_xticks(
        xt[::2], [str(y.year) for y in years if pd.Period(f"{y.year}-01", freq="M") in m_idx][::2], fontsize=7
    )
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0)

    fig.subplots_adjust(top=0.965, bottom=0.075, left=0.09, right=0.93)
    p = facts["panel"]
    _header(
        fig,
        T["g01_head"].format(n=_num(p["n_obs"])),
        T["g01_sub"].format(n=p["n_series_structural"], pct=f"{p['pct_frozen']:.0f}"),
        y=0.975,
        dy=0.022,
    )
    fig.legend(
        handles=[
            Patch(fc=BLUE, label=T["leg_adv"]),
            Patch(fc=MUTE, label=T["leg_frozen"]),
            Patch(fc=WINE, label=T["leg_retro"]),
            Patch(fc=TEAL, label=T["leg_current"]),
            Patch(fc=UNK_FILL, ec=MID, lw=0.4, label=T["leg_nodata"]),
        ],
        loc="lower center",
        ncol=5,
        fontsize=7.6,
        frameon=False,
        bbox_to_anchor=(0.5, 0.038),
    )
    _footer(fig, facts["vintage"], T["g01_foot"], y=0.022)
    return _save(fig, "g01_panel")


# ---------------------------------------------------------------------------- G2
def g02_trayectorias(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """Un cuarto de siglo de fila: las 25 series familiares FAD, anotadas."""
    f = df[(df.status == "F") & (df.block == "family") & (df.table == "FAD")].copy()
    f["years"] = days_to_year(f.days_since_base)  # AD3
    fig, ax = plt.subplots(figsize=(8.6, 4.9))
    for (c, _cat), s in f.groupby(["country", "category"]):
        s = s.sort_values("bulletin_date")
        ax.plot(s.bulletin_date, s.years, color=COUNTRY[c], lw=0.9, alpha=0.8)
    # etiqueta directa por país al borde derecho (nada de leyenda), sin encimarse
    last = f[f.bulletin_date == f.bulletin_date.max()]
    ends = {c: g.years.mean() for c, g in last.groupby("country")}
    ys = _spread([ends[c] for c in PILOT], min_gap=1.6)
    for c, ylab in zip(PILOT, ys, strict=True):
        ax.annotate(
            CNAME[c],
            (f.bulletin_date.max(), ylab),
            xytext=(8, 0),
            textcoords="offset points",
            color=COUNTRY[c],
            fontsize=8.5,
            fontweight="bold",
            va="center",
        )
    # anotación derivada: el mes con la retrogresión agregada más brutal del bloque
    ev = pd.DataFrame(facts["retro_events"])
    ev = ev[(ev.block == "family") & (ev.table == "FAD")]
    worst = ev.groupby("date").days.sum().idxmax()
    n_hit = int((ev.date == worst).sum())
    yrs_lost = ev[ev.date == worst].days.sum() / DAYS_PER_YEAR
    wd = pd.Timestamp(worst + "-01")
    ax.axvline(wd, color=WINE, lw=0.9, ls="--", alpha=0.8)
    ax.annotate(
        T["g02_quake"].format(mes=MESL[wd.month], anio=wd.year, n=n_hit, yrs=f"{yrs_lost:.0f}"),
        (wd, f.years.min() + 0.5),
        xytext=(14, 0),
        textcoords="offset points",
        ha="left",
        va="bottom",
        fontsize=7.6,
        color=WINE,
        arrowprops={"arrowstyle": "-", "color": WINE, "lw": 0.7},
    )
    # banda pandémica, solo si los datos la sostienen (share de congelamiento)
    f = f.sort_values("bulletin_date")
    f["delta"] = f.groupby(["country", "category"])["days_since_base"].diff()
    win = f[(f.bulletin_date >= "2020-04-01") & (f.bulletin_date <= "2021-09-01")]
    frozen_win = float((win.delta == 0).mean())
    if frozen_win > float((f.delta == 0).mean()):
        ax.axvspan(pd.Timestamp("2020-04-01"), pd.Timestamp("2021-09-01"), color=GRID, alpha=0.6, zorder=0)
        ax.annotate(
            T["g02_pand"].format(pct=f"{frozen_win:.0%}"),
            (pd.Timestamp("2020-11-01"), f.years.max() - 2.0),
            ha="center",
            fontsize=7.4,
            color=GRAY,
        )
    ax.set_ylabel(T["g02_ylabel"])
    ax.set_xlim(f.bulletin_date.min(), f.bulletin_date.max() + pd.DateOffset(months=54))
    ax.grid(True, axis="y", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    med = f.delta.median()
    _header(fig, T["g02_head"], T["g02_sub"].format(med=f"{med:.0f}"))
    _footer(fig, facts["vintage"])
    return _save(fig, "g02_trayectorias")


# ---------------------------------------------------------------------------- G3
def g03_backlog(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """¿Cuántos años de fila? — matriz Latinometrics familia + empleo."""
    bt = pd.DataFrame(facts["backlog_today"])
    bt = bt[bt.table == "FAD"]
    fam = bt[bt.block == "family"]
    emp = bt[bt.block == "employment"]
    top_f = fam.loc[fam.backlog_years.idxmax()]
    top_e = emp.loc[emp.backlog_years.idxmax()]
    vmax = float(bt.backlog_years.max())

    fig, axes = plt.subplots(2, 5, figsize=(9.4, 5.6), sharex=True)
    for row, (cats, blk) in enumerate(((FAM, "family"), (EB, "employment"))):
        for j, cat in enumerate(cats):
            ax = axes[row][j]
            g = bt[(bt.block == blk) & (bt.category == cat)].sort_values("backlog_years")
            if g.empty:
                ax.text(
                    0.5,
                    0.5,
                    T["g03_current"],
                    transform=ax.transAxes,
                    ha="center",
                    va="center",
                    fontsize=8,
                    color=MID,
                    style="italic",
                )
            ax.barh(
                range(len(g)),
                g.backlog_years,
                color=[BLUE if c == "mexico" else MUTE for c in g.country],
                height=0.62,
                zorder=2,
            )
            for i, r in enumerate(g.itertuples()):
                ab = AnnotationBbox(
                    OffsetImage(_flag(r.country), zoom=0.085),
                    (-vmax * 0.10, i),
                    frameon=False,
                    box_alignment=(0.5, 0.5),
                    pad=0,
                    annotation_clip=False,
                )
                ax.add_artist(ab)
                ax.text(
                    r.backlog_years + vmax * 0.03,
                    i,
                    f"{r.backlog_years:.0f}",
                    va="center",
                    fontsize=7.4,
                    fontweight="bold" if r.country == "mexico" else "normal",
                    color=BLUE if r.country == "mexico" else MID,
                )
            ax.set_title(cat.replace("EB", "EB-"), fontsize=9, color=BLUE, pad=3)
            ax.set_xlim(-vmax * 0.18, vmax * 1.22)
            ax.set_ylim(-0.7, 4.7)
            ax.set_xticks([])
            ax.set_yticks([])
            for sp in ax.spines.values():
                sp.set_visible(False)
    _header(
        fig,
        T["g03_head"].format(
            cf=CNAME[top_f.country],
            catf=top_f.category,
            yf=f"{top_f.backlog_years:.0f}",
            ce=CNAME[top_e.country],
            cate=top_e.category.replace("EB", "EB-"),
            ye=f"{top_e.backlog_years:.0f}",
        ),
        T["g03_sub"],
        y=0.965,
        dy=0.045,
    )
    _footer(fig, facts["vintage"], T["g03_foot"], y=0.015)
    return _save(fig, "g03_backlog")


# ---------------------------------------------------------------------------- G4
def g04_retros(facts: dict) -> plt.Figure:
    """Los meses en que el sistema se rompió: TODAS las retrogresiones."""
    ev = pd.DataFrame(facts["retro_events"])
    ev["date_ts"] = pd.to_datetime(ev.date + "-01")
    ev["years_lost"] = ev.days / DAYS_PER_YEAR
    fig, ax = plt.subplots(figsize=(8.6, 4.6))
    ax.scatter(
        ev.date_ts,
        ev.years_lost,
        s=8 + 55 * ev.years_lost / ev.years_lost.max(),
        c=[COUNTRY[c] for c in ev.country],
        alpha=0.65,
        edgecolor=PAPER,
        lw=0.4,
        zorder=3,
    )
    top = ev.nlargest(5, "days").reset_index(drop=True)
    for i, r in top.iterrows():
        d = pd.Timestamp(r.date_ts)
        # alterna la anotación arriba/abajo-derecha para no encimar los eventos cercanos
        dx, dy, ha = (10, -4, "left") if i % 2 == 0 else (-10, -16, "right")
        ax.annotate(
            T["g04_note"].format(
                name=f"{CNAME[r.country]} {r.category.replace('EB', 'EB-')}",
                table=r.table,
                mes=MESL[d.month],
                anio=d.year,
                yrs=f"{r.years_lost:.1f}",
            ),
            (r.date_ts, r.years_lost),
            xytext=(dx, dy),
            textcoords="offset points",
            fontsize=7.2,
            color=INK,
            ha=ha,
            arrowprops={"arrowstyle": "-", "color": MID, "lw": 0.7},
        )
    ax.set_ylabel(T["g04_ylabel"])
    ax.grid(True, axis="y", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    handles = [plt.Line2D([0], [0], marker="o", ls="", mfc=COUNTRY[c], mec=PAPER, ms=6, label=CNAME[c]) for c in PILOT]
    fig.legend(handles=handles, fontsize=7.6, frameon=False, loc="lower center", ncol=5, bbox_to_anchor=(0.5, -0.005))
    n = len(ev)
    pct = facts["panel"]["pct_retro"]
    _header(fig, T["g04_head"].format(n=n), T["g04_sub"].format(pct=f"{pct:.1f}"))
    _footer(fig, facts["vintage"], y=-0.085)
    return _save(fig, "g04_retros")


# ---------------------------------------------------------------------------- G5
def g05_brecha(facts: dict) -> plt.Figure:
    """La brecha entre las dos tablas: dumbbell FAD ↔ DFF hoy."""
    bt = pd.DataFrame(facts["backlog_today"])
    wide = bt.pivot_table(index=["country", "block", "category"], columns="table", values="backlog_years")
    wide = wide.dropna().reset_index()
    wide["gap"] = wide.FAD - wide.DFF
    # la mediana del titular se calcula sobre TODAS las parejas vigentes, ANTES de
    # truncar a las 22 mostradas (si no, el titular hereda el sesgo del top del chart
    # y se desalinea del caption web, que deriva del censo completo — regla #0)
    med_gap = float(wide.gap.median()) * 12
    wide = wide.sort_values("FAD").reset_index(drop=True)
    if len(wide) > 22:
        wide = wide.tail(22).reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(8.0, 6.2))
    for i, r in wide.iterrows():
        ax.plot([r.DFF, r.FAD], [i, i], color=MUTE, lw=2.2, zorder=1, solid_capstyle="round")
        ax.scatter(r.FAD, i, color=BLUE, s=42, zorder=3)
        ax.scatter(r.DFF, i, color=GOLD, s=42, zorder=3, edgecolor=INK, lw=0.4)
        name = f"{CNAME[r.country]} {r.category.replace('EB', 'EB-')}"
        ax.text(-0.4, i, name, ha="right", va="center", fontsize=7.6, color=INK)
    big = wide.loc[wide.gap.idxmax()]
    ax.annotate(
        T["g05_gap"].format(yrs=f"{big.gap:.1f}"),
        (float(big.FAD), int(wide.gap.idxmax())),
        xytext=(26, 0),
        textcoords="offset points",
        ha="left",
        va="center",
        fontsize=7.8,
        color=GRAY,
        arrowprops={"arrowstyle": "-", "color": MID, "lw": 0.7},
    )
    ax.scatter([], [], color=BLUE, s=42, label=T["g05_fad"])
    ax.scatter([], [], color=GOLD, s=42, edgecolor=INK, lw=0.4, label=T["g05_dff"])
    ax.legend(fontsize=8, frameon=False, loc="lower right")
    ax.set_xlabel(T["g05_xlabel"])
    ax.set_yticks([])
    ax.set_xlim(left=-6.5)
    ax.grid(True, axis="x", color=GRID, lw=0.6)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    _header(fig, T["g05_head"].format(n=f"{med_gap:.0f}"), T["g05_sub"], y=0.985, dy=0.04)
    _footer(fig, facts["vintage"], T["g05_foot"], y=0.02)
    return _save(fig, "g05_brecha")


# ---------------------------------------------------------------------------- G6
def g06_pulso_fiscal(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """El pulso del año fiscal: avance mediano mes × año fiscal."""
    f = df[df.status == "F"].sort_values("bulletin_date").copy()
    f["delta"] = f.groupby(["country", "block", "category", "table"])["days_since_base"].diff()
    f = f.dropna(subset=["delta"])
    f["fy"] = f.bulletin_date.dt.year + (f.bulletin_date.dt.month >= 10).astype(int)
    fiscal_order = [10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    piv = f.pivot_table(index="fy", columns=f.bulletin_date.dt.month, values="delta", aggfunc="median")
    piv = piv.reindex(columns=fiscal_order)
    # escala al p98: sin el recorte, un único FY extremo lava el resto de la textura
    lim = float(np.nanpercentile(np.abs(piv.to_numpy()), 98))
    fig, (ax, axm) = plt.subplots(
        2, 1, figsize=(7.2, 6.4), height_ratios=(5, 1.1), sharex=True, gridspec_kw={"hspace": 0.08}
    )
    im = ax.imshow(piv.to_numpy(), cmap=DIV, vmin=-lim, vmax=lim, aspect="auto", interpolation="nearest")
    ax.set_yticks(range(len(piv.index)), [f"FY{y}" for y in piv.index], fontsize=6.4)
    ax.set_xticks([])
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.01, extend="both")
    cb.set_label(T["g06_cb"], fontsize=7.5)
    # anotación derivada: la era congelada = la racha CONSECUTIVA más larga de años
    # fiscales con avance mediano <= 0 (texto sobre sus filas, que quedan en blanco)
    fy_med = piv.median(axis=1)
    frozen = fy_med.index[fy_med <= 0].tolist()
    runs: list[list[int]] = []
    for fy in frozen:
        if runs and fy - 1 == runs[-1][-1]:
            runs[-1].append(fy)
        else:
            runs.append([fy])
    if runs:
        run = max(runs, key=len)
        y0, y1 = piv.index.get_loc(run[0]), piv.index.get_loc(run[-1])
        ax.text(
            5.5,
            (y0 + y1) / 2,
            T["g06_era"].format(a=run[0], b=run[-1]),
            fontsize=7.8,
            color=GRAY,
            ha="center",
            va="center",
            style="italic",
        )
    med = pd.Series(facts["monthly_advance_median"])
    med.index = med.index.astype(int)
    med = med.reindex(fiscal_order)
    axm.bar(range(12), med.to_numpy(), color=[GOLD if m == 10 else BLUE for m in fiscal_order], alpha=0.9)
    axm.set_xticks(range(12), T["g06_months"], fontsize=7.5)
    axm.set_ylabel(T["g06_ylabel"], fontsize=7)
    axm.grid(True, axis="y", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        axm.spines[sp].set_visible(False)
    axm.annotate(
        T["g06_kick"],
        (0.35, float(med.iloc[0]) * 0.92),
        xytext=(10, 0),
        textcoords="offset points",
        fontsize=7.2,
        color=GRAY,
        va="center",
        arrowprops={"arrowstyle": "-", "color": MID, "lw": 0.7},
    )
    lo, hi = float(med.min()), float(med.max())
    _header(fig, T["g06_head"].format(lo=f"{lo:.0f}", hi=f"{hi:.0f}"), T["g06_sub"], y=0.945, dy=0.035)
    _footer(fig, facts["vintage"], T["g06_foot"], y=0.03)
    return _save(fig, "g06_pulso_fiscal")


# ---------------------------------------------------------------------------- G7
def g07_leadlag(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """¿Quién se mueve primero? Correlación cruzada con retardos entre áreas."""
    f = df[(df.status == "F") & (df.block == "family") & (df.table == "FAD")].sort_values("bulletin_date").copy()
    f["delta"] = f.groupby(["country", "category"])["days_since_base"].diff()
    sig = {c: f[f.country == c].groupby("bulletin_date").delta.mean().asfreq("MS") for c in PILOT}
    lags = range(-6, 7)
    n = len(PILOT)
    best_r = np.zeros((n, n))
    best_l = np.zeros((n, n), dtype=int)
    for i, a in enumerate(PILOT):
        for j, b in enumerate(PILOT):
            rs = [(sig[a].corr(sig[b].shift(k)), k) for k in lags]
            r, k = max(rs, key=lambda t: t[0] if not np.isnan(t[0]) else -9)
            best_r[i, j], best_l[i, j] = r, k
    fig, ax = plt.subplots(figsize=(5.6, 5.0))
    im = ax.imshow(best_r, cmap=SEQ, vmin=0, vmax=1)
    names = [CNAME[c] for c in PILOT]
    ax.set_xticks(range(n), names, rotation=30, ha="right", fontsize=8)
    ax.set_yticks(range(n), names, fontsize=8)
    any_lag = False
    for i in range(n):
        for j in range(n):
            dark = best_r[i, j] > 0.55
            lag_txt = f"\n{best_l[i, j]:+d} m" if (i != j and best_l[i, j]) else ""
            any_lag |= bool(lag_txt)
            ax.text(
                j,
                i,
                f"{best_r[i, j]:.2f}{lag_txt}",
                ha="center",
                va="center",
                fontsize=8,
                color=PAPER if dark else INK,
            )
    cb = fig.colorbar(im, ax=ax, fraction=0.045)
    cb.set_label(T["g07_cb"], fontsize=7.5)
    mask = ~np.eye(n, dtype=bool)
    strong = int((best_r[mask] > 0.5).sum() // 2)
    lag_note = T["g07_lag_some"] if any_lag else T["g07_lag_zero"]
    _header(
        fig,
        T["g07_head"].format(n=strong, s="s" if strong != 1 else ""),
        T["g07_sub"].format(lag_note=lag_note),
        y=0.99,
        dy=0.075,
    )
    _footer(fig, facts["vintage"], y=-0.075)
    return _save(fig, "g07_leadlag")


# ---------------------------------------------------------------------------- G8
def g08_congelados(facts: dict) -> plt.Figure:
    """Los meses congelados: % de meses sin movimiento por serie."""
    census = pd.DataFrame(facts["series"])
    g = census[(census.block == "family") & (census.table == "FAD")].copy()
    g["name"] = g.country.map(CNAME) + " " + g.category
    g = g.sort_values("pct_frozen")
    fig, ax = plt.subplots(figsize=(7.6, 6.0))
    ax.barh(range(len(g)), g.pct_frozen * 100, color=[COUNTRY[c] for c in g.country], height=0.68, zorder=2)
    for i, r in enumerate(g.itertuples()):
        ax.text(r.pct_frozen * 100 + 0.8, i, f"{r.pct_frozen:.0%}", va="center", fontsize=7, color=MID)
        ax.text(-1.2, i, r.name, ha="right", va="center", fontsize=7.4, color=INK)
    panel_frozen = facts["panel"]["pct_frozen"]
    ax.axvline(panel_frozen, color=INK, lw=1.0, ls="--")
    ax.annotate(
        T["g08_panel"].format(n=f"{panel_frozen:.0f}"),
        (panel_frozen, 1.0),
        xytext=(8, 0),
        textcoords="offset points",
        fontsize=7.8,
        color=INK,
        va="center",
    )
    ax.set_xlabel(T["g08_xlabel"])
    ax.set_yticks([])
    ax.set_xlim(-16, 62)
    ax.grid(True, axis="x", color=GRID, lw=0.6)
    for sp in ("top", "right", "left"):
        sp_obj = ax.spines[sp]
        sp_obj.set_visible(False)
    med = float(g.pct_frozen.median()) * 100
    _header(fig, T["g08_head"].format(n=f"{med:.0f}"), T["g08_sub"], y=0.975, dy=0.04)
    _footer(fig, facts["vintage"], y=0.02)
    return _save(fig, "g08_congelados")


# ---------------------------------------------------------------------------- G9
def g09_estacionariedad(facts: dict) -> plt.Figure:
    """Censo de estacionariedad: ADF vs KPSS, 74 series juzgadas de un vistazo."""
    census = pd.DataFrame(facts["series"])
    ev = census[census.verdict.notna()].copy()
    rng = np.random.default_rng(7)  # jitter determinista (los p-values saturan en 0.01/0.99)
    x = ev.adf_p + rng.uniform(-0.012, 0.012, len(ev))
    y = ev.kpss_p + rng.uniform(-0.0015, 0.0015, len(ev))
    ymax = 0.055
    fig, ax = plt.subplots(figsize=(7.0, 4.6))
    # cuadrante "diferenciar": ADF no rechaza raíz unitaria Y KPSS rechaza estacionariedad
    ax.axhspan(0, 0.05, xmin=(0.05 + 0.03) / 1.06, color=QUAD_BLUE, alpha=0.6, zorder=0)
    ax.scatter(
        x,
        y,
        c=[COUNTRY[c] for c in ev.country],
        s=44,
        edgecolor=PAPER,
        lw=0.6,
        alpha=0.9,
        zorder=3,
    )
    counts = ev.verdict.value_counts()
    ax.text(
        0.55,
        0.035,
        T["g09_diff"].format(n=counts.get("difference", 0)),
        fontsize=10.5,
        fontweight="bold",
        color=BLUE,
        va="bottom",
        ha="center",
    )
    n_mixed = int(counts.get("mixed", 0))
    if n_mixed:
        mixed = ev[ev.verdict == "mixed"]
        ax.annotate(
            T["g09_mixed"].format(n=n_mixed),
            (float(mixed.adf_p.max()), float(mixed.kpss_p.mean())),
            xytext=(24, 16),
            textcoords="offset points",
            fontsize=7.8,
            color=GRAY,
            arrowprops={"arrowstyle": "-", "color": MID, "lw": 0.7},
        )
    ax.text(
        0.02,
        0.0515,
        T["g09_level"].format(n=counts.get("stationary", 0)),
        fontsize=7.8,
        color=TEAL,
        va="top",
    )
    ax.axvline(0.05, color=MID, lw=0.8, ls=":")
    ax.axhline(0.05, color=MID, lw=0.8, ls=":")
    ax.set_xlabel(T["g09_xlabel"])
    ax.set_ylabel(T["g09_ylabel"])
    ax.set_xlim(-0.03, 1.03)
    ax.set_ylim(-0.003, ymax)
    handles = [plt.Line2D([0], [0], marker="o", ls="", mfc=COUNTRY[c], mec=PAPER, ms=6, label=CNAME[c]) for c in PILOT]
    ax.legend(handles=handles, fontsize=7.4, frameon=False, loc="upper center", ncol=5, bbox_to_anchor=(0.5, 1.005))
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    n_diff, n_tot = int(counts.get("difference", 0)), len(ev)
    _header(fig, T["g09_head"].format(a=n_diff, b=n_tot), T["g09_sub"], y=0.99, dy=0.06)
    _footer(fig, facts["vintage"], T["g09_foot"], y=-0.02)
    return _save(fig, "g09_estacionariedad")


# ---------------------------------------------------------------------------- G10
def g10_dv(facts: dict) -> plt.Figure:
    """La lotería también hace fila: rangos de corte DV por región."""
    dv = pd.read_csv(ROOT / "data" / "raw" / "dv_visa_rank_timecourse.csv", parse_dates=["visa_bulletin_date"])
    dv = dv[dv.status == "F"].copy()
    region_name = T["g10_regions"]
    palette = [BLUE, WINE, GOLD, TEAL, SLATE, MID]
    regions = sorted(dv.region.unique())
    fig, ax = plt.subplots(figsize=(8.6, 4.8))
    ends = {}
    for i, reg in enumerate(regions):
        g = dv[dv.region == reg].sort_values("visa_bulletin_date")
        ax.plot(g.visa_bulletin_date, g.rank_cutoff, color=palette[i % 6], lw=1.1, alpha=0.85)
        ends[reg] = float(g.iloc[-1].rank_cutoff)
    # etiquetas directas al borde derecho, des-colisionadas (las 3 regiones chicas van pegadas a 0)
    ys = _spread([ends[r] for r in regions], min_gap=3600)
    for i, (reg, ylab) in enumerate(zip(regions, ys, strict=True)):
        ax.annotate(
            region_name.get(reg, reg.replace("_", " ").title()),
            (dv.visa_bulletin_date.max(), ylab),
            xytext=(8, 0),
            textcoords="offset points",
            color=palette[i % 6],
            fontsize=8,
            fontweight="bold",
            va="center",
        )
    ax.set_ylabel(T["g10_ylabel"])
    ax.yaxis.set_major_formatter(lambda v, _: f"{int(v / 1000)}k" if v else "0")
    ax.set_xlim(dv.visa_bulletin_date.min(), dv.visa_bulletin_date.max() + pd.DateOffset(months=64))
    ax.grid(True, axis="y", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    latest = dv[dv.visa_bulletin_date == dv.visa_bulletin_date.max()]
    top = latest.loc[latest.rank_cutoff.idxmax()]
    _header(
        fig,
        T["g10_head"].format(region=region_name.get(top.region, top.region), n=int(top.rank_cutoff / 1000)),
        T["g10_sub"].format(n=_num(facts["dv"]["n_rows"])),
    )
    _footer(fig, facts["vintage"], T["g10_foot"])
    return _save(fig, "g10_dv")


# ---------------------------------------------------------------------------- G11
def g11_completitud(facts: dict) -> plt.Figure:
    """Radiografía de completitud de las 194 series estructurales."""
    census = pd.DataFrame(facts["series"]).copy()
    census["pct_F"] = census.n_F / census.n_total
    order_block = {"family": 0, "employment": 1}
    census = census.sort_values(["block", "pct_F"], key=lambda s: s.map(order_block) if s.name == "block" else s)
    census = census.reset_index(drop=True)
    fig, (ax, axh) = plt.subplots(1, 2, figsize=(9.2, 6.4), width_ratios=(2.1, 1), gridspec_kw={"wspace": 0.16})
    colors = {"n_F": BLUE, "n_C": TEAL, "n_U": WINE, "n_UNK": NODATA}
    left = np.zeros(len(census))
    for col, colr in colors.items():
        frac = census[col] / census.n_total
        ax.barh(range(len(census)), frac, left=left, color=colr, height=1.0, lw=0)
        left += frac.to_numpy()
    n_fam = int((census.block == "family").sum())
    ax.axhline(n_fam - 0.5, color=INK, lw=1.1)
    ax.text(
        -0.015,
        n_fam / 2,
        T["blk_family"].lower(),
        rotation=90,
        va="center",
        ha="right",
        fontsize=8,
        color=INK,
        transform=ax.get_yaxis_transform(),
    )
    ax.text(
        -0.015,
        (n_fam + len(census)) / 2,
        T["blk_employment"].lower(),
        rotation=90,
        va="center",
        ha="right",
        fontsize=8,
        color=INK,
        transform=ax.get_yaxis_transform(),
    )
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.5, len(census) - 0.5)
    ax.set_yticks([])
    ax.set_xlabel(T["g11_xlabel"])
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.legend(
        handles=[
            Patch(fc=BLUE, label=T["g11_F"]),
            Patch(fc=TEAL, label=T["g11_C"]),
            Patch(fc=WINE, label=T["g11_U"]),
            Patch(fc=NODATA, label=T["g11_nodata"]),
        ],
        fontsize=7.2,
        frameon=False,
        loc="lower left",
        bbox_to_anchor=(-0.06, -0.185),
        ncol=4,
        columnspacing=1.1,
        handletextpad=0.5,
    )
    # panel derecho: continuidad de las series CON observaciones F + umbral evaluable
    ev = census[census.n_F > 0]
    axh.hist(ev.continuity, bins=24, color=BLUE, edgecolor=PAPER, lw=0.5)
    axh.set_xlabel(T["g11_cont_x"])
    axh.set_ylabel(T["g11_cont_y"])
    axh.grid(True, axis="y", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        axh.spines[sp].set_visible(False)
    n_eval = facts["panel"]["n_series_evaluable"]
    axh.set_title(
        T["g11_title"].format(nf=int((census.n_F > 0).sum()), ne=n_eval),
        fontsize=8.5,
        color=BLUE,
    )
    p = facts["panel"]
    _header(
        fig,
        T["g11_head"].format(p=p["pct_trainable_F"]),
        T["g11_sub"].format(n=p["n_series_structural"]),
        y=0.975,
        dy=0.04,
    )
    _footer(fig, facts["vintage"], T["g11_foot"], y=-0.055)
    return _save(fig, "g11_completitud")


def _run_all(df: pd.DataFrame, facts: dict) -> None:
    for fn in (g01_panel, g02_trayectorias, g03_backlog, g06_pulso_fiscal, g07_leadlag):
        plt.close(fn(df, facts))
    for fn2 in (g04_retros, g05_brecha, g08_congelados, g09_estacionariedad, g10_dv, g11_completitud):
        plt.close(fn2(facts))


if __name__ == "__main__":
    df, facts = _load()
    # 4 pasadas idioma × tema; SOLO es-claro escribe los PDF del .tex y el reporte
    for lang in ("es", "en"):
        _apply_lang(lang)
        _apply_theme(dark=False)
        _run_all(df, facts)
        _apply_theme(dark=True)  # web oscura -> gallery/dark/ (es) · gallery/en/dark/ (en)
        _run_all(df, facts)
    _apply_lang("es")
    _apply_theme(dark=False)
    print("Galería EDA (es/en × clara/oscura) en", FIG_TEX, "y", FIG_PNG)
