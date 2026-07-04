"""Galería de figuras de Feature Engineering y limpieza (PI-I §1.2.2, épica AE2).

Siete figuras f01–f07 con la gramática editorial de la galería EDA (titular-frase,
anotación directa, paleta canónica) que documentan las decisiones magistrales de FE:
diferenciación, calendario cíclico, importancia, política de huecos (MNAR), máscara
de régimen F-only (B1), tolerancia del parser y el pipeline de limpieza end-to-end.
Los titulares se DERIVAN de los datos/fe_facts en cada corrida (regla #0).

Contrato (AE1): cada maker RETORNA la Figure viva; ``build_fe_report`` la embebe en
vectorial vía PdfPages sin re-rasterizar — mismo contrato que ``make_gallery_figures``.

Salida cuádruple (idioma × tema): la pasada ES-clara escribe reports/latex/Figures/
fe_*.pdf (vector, .tex; nombres históricos preservados) + reports/fe/gallery/f0*.png
(300 dpi); las otras tres solo PNG — gallery/dark/, gallery/en/ y gallery/en/dark/.
Corre en `ante`:  ante/bin/python experiments/make_fe_figures.py  (o `make fe-figures`)
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from make_latinometrics_figures import MES  # noqa: E402  (sys.path[0] = experiments/)
from matplotlib.patches import FancyArrowPatch, Patch, Rectangle  # noqa: E402

from vp_data.visa_common import classify_status, string_to_datetime  # noqa: E402
from vp_model import dataset, missingness, preprocess  # noqa: E402
from vp_model import palette as _palette  # noqa: E402
from vp_model.config import days_to_year  # noqa: E402
from vp_model.palette import (  # noqa: E402
    BLUE,
    GOLD,
    GRAY,
    GRID,
    INK,
    MID,
    STRIPE,
    TEAL,
    WINE,
    style,
)

ROOT = Path(__file__).resolve().parents[1]
FIG_TEX = ROOT / "reports" / "latex" / "Figures"
FIG_PNG = ROOT / "reports" / "fe" / "gallery"
style()
plt.rcParams.update({"font.size": 9, "axes.grid": False})

# --- Tema claro/oscuro -------------------------------------------------------------
# Igual que la galería EDA: los neutros viven en palette.LIGHT/DARK (fuente única);
# aquí solo se re-vinculan los nombres de color del módulo antes de cada pasada.
_LIGHT = _palette.LIGHT
PAPER = _LIGHT["PAPER"]
UNK_FILL = _LIGHT["UNK_FILL"]
DARK_MODE = False


def _apply_theme(dark: bool) -> None:
    """Re-vincula los colores del módulo y los rcParams al tema pedido."""
    global PAPER, INK, GRAY, MID, GRID, STRIPE, BLUE, TEAL, WINE, GOLD, UNK_FILL, DARK_MODE
    src: dict = _palette.DARK if dark else _LIGHT
    DARK_MODE = dark
    PAPER, INK, GRAY, MID, GRID, STRIPE = (
        src["PAPER"],
        src["INK"],
        src["GRAY"],
        src["MID"],
        src["GRID"],
        src["STRIPE"],
    )
    BLUE, TEAL, WINE, GOLD = src["BLUE"], src["TEAL"], src["WINE"], src["GOLD"]
    UNK_FILL = src["UNK_FILL"]
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


def _regime_fill(code: str) -> str | tuple[float, float, float, float]:
    """Relleno de banda/chip por régimen C/F/U/UNK.

    Claro: los pasteles canónicos de palette.REGIME. Oscuro: se DERIVA del color de
    línea del tema con alfa (los pasteles claros ensucian la superficie charcoal) —
    sin hex nuevos, fuente única de color intacta.
    """
    if DARK_MODE:
        line = {"F": BLUE, "C": TEAL, "U": WINE, "UNK": MID}[code]
        return mcolors.to_rgba(line, 0.30)
    return _palette.REGIME[code]["fill"]


# --- Idioma ------------------------------------------------------------------------
# Cuatro pasadas idioma × tema, como la galería EDA. Todo texto visible vive en TXT;
# el código consume el vínculo global T/MESL que _apply_lang() re-apunta.
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
TXT: dict[str, dict] = {
    "es": {
        "footer": "Fuente: U.S. Department of State, Visa Bulletin (dic 2001 – {mes} {anio}).",
        # f01 — diferenciación
        "f01_head": "Los árboles no extrapolan: se les entrena sobre el avance mensual, no el nivel",
        "f01_sub": "Serie {name}: el nivel arrastra décadas de tendencia (un árbol se satura en el máximo "
        "visto); la primera diferencia es estacionaria y el pronóstico se reintegra de forma causal.",
        "f01_series": "Resto del mundo · F2A · FAD",
        "f01_a": "(a) Serie en niveles: tendencia fuerte (no estacionaria)",
        "f01_b": "(b) Primera diferencia: estacionaria y extrapolable",
        "f01_ylab_a": "Fecha de prioridad (año)",
        "f01_ylab_b": "Avance mensual (días)",
        "f01_clip": "{n} pasos fuera de [{lo}, {hi}] d (extremo: {worst} d), truncados solo en pantalla",
        # f02 — calendario cíclico
        "f02_head": "Diciembre y enero son vecinos: el calendario fiscal se codifica en círculo",
        "f02_sub": "Seno/coseno de la posición del mes en el año fiscal (arranca en octubre, cuando se "
        "reinician las cuotas), generados por el encoder canónico que consumen los modelos.",
        "f02_months": ["Oct", "Nov", "Dic", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep"],
        "f02_a": "(a) Componentes seno/coseno",
        "f02_b": "(b) Meses sobre el círculo unitario",
        "f02_xlab": "Mes del año fiscal",
        "f02_ylab": "Valor de la codificación",
        # f03 — importancia
        "f03_head": "La característica dominante: {top}",
        "f03_sub": "Importancia por ganancia de un LightGBM entrenado sobre el avance mensual de las series "
        "familiares FAD (rezagos del avance + calendario cíclico).",
        "f03_lag": "Avance en $t-{k}$",
        "f03_sin": "Calendario (seno)",
        "f03_cos": "Calendario (coseno)",
        "f03_xlab": "Importancia (ganancia, LightGBM)",
        # f04 — política de huecos (MNAR)
        "f04_head": "Huecos de hasta {cap} meses se interpolan; los {n} tramos largos jamás se inventan",
        "f04_sub": "{name}: {miss} meses sin fecha en {runs} corridas (la más larga, {maxrun} meses). "
        "Las corridas cortas (≤{cap}) se interpolan linealmente para la rejilla de entrenamiento; las "
        "largas quedan vacías y solo el EDA las caracteriza con suavizado de Kalman.",
        "f04_series": "México · EB4_RW (trabajadores religiosos) · FAD",
        "f04_long": "hueco de {n} meses:\nKalman solo para EDA,\njamás para entrenar",
        "f04_obs": "observación F (real)",
        "f04_interp": "interpolación corta (≤{cap} m)",
        "f04_kalman": "imputación Kalman (solo EDA)",
        "f04_gap": "hueco largo (sin inventar)",
        "f04_ylab": "Fecha de prioridad (año)",
        # f05 — máscara de régimen (B1)
        "f05_head": "C y U son anotación, no objetivo: solo se puntúan las {nf} fechas publicadas",
        "f05_sub": "{name}: la línea existe solo en los meses F; los fondos marcan el régimen del boletín "
        "({nc} meses Current, {nu} Unavailable, {nk} sin dato). La franja inferior muestra los meses que la "
        "evaluación puntúa (máscara F-only).",
        "f05_series": "India · EB-2 · FAD",
        "f05_strip": "puntuado (F)",
        "f05_F": "F — fecha publicada ({n})",
        "f05_C": "C — Current ({n})",
        "f05_U": "U — Unavailable ({n})",
        "f05_UNK": "sin dato ({n})",
        "f05_ylab": "Fecha de prioridad (año)",
        # f06 — parser tolerante
        "f06_head": "{yrs} años de deriva de formato, normalizados a {n} regímenes",
        "f06_sub": "Cada fila llama al parser real (vp_data.visa_common) con la celda tal cual se publicó: "
        "nada se corrige en silencio y la celda cruda se conserva siempre en raw_value.",
        "f06_cols": ["celda cruda", "valor parseado", "regla aplicada"],
        "f06_empty": "(vacía)",
        "f06_current": "Current",
        "f06_unavailable": "Unavailable",
        "f06_unk": "sin dato",
        "f06_rules": [
            "fecha exacta: strptime %d%b%y",
            "pivote de siglo: %y 69–99 → 19xx (guardia anti-futuro contra el boletín)",
            "footnote tolerado: se extrae el token de fecha y se valida con strptime",
            "Current — sin atraso ese mes; anotación descriptiva, no objetivo",
            "footnote tolerado también en el estado (U* → U)",
            "celda vacía → centinela UNK (nunca el string NA); raw_value se preserva",
        ],
        # f07 — pipeline de limpieza
        "f07_head": "De HTML congelado a objetivo evaluable: {n} etapas, cero correcciones en silencio",
        "f07_sub": "Cada etapa publica su ledger: los ceros son inalcanzables por construcción (guardias "
        "que abortan el build) y lo anómalo se anota, jamás se recorta.",
        "f07_stages": [
            "HTML congelado (S3)",
            "Parser tolerante",
            "Panel y(p,c,b,t)",
            "Gates + ledger",
            "Almacén estrella (CHECKs)",
            "Máscara F-only (evaluación)",
        ],
        "f07_stats": [
            "{n_months} boletines mensuales",
            "{unp} fechas F imparseables",
            "{n_rows} filas · {n_series} series",
            "{dups} duplicados colapsados · {jumps} saltos >8 años anotados",
            "{under} desbordes de época (el CHECK re-verifica la aritmética)",
            "{nf} filas F ({pct}% del panel)",
        ],
    },
    "en": {
        "footer": "Source: U.S. Department of State, Visa Bulletin (Dec 2001 – {mes} {anio}).",
        "f01_head": "Trees don't extrapolate: they are trained on the monthly advance, not the level",
        "f01_sub": "Series {name}: the level carries decades of trend (a tree saturates at the maximum it "
        "saw); the first difference is stationary and the forecast is reintegrated causally.",
        "f01_series": "Rest of world · F2A · FAD",
        "f01_a": "(a) Series in levels: strong trend (non-stationary)",
        "f01_b": "(b) First difference: stationary and extrapolable",
        "f01_ylab_a": "Priority date (year)",
        "f01_ylab_b": "Monthly advance (days)",
        "f01_clip": "{n} steps outside [{lo}, {hi}] d (extreme: {worst} d), clipped on screen only",
        "f02_head": "December and January are neighbors: the fiscal calendar is encoded on a circle",
        "f02_sub": "Sine/cosine of the month's position in the fiscal year (starts in October, when quotas "
        "reset), produced by the canonical encoder the models consume.",
        "f02_months": ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"],
        "f02_a": "(a) Sine/cosine components",
        "f02_b": "(b) Months on the unit circle",
        "f02_xlab": "Fiscal-year month",
        "f02_ylab": "Encoding value",
        "f03_head": "The dominant feature: {top}",
        "f03_sub": "Gain importance of a LightGBM trained on the monthly advance of the family FAD series "
        "(advance lags + cyclic calendar).",
        "f03_lag": "Advance at $t-{k}$",
        "f03_sin": "Calendar (sine)",
        "f03_cos": "Calendar (cosine)",
        "f03_xlab": "Importance (gain, LightGBM)",
        "f04_head": "Gaps of up to {cap} months are interpolated; the {n} long runs are never invented",
        "f04_sub": "{name}: {miss} months without a date across {runs} runs (the longest, {maxrun} months). "
        "Short runs (≤{cap}) are linearly interpolated for the training grid; long ones stay empty and only "
        "the EDA characterizes them with Kalman smoothing.",
        "f04_series": "Mexico · EB4_RW (religious workers) · FAD",
        "f04_long": "{n}-month gap:\nKalman for EDA only,\nnever for training",
        "f04_obs": "F observation (real)",
        "f04_interp": "short interpolation (≤{cap} mo)",
        "f04_kalman": "Kalman imputation (EDA only)",
        "f04_gap": "long gap (never invented)",
        "f04_ylab": "Priority date (year)",
        "f05_head": "C and U are annotation, not targets: only the {nf} published dates are scored",
        "f05_sub": "{name}: the line exists only in F months; the backgrounds mark the bulletin regime "
        "({nc} Current months, {nu} Unavailable, {nk} no data). The bottom strip shows the months the "
        "evaluation actually scores (F-only mask).",
        "f05_series": "India · EB-2 · FAD",
        "f05_strip": "scored (F)",
        "f05_F": "F — published date ({n})",
        "f05_C": "C — Current ({n})",
        "f05_U": "U — Unavailable ({n})",
        "f05_UNK": "no data ({n})",
        "f05_ylab": "Priority date (year)",
        "f06_head": "{yrs} years of format drift, normalized to {n} regimes",
        "f06_sub": "Each row calls the real parser (vp_data.visa_common) with the cell exactly as "
        "published: nothing is silently corrected and the raw cell is always kept in raw_value.",
        "f06_cols": ["raw cell", "parsed value", "rule applied"],
        "f06_empty": "(empty)",
        "f06_current": "Current",
        "f06_unavailable": "Unavailable",
        "f06_unk": "no data",
        "f06_rules": [
            "exact date: strptime %d%b%y",
            "century pivot: %y 69–99 → 19xx (anti-future guard against the bulletin)",
            "footnote tolerated: the date token is extracted and validated with strptime",
            "Current — no backlog that month; descriptive annotation, not a target",
            "footnote tolerated in the status too (U* → U)",
            "empty cell → UNK sentinel (never the string NA); raw_value is preserved",
        ],
        "f07_head": "From frozen HTML to an evaluable target: {n} stages, zero silent corrections",
        "f07_sub": "Every stage publishes its ledger: the zeros are unreachable by construction (guards "
        "that abort the build) and anomalies are annotated, never trimmed.",
        "f07_stages": [
            "Frozen HTML (S3)",
            "Tolerant parser",
            "Panel y(p,c,b,t)",
            "Gates + ledger",
            "Star schema (CHECKs)",
            "F-only mask (evaluation)",
        ],
        "f07_stats": [
            "{n_months} monthly bulletins",
            "{unp} unparseable F dates",
            "{n_rows} rows · {n_series} series",
            "{dups} duplicates collapsed · {jumps} >8-year jumps annotated",
            "{under} epoch underflows (the CHECK re-verifies the arithmetic)",
            "{nf} F rows ({pct}% of the panel)",
        ],
    },
}
T: dict = TXT["es"]
MESL: dict[int, str] = MES


def _apply_lang(lang: str) -> None:
    """Re-vincula los textos visibles (T) y los nombres de mes (MESL)."""
    global LANG, T, MESL
    LANG = lang
    T = TXT[lang]
    MESL = MES if lang == "es" else MES_EN


def _num(v: int) -> str:
    """Separador de miles con coma (27,611) — convención del proyecto en ambos idiomas."""
    return f"{v:,}"


def _load() -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv", parse_dates=["bulletin_date"])
    facts = json.loads((ROOT / "reports" / "fe" / "fe_facts.json").read_text())
    return df, facts


def _save(fig: plt.Figure, name: str) -> plt.Figure:
    """Guarda el par PDF-vector (.tex, nombres fe_*) + PNG-300dpi y DEVUELVE la figura viva.

    El caller cierra (``plt.close``); ``build_fe_report`` la re-usa como página
    vectorial. Variantes solo-web (EN y/o oscura): solo PNG en el subdir que toca.
    """
    if LANG == "en" or DARK_MODE:
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
    tex_name = "fe_" + name.split("_", 1)[1]  # f04_gaps -> fe_gaps.pdf (nombres históricos del .tex)
    fig.savefig(FIG_TEX / f"{tex_name}.pdf", bbox_inches="tight")
    fig.savefig(FIG_PNG / f"{name}.png", bbox_inches="tight", dpi=300)
    print(f"{tex_name} OK")
    return fig


def _header(fig: plt.Figure, headline: str, sub: str, y: float = 1.02, dy: float = 0.055) -> None:
    """Titular-frase (el hallazgo) + bajada explicativa, estilo editorial (= galería EDA)."""
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
    brand_y = y - 0.035 if len(text) > 80 else y
    fig.text(0.99, brand_y, "VisaPredict AI", fontsize=8.5, color=BLUE, ha="right", fontweight="bold")


# ------------------------------------------------------------------------------- f01
def f01_differencing(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """Niveles vs. primera diferencia: por qué los árboles ven Δy, no y."""
    s = (
        df[(df.status == "F") & (df.country == "all_chargeability") & (df.category == "F2A") & (df.table == "FAD")]
        .sort_values("bulletin_date")
        .copy()
    )
    s["years"] = days_to_year(s.days_since_base)
    s["delta"] = s.days_since_base.diff()
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.8, 4.6), sharex=True, gridspec_kw={"hspace": 0.32})
    a1.plot(s.bulletin_date, s.years, color=BLUE, lw=1.4)
    a1.set_ylabel(T["f01_ylab_a"], fontsize=8)
    a1.set_title(T["f01_a"], fontsize=9, color=BLUE, loc="left")
    # AC1: sin recorte silencioso — el rango central se muestra y lo extremo se ANOTA
    # (las retrogresiones/saltos grandes son señal real, no ruido a esconder).
    lo, hi = -120, 260
    n_lo, n_hi = int((s.delta < lo).sum()), int((s.delta > hi).sum())
    a2.fill_between(s.bulletin_date, 0, s.delta.clip(lo, hi), color=GOLD, alpha=0.9, step="mid")
    if n_lo or n_hi:
        worst = s.delta.min() if n_lo else s.delta.max()
        a2.text(
            0.01,
            0.05,
            T["f01_clip"].format(n=n_lo + n_hi, lo=lo, hi=hi, worst=f"{worst:,.0f}"),
            transform=a2.transAxes,
            fontsize=7,
            color=GRAY,
        )
    a2.axhline(0, color=GRAY, lw=0.8)
    a2.set_ylim(lo - 10, hi + 10)
    a2.set_ylabel(T["f01_ylab_b"], fontsize=8)
    a2.set_title(T["f01_b"], fontsize=9, color=BLUE, loc="left")
    for ax in (a1, a2):
        ax.grid(True, axis="y", color=GRID, lw=0.6)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    _header(fig, T["f01_head"], T["f01_sub"].format(name=T["f01_series"]))
    _footer(fig, facts["vintage"], y=-0.075)
    return _save(fig, "f01_differencing")


# ------------------------------------------------------------------------------- f02
def f02_calendar(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """Codificación cíclica del año fiscal desde el encoder CANÓNICO (AD4)."""
    months = T["f02_months"]
    idx = pd.date_range("2025-10-01", periods=12, freq="MS")  # un año fiscal completo
    cal = preprocess.calendar_features(idx)
    k = np.arange(12)
    sin, cos = cal["fiscal_sin"].to_numpy(), cal["fiscal_cos"].to_numpy()
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.8, 3.5))
    xx = np.linspace(0, 12, 300)
    a1.plot(xx, np.sin(2 * np.pi * xx / 12), color=BLUE, lw=1.6, label=r"$\sin(2\pi m/12)$")
    a1.plot(xx, np.cos(2 * np.pi * xx / 12), color=GOLD, lw=1.6, label=r"$\cos(2\pi m/12)$")
    a1.scatter(k, sin, color=BLUE, s=22, zorder=5)
    a1.scatter(k, cos, color=GOLD, s=22, zorder=5)
    a1.set_xticks(k, months, fontsize=7)
    a1.set_xlabel(T["f02_xlab"], fontsize=8)
    a1.set_ylabel(T["f02_ylab"], fontsize=8)
    a1.set_title(T["f02_a"], fontsize=9, color=BLUE, loc="left")
    a1.set_ylim(-1.12, 1.18)
    a1.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.6, 1.0), frameon=False)
    a1.grid(True, axis="y", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        a1.spines[sp].set_visible(False)
    a2.add_patch(plt.Circle((0, 0), 1, fill=False, color=MID, lw=1.0))
    a2.scatter(sin, cos, color=BLUE, s=40, zorder=5)
    for kk, mm in zip(k, months, strict=True):
        a2.annotate(
            mm, (sin[kk], cos[kk]), fontsize=7, ha="center", xytext=(sin[kk] * 1.18, cos[kk] * 1.18), textcoords="data"
        )
    a2.set_xlim(-1.45, 1.45)
    a2.set_ylim(-1.45, 1.45)
    a2.set_aspect("equal")
    a2.axis("off")
    a2.set_title(T["f02_b"], fontsize=9, color=BLUE, loc="left")
    _header(fig, T["f02_head"], T["f02_sub"], y=1.0, dy=0.075)
    _footer(fig, facts["vintage"], y=-0.09)
    return _save(fig, "f02_calendar")


# ------------------------------------------------------------------------------- f03
def f03_importance(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """Importancia de un LightGBM sobre Δy (rezagos + calendario canónico)."""
    import lightgbm as lgb

    f = df[(df.status == "F") & (df.block == "family") & (df.table == "FAD")].sort_values("bulletin_date").copy()
    f["delta"] = f.groupby(["country", "category"])["days_since_base"].diff()
    # AD4: encoder canónico (fiscal_sin/fiscal_cos), no el (month-10)%12 a mano.
    cal = preprocess.calendar_features(pd.DatetimeIndex(f.bulletin_date))
    f["mes_sin"] = cal["fiscal_sin"].to_numpy()
    f["mes_cos"] = cal["fiscal_cos"].to_numpy()
    lags = [1, 2, 3, 6, 12]
    for lg in lags:
        f[f"rezago_{lg}"] = f.groupby(["country", "category"])["delta"].shift(lg)
    feats = [f"rezago_{lg}" for lg in lags] + ["mes_sin", "mes_cos"]
    data = f.dropna(subset=[*feats, "delta"])
    model = lgb.LGBMRegressor(n_estimators=300, max_depth=4, learning_rate=0.05, verbose=-1, random_state=42)
    model.fit(data[feats], data["delta"])
    imp = pd.Series(model.feature_importances_, index=feats).sort_values()
    nice = {f"rezago_{lg}": T["f03_lag"].format(k=lg) for lg in lags}
    nice |= {"mes_sin": T["f03_sin"], "mes_cos": T["f03_cos"]}
    fig, ax = plt.subplots(figsize=(7.4, 3.6))
    is_cal = [k in ("mes_sin", "mes_cos") for k in imp.index]
    cols = [GOLD if c else BLUE for c in is_cal]
    ax.barh([nice[k] for k in imp.index], imp.to_numpy(), color=cols, edgecolor=PAPER)
    ax.set_xlabel(T["f03_xlab"], fontsize=8)
    ax.grid(True, axis="x", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    top = nice[str(imp.index[-1])]  # el titular se DERIVA del modelo, no se tipea
    _header(fig, T["f03_head"].format(top=top), T["f03_sub"], y=1.01, dy=0.09)
    _footer(fig, facts["vintage"], y=-0.10)
    return _save(fig, "f03_importance")


# ------------------------------------------------------------------------------- f04
def f04_gaps(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """La decisión maestra MNAR: interpolar corto, jamás inventar largo, Kalman solo EDA."""
    cap = int(facts["constants"]["max_interpolable_gap"])
    country, category, table = "mexico", "EB4_RW", "FAD"
    raw = dataset.load_series(country, category, table)
    prof = missingness.profile(country, category, table)
    grid_idx = pd.date_range(raw.index.min(), raw.index.max(), freq="MS")
    grid = raw.reindex(grid_idx).astype("float64")
    reg = preprocess.to_regular_monthly(raw, max_gap=cap)
    kal = missingness.kalman_impute(raw)

    # corridas de hueco sobre la rejilla mensual (contiguas de NaN)
    isna = grid.isna().to_numpy()
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(isna):
        if isna[i]:
            j = i
            while j + 1 < len(isna) and isna[j + 1]:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    short = [r for r in runs if r[1] - r[0] + 1 <= cap]
    long_ = [r for r in runs if r[1] - r[0] + 1 > cap]

    fig, ax = plt.subplots(figsize=(8.4, 4.4))
    pad = pd.DateOffset(days=15)
    for i0, i1 in long_:
        ax.axvspan(grid_idx[i0] - pad, grid_idx[i1] + pad, color=UNK_FILL, zorder=0, lw=0)
        seg = kal.iloc[max(i0 - 1, 0) : i1 + 2]
        ax.plot(seg.index, days_to_year(seg), color=MID, ls=":", lw=1.4, zorder=2)
    for i0, i1 in short:
        seg = reg.iloc[max(i0 - 1, 0) : i1 + 2]
        ax.plot(seg.index, days_to_year(seg), color=GOLD, lw=2.0, zorder=3, solid_capstyle="round")
    ax.scatter(raw.index, days_to_year(raw), s=10, color=BLUE, zorder=4, lw=0)
    # anotación derivada sobre la corrida más larga
    wi0, wi1 = max(long_, key=lambda r: r[1] - r[0])
    mid_t = grid_idx[(wi0 + wi1) // 2]
    y_lo, y_hi = ax.get_ylim()
    ax.text(
        mid_t,
        y_lo + (y_hi - y_lo) * 0.58,
        T["f04_long"].format(n=prof.max_gap_run),
        ha="center",
        fontsize=7.6,
        color=GRAY,
        style="italic",
    )
    ax.set_ylabel(T["f04_ylab"])
    ax.grid(True, axis="y", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    handles = [
        plt.Line2D([0], [0], marker="o", ls="", mfc=BLUE, mec=BLUE, ms=5, label=T["f04_obs"]),
        plt.Line2D([0], [0], color=GOLD, lw=2.0, label=T["f04_interp"].format(cap=cap)),
        plt.Line2D([0], [0], color=MID, ls=":", lw=1.4, label=T["f04_kalman"]),
        Patch(fc=UNK_FILL, ec=MID, lw=0.4, label=T["f04_gap"]),
    ]
    ax.legend(handles=handles, fontsize=7.4, frameon=False, loc="lower right")
    _header(
        fig,
        T["f04_head"].format(cap=cap, n=len(long_)),
        T["f04_sub"].format(
            name=T["f04_series"], miss=prof.n_missing, runs=prof.n_gap_runs, maxrun=prof.max_gap_run, cap=cap
        ),
    )
    _footer(fig, facts["vintage"], y=-0.08)
    return _save(fig, "f04_gaps")


# ------------------------------------------------------------------------------- f05
def f05_regime(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """La decisión maestra B1: los regímenes C/U anotan; solo las fechas F se puntúan."""
    country, category, table = "india", "EB2", "FAD"
    s = df[(df.country == country) & (df.category == category) & (df.table == table)].sort_values("bulletin_date")
    grid_idx = pd.date_range(s.bulletin_date.min(), s.bulletin_date.max(), freq="MS")
    status = s.set_index("bulletin_date").status.reindex(grid_idx).fillna("UNK")
    fdays = s[s.status == "F"].set_index("bulletin_date").days_since_base.reindex(grid_idx)
    counts = status.value_counts()

    fig, (ax, axs) = plt.subplots(
        2, 1, figsize=(8.4, 4.8), height_ratios=(6, 0.55), sharex=True, gridspec_kw={"hspace": 0.07}
    )
    pad = pd.DateOffset(days=15)
    # bandas de régimen: corridas contiguas del mismo estado NO-F (F queda en papel)
    codes = status.to_numpy()
    i = 0
    while i < len(codes):
        if codes[i] != "F":
            j = i
            while j + 1 < len(codes) and codes[j + 1] == codes[i]:
                j += 1
            ax.axvspan(grid_idx[i] - pad, grid_idx[j] + pad, color=_regime_fill(str(codes[i])), zorder=0, lw=0)
            i = j + 1
        else:
            i += 1
    ax.plot(grid_idx, days_to_year(fdays), color=BLUE, lw=1.3, zorder=3)
    ax.set_ylabel(T["f05_ylab"])
    ax.grid(True, axis="y", color=GRID, lw=0.6)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    handles = [
        plt.Line2D([0], [0], color=BLUE, lw=1.6, label=T["f05_F"].format(n=int(counts.get("F", 0)))),
        Patch(fc=_regime_fill("C"), label=T["f05_C"].format(n=int(counts.get("C", 0)))),
        Patch(fc=_regime_fill("U"), label=T["f05_U"].format(n=int(counts.get("U", 0)))),
        Patch(fc=_regime_fill("UNK"), ec=MID, lw=0.4, label=T["f05_UNK"].format(n=int(counts.get("UNK", 0)))),
    ]
    ax.legend(handles=handles, fontsize=7.2, frameon=False, loc="upper left", ncol=2)
    # franja inferior: los meses que la evaluación PUNTÚA (máscara F-only, fix B1)
    f_mask = codes == "F"
    i = 0
    while i < len(f_mask):
        if f_mask[i]:
            j = i
            while j + 1 < len(f_mask) and f_mask[j + 1]:
                j += 1
            axs.axvspan(grid_idx[i] - pad, grid_idx[j] + pad, color=BLUE, lw=0)
            i = j + 1
        else:
            i += 1
    axs.set_yticks([])
    axs.set_ylim(0, 1)
    axs.text(-0.012, 0.5, T["f05_strip"], transform=axs.transAxes, ha="right", va="center", fontsize=7, color=BLUE)
    for sp in ("top", "right", "left"):
        axs.spines[sp].set_visible(False)
    _header(
        fig,
        T["f05_head"].format(nf=int(counts.get("F", 0))),
        T["f05_sub"].format(
            name=T["f05_series"],
            nc=int(counts.get("C", 0)),
            nu=int(counts.get("U", 0)),
            nk=int(counts.get("UNK", 0)),
        ),
    )
    _footer(fig, facts["vintage"], y=-0.075)
    return _save(fig, "f05_regime")


# ------------------------------------------------------------------------------- f06
def f06_parser(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """Panel tipográfico: el parser REAL normaliza 6 celdas verificables en vivo."""
    bulletin = pd.Timestamp(facts["vintage"] + "-01").to_pydatetime()
    examples = ["01MAY16", "08OCT79", "15JUL05*", "C", "U*", ""]
    rows = []
    for raw, rule in zip(examples, T["f06_rules"], strict=True):
        st = classify_status(raw)
        dt = string_to_datetime(raw, bulletin)
        if st == "F" and dt is not None:
            parsed = dt.date().isoformat()
        else:
            parsed = {"C": T["f06_current"], "U": T["f06_unavailable"], "UNK": T["f06_unk"]}[st]
        rows.append((raw, st, parsed, rule))
    n_regimes = len({st for _, st, _, _ in rows})
    yrs = int(df.bulletin_date.max().year - df.bulletin_date.min().year)

    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    cols_x = (0.03, 0.30, 0.55)
    for x, name in zip(cols_x, T["f06_cols"], strict=True):
        ax.text(x, 0.97, name, fontsize=8.5, color=GRAY, fontweight="bold", va="top")
    y = 0.86
    step = 0.148
    for irow, (raw, st, parsed, rule) in enumerate(rows):
        if irow % 2 == 1:
            ax.add_patch(Rectangle((0.0, y - 0.088), 1.0, step, fc=STRIPE, lw=0, zorder=0))
        ax.add_patch(Rectangle((cols_x[0], y - 0.052), 0.185, 0.082, fc=_regime_fill(st), ec=MID, lw=0.5, zorder=1))
        shown = raw if raw else T["f06_empty"]
        ax.text(
            cols_x[0] + 0.0925, y - 0.011, shown, fontsize=9, color=INK, family="monospace", ha="center", va="center"
        )
        line_c = {"F": BLUE, "C": TEAL, "U": WINE, "UNK": MID}[st]
        ax.text(cols_x[1], y - 0.011, st, fontsize=9.5, color=line_c, fontweight="bold", va="center")
        ax.text(cols_x[1] + 0.085, y - 0.011, parsed, fontsize=9, color=INK, family="monospace", va="center")
        ax.text(cols_x[2], y - 0.011, textwrap.fill(rule, 46), fontsize=7.8, color=GRAY, va="center")
        y -= step
    _header(fig, T["f06_head"].format(yrs=yrs, n=n_regimes), T["f06_sub"], y=0.985, dy=0.05)
    _footer(fig, facts["vintage"], y=0.005)
    return _save(fig, "f06_parser")


# ------------------------------------------------------------------------------- f07
def f07_pipeline(df: pd.DataFrame, facts: dict) -> plt.Figure:
    """El pipeline de limpieza end-to-end con el ledger vivo bajo cada etapa."""
    led = facts["cleaning_ledger"]
    n_rows, n_f = int(led["n_rows"]), int(led["rows_by_status"]["F"])
    stats = [
        s.format(
            n_months=int(df.bulletin_date.dt.to_period("M").nunique()),
            unp=int(led["f_priority_date_unparseable"]),
            n_rows=_num(n_rows),
            n_series=int(led["n_series"]),
            dups=int(led["dup_collapsed"]),
            jumps=int(led["big_jumps_gt_8y"]),
            under=int(led["epoch_underflow"]),
            nf=_num(n_f),
            pct=f"{100 * n_f / n_rows:.0f}",
        )
        for s in T["f07_stats"]
    ]
    stages = T["f07_stages"]
    fig, ax = plt.subplots(figsize=(10.6, 3.4))
    ax.set_axis_off()
    ax.set_xlim(0, len(stages))
    ax.set_ylim(0, 1)
    for i, (title, stat) in enumerate(zip(stages, stats, strict=True)):
        x0, x1 = i + 0.04, i + 0.96
        ax.add_patch(Rectangle((x0, 0.24), x1 - x0, 0.30, fc=PAPER, ec=MID, lw=0.8, zorder=1))
        ax.add_patch(Rectangle((x0, 0.54), x1 - x0, 0.30, fc=BLUE, ec=BLUE, lw=0.8, zorder=1))
        ax.text(x0 + 0.06, 0.795, str(i + 1), fontsize=13, color=PAPER, fontweight="bold", va="center", zorder=2)
        ax.text(
            x0 + 0.17,
            0.69,
            textwrap.fill(title, 13),
            fontsize=7.4,
            color=PAPER,
            fontweight="bold",
            va="center",
            zorder=2,
        )
        ax.text(
            (x0 + x1) / 2,
            0.39,
            textwrap.fill(stat, 22),
            fontsize=7.0,
            color=INK,
            ha="center",
            va="center",
            zorder=2,
        )
        if i < len(stages) - 1:
            ax.add_patch(
                FancyArrowPatch(
                    (x1, 0.54), (i + 1 + 0.04, 0.54), arrowstyle="-|>", mutation_scale=11, color=MID, lw=1.1, zorder=3
                )
            )
    _header(fig, T["f07_head"].format(n=len(stages)), T["f07_sub"], y=0.93, dy=0.09)
    _footer(fig, facts["vintage"], y=0.03)
    return _save(fig, "f07_pipeline")


MAKERS = (
    f01_differencing,
    f02_calendar,
    f03_importance,
    f04_gaps,
    f05_regime,
    f06_parser,
    f07_pipeline,
)


def _run_all(df: pd.DataFrame, facts: dict) -> None:
    for fn in MAKERS:
        plt.close(fn(df, facts))


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
    print("Galería FE (es/en × clara/oscura) en", FIG_TEX, "y", FIG_PNG)
