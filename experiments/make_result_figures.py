"""Genera las figuras de RESULTADOS del entregable LaTeX (PI-I), paleta UACJ + serif, PDF vectorial.

Las cuatro figuras que faltaban en el Cap. de Resultados (hoy todo en tablas):
  F1  ranking MASE de los 21 modelos (FAD + DFF)          -> results_ranking_mase.pdf
  F2  pronóstico del ganador profundo vs real + PI 95%    -> results_forecast_winner.pdf
  F3  intervalo de confianza multi-semilla deep vs listón -> results_multiseed_ci.pdf
  F4  cobertura del PI antes/después de ACI + CRPS        -> results_coverage_crps.pdf

Lee solo CSVs ya versionados en reports/. Corre en `ante` (vp_model + matplotlib):
    ante/bin/python make_result_figures.py [--crps-parent-root RAIZ_MADRE]

★ M74-F · F4 rechaza un CRPS de referencia no acreditado (decisión del autor, 13-oct-2026): el panel (b) lee
`crps_fad.csv` a través de `champion.baseline_crps_provenance`; si no sale ``acreditado`` (sin recibo, de otra
transacción, o una complementaria sin la raíz de su madre, que se da con ``--crps-parent-root``), el script falla
con el motivo ANTES de generar ninguna figura.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REP = ROOT / "reports"
FIG = REP / "latex" / "Figures"

from vp_model.config import BASE_EPOCH, days_to_year  # noqa: E402
from vp_model.palette import BLUE, FONT_RC, GOLD, GRAY, GRID, INK, MID, MUTE, STRIPE, WARN, series_label  # noqa: E402

BLACK = INK  # alias: el cuerpo usa BLACK para el texto/línea dominante

# C5/R1: el estilo de imprenta de los resultados ya NO se aplica al importar. Es
# monolingue y solo emite PDF, asi que no tiene variantes; pero su marco visual vive en
# un tema reversible como el de las galerias, y se aplica al generar, no al importar.
RESULTS_RC = {
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


def results_style():
    """El estilo de imprenta de estas figuras, en un contexto reversible.

    Es `RESULTS_RC` y nada más: un `Theme` completo del kit añadiría las semánticas de
    color de la web (título azul, texto y ejes grises) que este generador nunca aplicó, y
    cambiaría el aspecto de todas las figuras del entregable.

    El import va aquí dentro a propósito: este archivo coloca sus imports después de
    definir ROOT (E402 ya suprimido línea a línea), y añadir una supresión más solo para el
    kit haría subir el trinquete de deuda por una razón puramente cosmética.
    """
    from _figkit import plain_style

    return plain_style(RESULTS_RC)


def _emit(fig: plt.Figure, name: str) -> None:
    """Guarda el PDF del .tex y cierra la figura.

    Este generador es monolingue y solo emite PDF vectorial para el entregable, asi que
    no usa `save_dual` del kit: no tiene variantes de idioma ni de tema. Lo que si era
    duplicacion real era este par savefig/close repetido en cada figura.
    """
    # ★ R21 · desde M66 los .tex viven en el repositorio documental y `reports/latex/Figures/` es una salida
    # LOCAL ignorada por git que ya no existe en un checkout limpio: la 3ª campaña real habría muerto aquí.
    FIG.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG / f"{name}.pdf")
    plt.close(fig)


#: Listón parsimonioso = la mejor MASE de RESERVADO de estos modelos (ETS/Theta en FAD; en DFF Theta colapsa al paseo
#: aleatorio, así que el listón es ETS). G7 · B1: antes era `fad_champion_mase`, una MASE de SELECCIÓN dibujada sobre ejes
#: de reservado; con la campaña causal las dos métricas se separaron y F1/F3 mostraban al profundo por encima de un
#: listón que en reservado no existía.
LISTON = {"FAD": ("ets", "theta"), "DFF": ("ets",)}

#: Rótulo legible de cada modelo en el texto de las figuras (nunca el identificador crudo del código).
MODEL_LABEL = {
    "naive1": "naïve-1",
    "naive": "naïve",
    "drift": "Drift",
    "theta": "Theta",
    "ets": "ETS",
    "arima": "ARIMA",
    "sarima": "SARIMA",
    "prophet": "Prophet",
    "lightgbm": "LightGBM",
    "xgboost": "XGBoost",
    "catboost": "CatBoost",
    "llt": "LLT",
    "kalman": "Kalman",
    "nlinear": "NLinear",
    "dlinear": "DLinear",
    "rlinear": "RLinear",
    "tft": "TFT",
    "tide": "TiDE",
    "nhits": "N-HiTS",
    "nbeats": "N-BEATS",
    "lstm": "LSTM",
    "arima_lstm": "ARIMA-LSTM",
    "deepar": "DeepAR",
    "chronos": "Chronos",
}
MES_ABBR = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _mlabel(m: str) -> str:
    return MODEL_LABEL.get(m, m)


def _fmt_decimal_year(v: float, _pos=None) -> str:
    """Eje de fecha de prioridad en años decimales -> 'jul 2007' (o '2008' si cae en enero)."""
    y = int(np.floor(v + 1e-9))
    m = int(round((v - y) * 12))
    if m == 12:
        y, m = y + 1, 0
    return f"{y}" if m == 0 else f"{MES_ABBR[m]} {y}"


def _year_ticks(ax, max_ticks: int) -> None:
    """Ticks del eje de años decimales en pasos de calendario (trimestre, semestre, año...)."""
    lo, hi = ax.get_ylim()
    for step in (0.25, 0.5, 1, 2, 5, 10, 20):
        if (hi - lo) / step <= max_ticks:
            break
    ax.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(step))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_decimal_year))


def _month_axis(ax, months: tuple[int, ...] = (1, 7)) -> None:
    """Eje x de fechas mensuales en español ('ene\n2025') en lugar de '2025-01'."""
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=months))
    ax.xaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(
            lambda v, _: f"{MES_ABBR[mdates.num2date(v).month - 1]}\n{mdates.num2date(v).year}"
        )
    )


def _liston(table: str, universo: set[tuple[str, str]] | None = None) -> tuple[float, int, str]:
    """``(valor, n series, nombre)``: la mejor MASE media de reservado de los modelos del listón sobre ``universo``.

    ``universo`` = las series que de verdad puntúa lo que se dibuja junto al listón; ``None`` = todas las de la tabla.
    Un modelo del listón que no cubre una serie del universo no se promedia en silencio: la figura no se dibuja.
    """
    df = pd.read_csv(REP / "eval" / f"model_comparison_{table}21.csv").pipe(lambda d: d[d.run_id == d.run_id.max()])
    series = sorted(universo if universo is not None else set(zip(df.country, df.category, strict=True)))
    medias = {}
    for modelo in LISTON[table]:
        hold = df[df.model == modelo].set_index(["country", "category"])["hold_mase"].reindex(series)
        faltan = [k for k, v in hold.items() if not np.isfinite(v)]
        if faltan:
            raise SystemExit(
                f"listón {table}: {modelo} no cubre {len(faltan)} serie(s) del universo dibujado {faltan[:3]}"
            )
        medias[modelo] = float(hold.mean())
    nombre = "/".join("ETS" if m == "ets" else m.capitalize() for m in LISTON[table])
    return min(medias.values()), len(series), nombre


# ---------- F1: ranking MASE de los 21 modelos ----------
def fig_ranking() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(6.25, 3.9))
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        df = pd.read_csv(REP / "eval" / f"model_comparison_{table}21.csv").pipe(lambda d: d[d.run_id == d.run_id.max()])
        # Sólo se ranquea contra el listón lo que puntúa todas las series del listón: un modelo que no converge en
        # algunas series se promediaría sobre otras (ARIMA DFF: 0.120 en 23 series frente a ETS 0.115 en esas 23
        # y 0.129 en las 25). Los parciales se declaran aparte con su cobertura, sin punto ni línea.
        universo = set(zip(df.country, df.category, strict=True))
        liston, n_series, nombre = _liston(table, universo)
        validas = df[np.isfinite(df.hold_mase)]
        cobertura = validas.drop_duplicates(["model", "country", "category"]).groupby("model").size()
        cobertura = cobertura.reindex(sorted(set(df.model)), fill_value=0)
        completos = cobertura.index[cobertura == n_series]
        parciales = cobertura[cobertura < n_series].sort_values(ascending=False)
        medias = validas[validas.model.isin(completos)].groupby("model")["hold_mase"].mean()
        m = medias[medias <= 0.40].sort_values(ascending=False)  # recorta off-scale; se anota aparte
        n_off = int(medias.gt(0.40).sum())
        colors = [BLUE if mm in ("ets", "theta") else GRAY for mm in m.index]
        y = np.arange(len(m))
        ax.hlines(y, liston, m.values, color=MUTE, lw=1.2, zorder=1)
        ax.scatter(m.values, y, color=colors, s=34, zorder=3, edgecolor=BLACK, linewidth=0.4)
        ax.axvline(liston, color=GOLD, ls="--", lw=1.3, zorder=2)
        ax.set_yticks(y)
        ax.set_yticklabels(m.index, fontsize=7.5)  # identificador crudo: lo fijan las pruebas G7 (F1)
        ax.tick_params(axis="x", labelsize=7.5)
        notas = [f"{n_off} fuera de escala (>0.40)"] if n_off else []
        if len(parciales):
            notas.append("cobertura parcial, fuera del ranking:")
            notas.append(", ".join(f"{k} {v}/{n_series}" for k, v in parciales.items()))
        ax.set_xlabel("\n".join(["MASE en el lapso reservado", *notas]), fontsize=7.5)
        ax.set_title(f"({'a' if table == 'FAD' else 'b'}) {table} · {n_series} series familiares", fontsize=9)
        ax.text(
            liston,
            len(m) - 0.3,
            f" listón {nombre} {liston:.3f} (reservado)",
            color=GOLD,
            fontsize=7,
            va="top",
        )
    fig.tight_layout()
    _emit(fig, "results_ranking_mase")
    print("F1 ranking OK")


# ---------- F2: pronóstico del ganador vs real + banda PI 95% ----------
def _to_year(days: np.ndarray) -> np.ndarray:
    return days_to_year(np.asarray(days, dtype="float64"))  # AD3


def fig_forecast() -> None:
    picks = {"DFF": ("BiTCN", None), "FAD": ("BiTCN", None)}
    fig, axes = plt.subplots(1, 2, figsize=(6.25, 2.9))
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        col = picks[table][0]
        d = pd.read_csv(REP / "eval" / f"deep_pi_{table}.csv", parse_dates=["ds"])
        # serie con más puntos en el hold-out (la más informativa de graficar)
        uid = d.groupby("unique_id").size().idxmax()
        g = d[d.unique_id == uid].sort_values("ds")
        x = g["ds"]
        ax.fill_between(
            x,
            _to_year(g[f"{col}-lo-95"]),
            _to_year(g[f"{col}-hi-95"]),
            color=BLUE,
            alpha=0.15,
            label="Intervalo de predicción 95 %",
        )
        ax.plot(x, _to_year(g["y"]), color=BLACK, lw=1.6, label="Real (F)")
        ax.plot(x, _to_year(g[col]), color=BLUE, lw=1.4, ls="--", label=f"{col} (pronóstico)")
        country, _b, cat = uid.split("/")
        ax.set_title(f"({'a' if table == 'FAD' else 'b'}) {series_label(f'{country}/{cat}/{table}')}", fontsize=9)
        ax.set_ylabel("Fecha de prioridad", fontsize=8)
        _year_ticks(ax, 6)
        _month_axis(ax)
        ax.tick_params(labelsize=7)
        ax.legend(fontsize=6.5, loc="upper left", framealpha=0.9)
    fig.tight_layout()
    _emit(fig, "results_forecast_winner")
    print("F2 forecast OK")


# ---------- F3: IC multi-semilla deep vs listón ----------
def _seed_scores(pattern: str, col: str, table: str) -> list[pd.Series]:
    """MASE de reservado POR SERIE de cada semilla (el universo que se dibuja). AP2: el puntuador canónico."""
    from vp_model.metrics import mase_by_series

    out = []
    for p in sorted(REP.glob(pattern)):
        df = pd.read_csv(p, parse_dates=["ds"])
        if col not in df.columns:
            continue
        parts = df["unique_id"].str.split("/", expand=True)  # country/block/category
        frame = df.assign(country=parts[0], category=parts[2])
        ms = mase_by_series(frame, table, pred_col=col, date_col="ds", min_points=2)
        if len(ms):
            out.append(ms)
    return out


def fig_multiseed() -> None:
    from scipy import stats

    # G7 · B1: sin «oráculo de selección» (era un 0.113 fijo de una corrida vieja, sin artefacto) y con el listón en
    # MASE de reservado sobre EXACTAMENTE las series que puntúan las semillas.
    specs = [
        ("FAD", "campaign/global_FAD_camp_auto_s*.csv", "AutoBiTCN"),
        ("DFF", "campaign/global_DFF_camp_diff_s*.csv", "BiTCN"),
    ]
    medidas = []
    for table, pat, col in specs:
        scores = _seed_scores(pat, col, table)
        universos = {frozenset(s.index) for s in scores}
        if len(universos) != 1:
            raise SystemExit(
                f"F3 {table}: las {len(scores)} semillas puntúan un universo de series distinto (o ninguno)"
            )
        medidas.append((table, col, np.array([float(s.mean()) for s in scores]), _liston(table, set(universos.pop()))))
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ypos, labels = [], []
    for i, (table, col, vals, (liston, n_series, nombre)) in enumerate(medidas):
        m, sd, n_semillas = vals.mean(), vals.std(ddof=1), len(vals)
        half = stats.t.ppf(0.975, n_semillas - 1) * sd / np.sqrt(n_semillas)
        y = i
        ax.errorbar(
            m,
            y,
            xerr=half,
            fmt="o",
            color=BLUE,
            ecolor=BLUE,
            elinewidth=1.6,
            capsize=4,
            ms=7,
            zorder=3,
            label="Deep global (media ± IC 95%)" if i == 0 else None,
        )
        ax.scatter(
            [liston],
            [y],
            marker="D",
            color=GOLD,
            s=46,
            zorder=4,
            edgecolor=BLACK,
            linewidth=0.4,
            label=f"Listón parsimonioso ({nombre}, reservado, n={n_series})" if i == 0 else None,
        )
        ypos.append(y)
        labels.append(f"{table}\n{col}")
        ax.text(m, y + 0.16, f"{m:.3f}", color=BLUE, ha="center", fontsize=7.5)
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels)
    ax.set_ylim(-0.6, len(specs) - 0.2)
    ax.set_xlabel("MASE en el lapso reservado")
    ax.set_title("Validación multi-semilla: aprendizaje profundo global frente al listón", fontsize=9.5)
    ax.legend(fontsize=7.5, loc="lower right", framealpha=0.95)
    fig.tight_layout()
    _emit(fig, "results_multiseed_ci")
    print("F3 multiseed OK")


# ---------- F4: cobertura PI (antes/después ACI) + CRPS ----------
def _pinball(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    d = y - q
    return float(np.mean(np.maximum(tau * d, (tau - 1) * d)))


def _bitcn_crps(table: str) -> float:
    """CRPS de BiTCN (2·media pinball sobre cuantiles), idéntico a experiments/eval_deep_pi.py, F-only por serie."""
    from vp_model import dataset

    d = pd.read_csv(REP / "eval" / f"deep_pi_{table}.csv", parse_dates=["ds"])
    levels = {95: (0.025, 0.975), 90: (0.05, 0.95), 80: (0.10, 0.90), 50: (0.25, 0.75)}
    crps = []
    for uid, g in d.groupby("unique_id"):
        country, _b, category = uid.split("/")
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        g = g.sort_values("ds")
        g = g[g["ds"].isin(full.index)]
        if g.empty:
            continue
        y = full.reindex(g["ds"]).to_numpy()
        cols_taus = [(g["BiTCN"].to_numpy(), 0.5)]
        for lvl, (lo, hi) in levels.items():
            cols_taus += [(g[f"BiTCN-lo-{lvl}"].to_numpy(), lo), (g[f"BiTCN-hi-{lvl}"].to_numpy(), hi)]
        crps.append(2 * float(np.mean([_pinball(y, q, t) for q, t in cols_taus])))
    return float(np.mean(crps))


def _crps_referencia(parent_root: Path | None = None) -> pd.Series:
    """Media del CRPS por modelo de `crps_fad.csv`, SÓLO si su procedencia se acredita; si no, ``SystemExit``."""
    from vp_model import champion
    from vp_model.artifact_receipt import PANEL_REL

    prov = champion.baseline_crps_provenance(reports=REP, panel=REP.parent / PANEL_REL, parent_root=parent_root)
    if prov["status"] != "acreditado":
        raise SystemExit(f"✗ F4 rechaza el CRPS de referencia ({prov['status']}): {prov['reason']}")
    return pd.Series({m: v for m, v in prov["means"].items() if v is not None}, dtype="float64")


def fig_coverage_crps(crps_parent_root: Path | None = None) -> None:
    # Acredita SIEMPRE: ninguna vía pública dibuja un CRPS que no pase por el lector (auditoría F4).
    crps = _crps_referencia(crps_parent_root)
    cov = pd.read_csv(REP / "eval" / "conformal_coverage.csv").set_index("table")
    fig, (axc, axr) = plt.subplots(1, 2, figsize=(6.25, 3.0))
    # (a) cobertura
    tables = ["FAD", "DFF"]
    groups = [
        ("baseline_coverage", "Base", GRAY),
        ("split_coverage", "Split-conf.", GOLD),
        ("aci_coverage", "ACI", BLUE),
    ]
    xb = np.arange(len(tables))
    w = 0.26
    for j, (kcol, lab, c) in enumerate(groups):
        axc.bar(xb + (j - 1) * w, cov.loc[tables, kcol], w, label=lab, color=c, edgecolor=BLACK, linewidth=0.4)
    axc.axhline(0.95, color=BLACK, ls="--", lw=1.1)
    axc.text(1.4, 0.955, "nominal 0.95", fontsize=7, va="bottom", ha="right")
    axc.set_xticks(xb)
    axc.set_xticklabels(tables)
    axc.set_ylim(0.6, 1.0)
    axc.set_ylabel("Cobertura empírica del IP 95 %", fontsize=8)
    axc.set_title("(a) Calibración conforme adaptativa", fontsize=9)
    axc.tick_params(labelsize=7.5)
    # leyenda al pie de la figura (fuera del área de barras, evita encimarse con DFF)
    # (b) CRPS por modelo (FAD): clásicos + el deep ganador (BiTCN, calculado desde sus cuantiles)
    crps["BiTCN"] = _bitcn_crps("FAD")
    keep = [m for m in ["BiTCN", "sarima", "arima", "deepar"] if m in crps.index]
    crps = crps[keep].sort_values()
    cols = [BLUE if m == "BiTCN" else GRAY for m in crps.index]
    axr.barh(np.arange(len(crps)), crps.values, color=cols, edgecolor=BLACK, linewidth=0.4)
    axr.set_yticks(np.arange(len(crps)))
    axr.set_yticklabels([_mlabel(k) for k in crps.index], fontsize=8)
    axr.set_xlabel("CRPS (días), menor es mejor", fontsize=8)
    axr.set_title("(b) Afilado de la distribución predictiva", fontsize=9)
    axr.tick_params(labelsize=7.5)
    for i, v in enumerate(crps.values):
        axr.text(v, i, f" {v:.1f}", va="center", fontsize=7.5)
    axr.set_xlim(0, float(crps.max()) * 1.2)  # margen para el rótulo del valor (antes chocaba con el marco)
    axc.set_axisbelow(True)
    axr.set_axisbelow(True)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.legend(
        *axc.get_legend_handles_labels(),
        loc="lower center",
        ncol=3,
        fontsize=7.5,
        frameon=False,
        bbox_to_anchor=(0.5, -0.01),
    )
    _emit(fig, "results_coverage_crps")
    print("F4 coverage+crps OK")


# ---------- F5: grid 5x5 de backtest (todas las categorías) ----------
AREAS = [
    ("mexico", "México"),
    ("india", "India"),
    ("china", "China"),
    ("philippines", "Filipinas"),
    ("all_chargeability", "Resto del mundo"),
]
CATS = ["F1", "F2A", "F2B", "F3", "F4"]
BASE = pd.Timestamp(BASE_EPOCH)  # AD3: single-source t0


def fig_backtest_grid(table: str) -> None:
    from vp_model import dataset, holdout_window
    from vp_model.config import HOLDOUT

    d = pd.read_csv(REP / "eval" / f"deep_pi_{table}.csv", parse_dates=["ds"])
    # G7 · puerta 2: la ventana del protocolo, y sólo si los pronósticos la cubren (antes, literales de otra añada)
    hold_start, hold_end = holdout_window.window_of(d)
    win_start = hold_start - pd.DateOffset(months=HOLDOUT)  # tantos meses de contexto como de hold-out
    fig, axes = plt.subplots(5, 5, figsize=(6.25, 7.6), sharex=True)  # ancho de texto: letra a tamaño real
    for r, (acode, aname) in enumerate(AREAS):
        for c, cat in enumerate(CATS):
            ax = axes[r, c]
            ax.axvspan(hold_start, hold_end, color=STRIPE, zorder=0)  # lapso de hold-out marcado
            try:
                full = dataset.load_series(acode, cat, table).astype("float64")
            except KeyError:
                ax.text(0.5, 0.5, "sin datos", ha="center", va="center", fontsize=7, color=GRAY, transform=ax.transAxes)
                full = None
            if full is not None:
                fw = full[full.index >= win_start]
                ax.plot(fw.index, _to_year(fw.to_numpy()), color=BLACK, lw=0.9, zorder=2)
                g = d[d.unique_id == f"{acode}/family/{cat}"].sort_values("ds")
                g = g[g["ds"].isin(full.index)]  # solo donde la fecha real es específica (F)
                if len(g):
                    ax.fill_between(
                        g["ds"],
                        _to_year(g["BiTCN-lo-95"]),
                        _to_year(g["BiTCN-hi-95"]),
                        color=BLUE,
                        alpha=0.18,
                        zorder=1,
                    )
                    ax.plot(g["ds"], _to_year(g["BiTCN"]), color=BLUE, lw=1.1, ls="--", zorder=3)
            lo, hi = ax.get_ylim()  # series casi planas (fecha estancada): evita zoom al ruido sub-día
            if hi - lo < 0.5:
                mid = (lo + hi) / 2
                ax.set_ylim(mid - 0.5, mid + 0.5)
            ax.tick_params(labelsize=6.5, length=2, pad=1.5)
            ax.margins(x=0.02)
            ax.xaxis.set_major_locator(mdates.YearLocator(base=2))  # tres años por panel
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            # años decimales (2001.4) -> 'may 2001': la fecha de prioridad se lee como fecha
            _year_ticks(ax, 4)
            if r == 0:
                ax.set_title(cat, fontsize=9, color=BLUE, fontweight="bold", pad=3)
            if c == 0:
                ax.set_ylabel(aname, fontsize=8, color=GRAY)
            for sp in ("top", "right"):
                ax.spines[sp].set_visible(False)
    # leyenda compartida + nota
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    handles = [
        Line2D([0], [0], color=BLACK, lw=1.2, label="Fecha real (estado F)"),
        Line2D([0], [0], color=BLUE, lw=1.2, ls="--", label="Pronóstico (BiTCN)"),
        Patch(facecolor=BLUE, alpha=0.18, label="Intervalo de predicción 95 %"),
        Patch(facecolor=STRIPE, label=f"Conjunto reservado ({HOLDOUT} meses)"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=2, fontsize=7.5, frameon=False, bbox_to_anchor=(0.5, -0.005))
    fig.supylabel("Fecha de prioridad", fontsize=8.5)
    fig.suptitle(f"Backtest del pronóstico sobre el conjunto reservado · tabla {table}", fontsize=10, y=0.997)
    fig.tight_layout(rect=(0.01, 0.045, 1, 0.99), h_pad=0.6, w_pad=0.4)
    _emit(fig, f"results_backtest_grid_{table}")
    print(f"F5 grid {table} OK")


# ---------- Tabla pronóstico vs. real (diferencias en días) ----------
def forecast_vs_actual_rows(table: str) -> pd.DataFrame:
    """Por serie: último mes F del hold-out (fecha real vs pronosticada, Δ días) + MAE en días."""
    from vp_model import dataset

    d = pd.read_csv(REP / "eval" / f"deep_pi_{table}.csv", parse_dates=["ds"])
    rows = []
    for acode, aname in AREAS:
        for cat in CATS:
            try:
                full = dataset.load_series(acode, cat, table).astype("float64")
            except KeyError:
                continue
            g = d[d.unique_id == f"{acode}/family/{cat}"].sort_values("ds")
            g = g[g["ds"].isin(full.index)]
            if g.empty:
                continue
            last = g.iloc[-1]
            real_d = BASE + pd.Timedelta(days=float(last["y"]))
            pred_d = BASE + pd.Timedelta(days=float(last["BiTCN"]))
            mae_days = float(np.mean(np.abs(g["y"].to_numpy() - g["BiTCN"].to_numpy())))
            rows.append(
                {
                    "area": aname,
                    "cat": cat,
                    "mes": last["ds"].strftime("%b-%Y"),
                    "real": real_d.strftime("%d-%b-%Y"),
                    "pred": pred_d.strftime("%d-%b-%Y"),
                    "delta_dias": round(float(last["BiTCN"] - last["y"])),
                    "mae_dias": round(mae_days),
                }
            )
    return pd.DataFrame(rows)


# ---------- F6: diagrama de Diferencia Crítica (Friedman-Nemenyi) ----------
def _avg_ranks(table: str):
    """Rangos promedio por modelo (1=mejor) + CD de Nemenyi, leídos de significance_summary.json.

    El diagrama dibuja el MISMO cálculo que reporta el texto: la autoridad deduplica las
    pseudo-réplicas del corte mundial (DFF: 14 series distintas). Recalcular aquí sobre las 25
    series crudas dibujaba otro ranking (naïve-1 4.24 en DFF) que ninguna tabla sostiene.
    """
    from scipy.stats import studentized_range

    ranking = json.loads((REP / "eval" / "significance_summary.json").read_text())["ranking"][table]
    avg = pd.Series(ranking["avg_rank"], dtype="float64").sort_values()
    k, n = int(ranking["n_models"]), int(ranking["n_series"])
    if len(avg) != k:
        raise ValueError(f"{table}: {len(avg)} rangos para n_models={k}")
    q = studentized_range.ppf(0.95, k, np.inf) / np.sqrt(2)  # valor crítico de Nemenyi
    cd = float(q * np.sqrt(k * (k + 1) / (6 * n)))
    return avg, cd, n, k, float(ranking["friedman_p"])


def _cd_cliques(ranks: np.ndarray, cd: float) -> list[tuple[float, float]]:
    """Grupos maximales de modelos cuyo rango cabe dentro de CD (sin diferencia significativa)."""
    k = len(ranks)
    cliques, i = [], 0
    while i < k:
        j = i
        while j + 1 < k and ranks[j + 1] - ranks[i] <= cd:
            j += 1
        if j > i:
            cliques.append((ranks[i], ranks[j]))
        i += 1
    return [
        c
        for idx, c in enumerate(cliques)
        if not any(c2[0] <= c[0] and c[1] <= c2[1] for k2, c2 in enumerate(cliques) if k2 != idx)
    ]


def _cd_panel(ax, avg, cd, title):
    models = list(avg.index)
    ranks = avg.to_numpy()
    k = len(models)
    lo, hi = 1, int(np.ceil(ranks.max()))
    half = (k + 1) // 2
    cliques = _cd_cliques(ranks, cd)
    # Las barras de grupo van en su PROPIA franja bajo el eje y los rótulos empiezan debajo de
    # ella (antes se encimaban unas con otras y con los conectores de los primeros modelos).
    step_c, step_r = 0.17, 0.40
    yaxis = 0.0
    first_row = yaxis - 0.30 - len(cliques) * step_c - 0.12
    lowest_y = first_row - (half - 1) * step_r
    ax.set_xlim(lo - 0.5, hi + 0.5)
    ax.set_ylim(lowest_y - 0.3, yaxis + 1.3)
    ax.axis("off")
    ax.plot([lo, hi], [yaxis, yaxis], color=BLACK, lw=1.0)  # eje de rangos
    for r in range(lo, hi + 1):
        ax.plot([r, r], [yaxis, yaxis + 0.10], color=BLACK, lw=1.0)
        ax.text(r, yaxis + 0.16, str(r), ha="center", va="bottom", fontsize=7)
    ax.text(lo - 0.5, yaxis + 0.95, title, ha="left", va="bottom", fontsize=8.5, color=BLUE, fontweight="bold")
    for i, (mdl, rk) in enumerate(zip(models, ranks, strict=True)):
        left = i < half
        row = (half - 1 - i) if left else (i - half)
        ytxt = first_row - row * step_r
        xtxt = lo - 0.4 if left else hi + 0.4
        best = i == 0
        col = BLUE if best else BLACK
        ax.plot([rk, rk], [yaxis, ytxt], color=col, lw=1.3 if best else 0.6)
        ax.plot([rk, xtxt], [ytxt, ytxt], color=col, lw=1.3 if best else 0.6)
        ax.text(
            xtxt + (-0.12 if left else 0.12),
            ytxt,
            f"{_mlabel(mdl)} ({rk:.2f})",
            ha="right" if left else "left",
            va="center",
            fontsize=7.5,
            color=col,
            fontweight="bold" if best else "normal",
        )
    ylev = yaxis - 0.25
    for a, b in cliques:
        ax.plot([a - 0.03, b + 0.03], [ylev, ylev], color=GOLD, lw=2.4, solid_capstyle="round", zorder=4)
        ylev -= step_c
    # barra de la Diferencia Crítica
    ax.plot([hi - cd, hi], [yaxis + 0.62, yaxis + 0.62], color=BLACK, lw=1.4)
    ax.plot([hi - cd, hi - cd], [yaxis + 0.57, yaxis + 0.67], color=BLACK, lw=1.0)
    ax.plot([hi, hi], [yaxis + 0.57, yaxis + 0.67], color=BLACK, lw=1.0)
    ax.text(hi - cd / 2, yaxis + 0.70, f"CD = {cd:.2f}", ha="center", va="bottom", fontsize=7)


def fig_cd_diagram() -> None:
    fig, axes = plt.subplots(2, 1, figsize=(5.9, 7.2))  # 0.92 del ancho de texto
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        avg, cd, n, k, pval = _avg_ranks(table)
        ptxt = "p < 0.001" if pval < 0.001 else f"p = {pval:.3f}"
        _cd_panel(
            ax, avg, cd, f"({'a' if table == 'FAD' else 'b'}) {table} · {k} modelos, {n} series · Friedman {ptxt}"
        )
    fig.tight_layout(h_pad=1.0)
    _emit(fig, "results_cd_diagram")
    print("F6 CD-diagram OK")


# ---------- F7: heatmap de error por serie (área x categoría) ----------
def fig_error_heatmap() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(6.25, 2.8), sharey=True)
    anames = [a[1] for a in AREAS]
    mats = {}
    for table in ("FAD", "DFF"):
        d = pd.read_csv(REP / "prospective" / f"forecast_vs_actual_{table}.csv")
        d["area"] = pd.Categorical(d["area"], [a[1] for a in AREAS], ordered=True)
        mats[table] = d.pivot_table(index="area", columns="cat", values="mae_dias", observed=True).reindex(anames)[CATS]
    # UNA escala de color para los dos paneles (antes cada uno tenía la suya y el mismo tono
    # significaba cifras distintas) y una sola barra rotulada en días.
    vmax = float(max(np.nanmax(m.to_numpy()) for m in mats.values()))
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        m = mats[table]
        ax.grid(False)
        im = ax.imshow(m.to_numpy(), cmap=WARN, aspect="auto", vmin=0, vmax=vmax)
        ax.set_xticks(range(len(CATS)), CATS, fontsize=8)
        ax.set_yticks(range(len(anames)), anames, fontsize=8)
        ax.tick_params(length=0)
        for i in range(m.shape[0]):
            for j in range(m.shape[1]):
                v = m.to_numpy()[i, j]
                if np.isfinite(v):
                    ax.text(
                        j,
                        i,
                        f"{int(v)}",
                        ha="center",
                        va="center",
                        fontsize=7.5,
                        color="white" if v > 0.6 * vmax else BLACK,
                    )
        ax.set_title(f"({'a' if table == 'FAD' else 'b'}) {table}", fontsize=9, color=BLUE, fontweight="bold")
    fig.suptitle("Error absoluto medio del pronóstico por serie", fontsize=10, y=0.97)
    fig.tight_layout(rect=(0, 0, 0.9, 1))
    cax = fig.add_axes((0.915, axes[0].get_position().y0, 0.018, axes[0].get_position().height))
    cb = fig.colorbar(im, cax=cax)
    cb.set_label("días", fontsize=8)
    cb.ax.grid(False)
    cb.ax.spines["outline"].set_linewidth(0.5)
    cb.ax.tick_params(labelsize=7)
    _emit(fig, "results_error_heatmap")
    print("F7 heatmap OK")


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--crps-parent-root",
        type=Path,
        default=None,
        help="raíz (sólo lectura) de la madre de una complementaria baseline_crps: sin ella su CRPS no se acredita",
    )
    args = ap.parse_args(argv)
    _crps_referencia(args.crps_parent_root)  # antes de dibujar NADA: F1–F3 no quedan a medias
    with results_style():
        fig_ranking()
        fig_forecast()
        fig_multiseed()
        fig_coverage_crps(args.crps_parent_root)
        fig_backtest_grid("FAD")
        fig_backtest_grid("DFF")
        fig_cd_diagram()
        for t in ("FAD", "DFF"):
            forecast_vs_actual_rows(t).to_csv(REP / "prospective" / f"forecast_vs_actual_{t}.csv", index=False)
            print(f"tabla {t} -> reports/prospective/forecast_vs_actual_{t}.csv")
        fig_error_heatmap()  # DESPUÉS de regenerar los CSV que lee (antes iba un run desfasado)
    print("Figuras en", FIG)


if __name__ == "__main__":
    main()
