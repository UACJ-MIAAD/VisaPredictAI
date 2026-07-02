"""Genera las figuras de RESULTADOS del entregable LaTeX (PI-I), paleta UACJ + serif, PDF vectorial.

Las cuatro figuras que faltaban en el Cap. de Resultados (hoy todo en tablas):
  F1  ranking MASE de los 21 modelos (FAD + DFF)          -> results_ranking_mase.pdf
  F2  pronóstico del ganador profundo vs real + PI 95%    -> results_forecast_winner.pdf
  F3  intervalo de confianza multi-semilla deep vs listón -> results_multiseed_ci.pdf
  F4  cobertura del PI antes/después de ACI + CRPS        -> results_coverage_crps.pdf

Lee solo CSVs ya versionados en reports/. Corre en `ante` (vp_model + matplotlib):
    ante/bin/python make_result_figures.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

ROOT = Path(__file__).resolve().parent
REP = ROOT / "reports"
FIG = REP / "latex" / "Figures"
from vp_model.palette import BLUE, GOLD, GRAY, GRID, INK, MID, MUTE, STRIPE, WARN  # noqa: E402

BLACK = INK  # alias: el cuerpo usa BLACK para el texto/línea dominante

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


# D5 (regla #0): listones desde la fuente única de verdad, no hardcodeados.
# Semántica = caption del .tex: "listón parsimonioso (ETS/Theta en FAD, ETS en DFF)".
_KF = json.loads((REP / "key_facts.json").read_text())


def _liston(table: str) -> float:
    return float(_KF["fad_champion_mase"] if table == "FAD" else _KF["ets_dff_mean"])


# ---------- F1: ranking MASE de los 21 modelos ----------
def fig_ranking() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 4.2))
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        df = pd.read_csv(REP / f"model_comparison_{table}21.csv")
        m = df.groupby("model")["hold_mase"].mean().sort_values(ascending=False)
        m = m[m <= 0.40]  # recorta off-scale (TFT 2.6, LSTM/DeepAR ~0.4+); se anota aparte
        n_off = df.groupby("model")["hold_mase"].mean().gt(0.40).sum()
        colors = [BLUE if mm in ("ets", "theta") else GRAY for mm in m.index]
        y = np.arange(len(m))
        ax.hlines(y, _liston(table), m.values, color=MUTE, lw=1.2, zorder=1)
        ax.scatter(m.values, y, color=colors, s=34, zorder=3, edgecolor=BLACK, linewidth=0.4)
        ax.axvline(_liston(table), color=GOLD, ls="--", lw=1.3, zorder=2)
        ax.set_yticks(y)
        ax.set_yticklabels(m.index, fontsize=7.5)
        ax.set_xlabel("MASE de hold-out")
        ax.set_title(f"({'a' if table == 'FAD' else 'b'}) {table} — 25 series familiares")
        ax.text(
            _liston(table),
            len(m) - 0.3,
            f" listón {_liston(table):.3f}",
            color=GOLD,
            fontsize=7.5,
            va="top",
        )
        if n_off:
            ax.text(
                0.97,
                0.03,
                f"{n_off} modelos fuera de escala (>0.40)",
                transform=ax.transAxes,
                ha="right",
                fontsize=6.5,
                color=GRAY,
                style="italic",
            )
    fig.tight_layout()
    fig.savefig(FIG / "results_ranking_mase.pdf")
    plt.close(fig)
    print("F1 ranking OK")


# ---------- F2: pronóstico del ganador vs real + banda PI 95% ----------
def _to_year(days: np.ndarray) -> np.ndarray:
    return 1975 + np.asarray(days, dtype="float64") / 365.25


def fig_forecast() -> None:
    picks = {"DFF": ("BiTCN", None), "FAD": ("BiTCN", None)}
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.6))
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        col = picks[table][0]
        d = pd.read_csv(REP / f"deep_pi_{table}.csv", parse_dates=["ds"])
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
            label="PI 95%",
        )
        ax.plot(x, _to_year(g["y"]), color=BLACK, lw=1.6, label="Real (F)")
        ax.plot(x, _to_year(g[col]), color=BLUE, lw=1.4, ls="--", label=f"{col} (pronóstico)")
        country, _b, cat = uid.split("/")
        ax.set_title(f"({'a' if table == 'FAD' else 'b'}) {table} — {country}/{cat}")
        ax.set_ylabel("Fecha de prioridad (año)")
        ax.tick_params(axis="x", labelrotation=30, labelsize=7)
        ax.legend(fontsize=7, loc="best", framealpha=0.9)
    fig.tight_layout()
    fig.savefig(FIG / "results_forecast_winner.pdf")
    plt.close(fig)
    print("F2 forecast OK")


# ---------- F3: IC multi-semilla deep vs listón ----------
def _seed_mases(pattern: str, col: str, table: str) -> list[float]:
    from vp_model import dataset
    from vp_model.metrics import naive_scale_before

    vals = []
    for p in sorted(REP.glob(pattern)):
        df = pd.read_csv(p, parse_dates=["ds"])
        if col not in df.columns:
            continue
        ms = []
        for uid, g in df.groupby("unique_id"):
            country, _b, category = uid.split("/")
            try:
                full = dataset.load_series(country, category, table).astype("float64")
            except KeyError:
                continue
            g = g.sort_values("ds")
            g = g[g["ds"].isin(full.index)]
            if len(g) < 2:
                continue
            scale = naive_scale_before(full, g["ds"].min())
            y = full.reindex(g["ds"]).to_numpy()
            ms.append(float(np.mean(np.abs(y - g[col].to_numpy()))) / scale)
        if ms:
            vals.append(float(np.mean(ms)))
    return vals


def fig_multiseed() -> None:
    from scipy import stats

    specs = [
        # oráculo 0.113 = corrida de selección perfecta (aggregate_seeds); no vive en key_facts
        ("FAD", "global_FAD_camp_auto_s*.csv", "AutoBiTCN", _liston("FAD"), 0.113),
        ("DFF", "global_DFF_camp_diff_s*.csv", "BiTCN", _liston("DFF"), None),
    ]
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ypos, labels = [], []
    for i, (table, pat, col, liston, oracle) in enumerate(specs):
        vals = np.array(_seed_mases(pat, col, table))
        m, sd, n = vals.mean(), vals.std(ddof=1), len(vals)
        half = stats.t.ppf(0.975, n - 1) * sd / np.sqrt(n)
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
            label="Listón parsimonioso (ETS/Theta)" if i == 0 else None,
        )
        if oracle:
            ax.scatter(
                [oracle], [y], marker="s", color=GRAY, s=40, zorder=4, label="Oráculo de selección" if i == 0 else None
            )
        ypos.append(y)
        labels.append(f"{table}\n{col}")
        ax.text(m, y + 0.16, f"{m:.3f}", color=BLUE, ha="center", fontsize=7.5)
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels)
    ax.set_ylim(-0.6, len(specs) - 0.2)
    ax.set_xlabel("MASE de hold-out")
    ax.set_title("Validación multi-semilla: aprendizaje profundo global vs. listón")
    ax.legend(fontsize=7.5, loc="lower right", framealpha=0.95)
    fig.tight_layout()
    fig.savefig(FIG / "results_multiseed_ci.pdf")
    plt.close(fig)
    print("F3 multiseed OK")


# ---------- F4: cobertura PI (antes/después ACI) + CRPS ----------
def _pinball(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    d = y - q
    return float(np.mean(np.maximum(tau * d, (tau - 1) * d)))


def _bitcn_crps(table: str) -> float:
    """CRPS de BiTCN (2·media pinball sobre cuantiles), idéntico a experiments/eval_deep_pi.py, F-only por serie."""
    from vp_model import dataset

    d = pd.read_csv(REP / f"deep_pi_{table}.csv", parse_dates=["ds"])
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


def fig_coverage_crps() -> None:
    cov = pd.read_csv(REP / "conformal_coverage.csv").set_index("table")
    fig, (axc, axr) = plt.subplots(1, 2, figsize=(7.4, 3.5))
    # (a) cobertura
    tables = ["FAD", "DFF"]
    groups = [
        ("baseline_coverage", "Base (nf)", GRAY),
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
    axc.set_ylabel("Cobertura empírica del PI 95%")
    axc.set_title("(a) Calibración conforme adaptativa")
    # leyenda al pie de la figura (fuera del área de barras, evita encimarse con DFF)
    # (b) CRPS por modelo (FAD): clásicos + el deep ganador (BiTCN, calculado desde sus cuantiles)
    crps = pd.read_csv(REP / "crps_fad.csv").groupby("model")["crps"].mean()
    crps["BiTCN"] = _bitcn_crps("FAD")
    keep = [m for m in ["BiTCN", "sarima", "arima", "deepar"] if m in crps.index]
    crps = crps[keep].sort_values()
    cols = [BLUE if m == "BiTCN" else GRAY for m in crps.index]
    axr.barh(np.arange(len(crps)), crps.values, color=cols, edgecolor=BLACK, linewidth=0.4)
    axr.set_yticks(np.arange(len(crps)))
    axr.set_yticklabels(crps.index, fontsize=8)
    axr.set_xlabel("CRPS (días) — menor es mejor")
    axr.set_title("(b) Afilado de la distribución predictiva")
    for i, v in enumerate(crps.values):
        axr.text(v, i, f" {v:.1f}", va="center", fontsize=7.5)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.legend(
        *axc.get_legend_handles_labels(),
        loc="lower center",
        ncol=3,
        fontsize=8,
        frameon=False,
        bbox_to_anchor=(0.5, -0.01),
    )
    fig.savefig(FIG / "results_coverage_crps.pdf")
    plt.close(fig)
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
HOLD_START = pd.Timestamp("2024-08-01")
HOLD_END = pd.Timestamp("2026-07-01")
WIN_START = pd.Timestamp("2022-08-01")  # 48 meses de ventana: 24 de contexto + 24 de hold-out
BASE = pd.Timestamp("1975-01-01")


def fig_backtest_grid(table: str) -> None:
    from vp_model import dataset

    d = pd.read_csv(REP / f"deep_pi_{table}.csv", parse_dates=["ds"])
    fig, axes = plt.subplots(5, 5, figsize=(9.6, 10.4), sharex=True)
    for r, (acode, aname) in enumerate(AREAS):
        for c, cat in enumerate(CATS):
            ax = axes[r, c]
            ax.axvspan(HOLD_START, HOLD_END, color=STRIPE, zorder=0)  # lapso de hold-out marcado
            try:
                full = dataset.load_series(acode, cat, table).astype("float64")
            except KeyError:
                ax.text(0.5, 0.5, "sin datos", ha="center", va="center", fontsize=6, color=GRAY, transform=ax.transAxes)
                full = None
            if full is not None:
                fw = full[full.index >= WIN_START]
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
            ax.tick_params(labelsize=6, length=2)
            ax.margins(x=0.02)
            ax.xaxis.set_major_locator(mdates.YearLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            ax.yaxis.set_major_locator(MaxNLocator(4))
            ax.ticklabel_format(axis="y", useOffset=False, style="plain")
            if r == 0:
                ax.set_title(cat, fontsize=9, color=BLUE, fontweight="bold", pad=4)
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
        Patch(facecolor=BLUE, alpha=0.18, label="Intervalo de predicción 95%"),
        Patch(facecolor=STRIPE, label="Lapso de hold-out (24 meses)"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=4, fontsize=8, frameon=False, bbox_to_anchor=(0.5, -0.005))
    fig.supylabel("Fecha de prioridad (año)", fontsize=9)
    fig.suptitle(f"Backtest del pronóstico sobre el hold-out — tabla {table}", fontsize=11, y=0.997)
    fig.tight_layout(rect=(0.01, 0.03, 1, 0.99))
    fig.savefig(FIG / f"results_backtest_grid_{table}.pdf")
    plt.close(fig)
    print(f"F5 grid {table} OK")


# ---------- Tabla pronóstico vs. real (diferencias en días) ----------
def forecast_vs_actual_rows(table: str) -> pd.DataFrame:
    """Por serie: último mes F del hold-out (fecha real vs pronosticada, Δ días) + MAE en días."""
    from vp_model import dataset

    d = pd.read_csv(REP / f"deep_pi_{table}.csv", parse_dates=["ds"])
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
    """Rangos promedio por modelo (1=mejor) sobre las series con cobertura completa + CD de Nemenyi."""
    from scipy.stats import friedmanchisquare, studentized_range

    df = pd.read_csv(REP / f"model_comparison_{table}21.csv")
    piv = df.pivot_table(index=["country", "category"], columns="model", values="hold_mase")
    piv = piv.dropna(axis=1)  # solo modelos presentes en TODAS las series (bloques completos)
    ranks = piv.rank(axis=1, ascending=True)  # 1 = menor MASE = mejor
    avg = ranks.mean().sort_values()
    k, n = piv.shape[1], piv.shape[0]
    q = studentized_range.ppf(0.95, k, np.inf) / np.sqrt(2)  # valor crítico de Nemenyi
    cd = float(q * np.sqrt(k * (k + 1) / (6 * n)))
    pval = friedmanchisquare(*[piv[c].to_numpy() for c in piv.columns]).pvalue
    return avg, cd, n, k, pval


def _cd_panel(ax, avg, cd, title):
    models = list(avg.index)
    ranks = avg.to_numpy()
    k = len(models)
    lo, hi = 1, int(np.ceil(ranks.max()))
    half = (k + 1) // 2
    yaxis = k / 2 + 1.7
    lowest_y = yaxis - 0.5 - (half - 1) * 0.42
    ax.set_xlim(lo - 0.5, hi + 0.5)
    ax.set_ylim(lowest_y - 0.45, yaxis + 1.35)
    ax.axis("off")
    ax.plot([lo, hi], [yaxis, yaxis], color=BLACK, lw=1.0)  # eje de rangos
    for r in range(lo, hi + 1):
        ax.plot([r, r], [yaxis, yaxis + 0.12], color=BLACK, lw=1.0)
        ax.text(r, yaxis + 0.20, str(r), ha="center", va="bottom", fontsize=7)
    ax.text((lo + hi) / 2, yaxis + 0.62, title, ha="center", fontsize=9, color=BLUE, fontweight="bold")
    half = (k + 1) // 2
    for i, (mdl, rk) in enumerate(zip(models, ranks, strict=True)):
        left = i < half
        row = (half - 1 - i) if left else (i - half)
        ytxt = yaxis - 0.5 - row * 0.42
        xtxt = lo - 0.4 if left else hi + 0.4
        best = i == 0
        col = BLUE if best else BLACK
        ax.plot([rk, rk], [yaxis, ytxt], color=col, lw=1.4 if best else 0.8)
        ax.plot([rk, xtxt], [ytxt, ytxt], color=col, lw=1.4 if best else 0.8)
        ax.text(
            xtxt + (-0.12 if left else 0.12),
            ytxt,
            f"{mdl} ({rk:.2f})",
            ha="right" if left else "left",
            va="center",
            fontsize=7.2,
            color=col,
            fontweight="bold" if best else "normal",
        )
    # barras de cliques (modelos sin diferencia significativa: rango dentro de CD)
    cliques, i = [], 0
    while i < k:
        j = i
        while j + 1 < k and ranks[j + 1] - ranks[i] <= cd:
            j += 1
        if j > i:
            cliques.append((ranks[i], ranks[j]))
        i += 1
    cliques = [
        c
        for idx, c in enumerate(cliques)
        if not any(c2[0] <= c[0] and c[1] <= c2[1] for k2, c2 in enumerate(cliques) if k2 != idx)
    ]
    ylev = yaxis - 0.22
    for a, b in cliques:
        ax.plot([a - 0.03, b + 0.03], [ylev, ylev], color=GOLD, lw=3.2, solid_capstyle="round")
        ylev -= 0.12
    # barra de la Diferencia Crítica
    ax.plot([lo, lo + cd], [yaxis + 0.95, yaxis + 0.95], color=BLACK, lw=1.6)
    ax.plot([lo, lo], [yaxis + 0.90, yaxis + 1.0], color=BLACK, lw=1.0)
    ax.plot([lo + cd, lo + cd], [yaxis + 0.90, yaxis + 1.0], color=BLACK, lw=1.0)
    ax.text(lo + cd / 2, yaxis + 1.02, f"CD = {cd:.2f}", ha="center", va="bottom", fontsize=7)


def fig_cd_diagram() -> None:
    fig, axes = plt.subplots(2, 1, figsize=(7.4, 6.6))
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        avg, cd, n, k, pval = _avg_ranks(table)
        ptxt = "p < 0.001" if pval < 0.001 else f"p = {pval:.3f}"
        _cd_panel(
            ax, avg, cd, f"({'a' if table == 'FAD' else 'b'}) {table} — {k} modelos, {n} series · Friedman {ptxt}"
        )
    fig.tight_layout(h_pad=1.2)
    fig.savefig(FIG / "results_cd_diagram.pdf")
    plt.close(fig)
    print("F6 CD-diagram OK")


# ---------- F7: heatmap de error por serie (área x categoría) ----------
def fig_error_heatmap() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.6, 3.8))
    anames = [a[1] for a in AREAS]
    for ax, table in zip(axes, ("FAD", "DFF"), strict=True):
        d = pd.read_csv(REP / f"forecast_vs_actual_{table}.csv")
        d["area"] = pd.Categorical(d["area"], [a[1] for a in AREAS], ordered=True)
        m = d.pivot_table(index="area", columns="cat", values="mae_dias", observed=True).reindex(anames)[CATS]
        ax.grid(False)
        im = ax.imshow(m.to_numpy(), cmap=WARN, aspect="auto", vmin=0, vmax=float(np.nanmax(m.to_numpy())))
        ax.set_xticks(range(len(CATS)), CATS, fontsize=8)
        ax.set_yticks(range(len(anames)), anames, fontsize=8)
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
                        color="white" if v > 0.6 * np.nanmax(m.to_numpy()) else BLACK,
                    )
        ax.set_title(f"({'a' if table == 'FAD' else 'b'}) {table}", fontsize=9, color=BLUE, fontweight="bold")
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cb.ax.tick_params(labelsize=6)
    fig.suptitle("Error absoluto medio del pronóstico por serie (días)", fontsize=10, y=1.02)
    fig.tight_layout()
    fig.savefig(FIG / "results_error_heatmap.pdf")
    plt.close(fig)
    print("F7 heatmap OK")


if __name__ == "__main__":
    fig_ranking()
    fig_forecast()
    fig_multiseed()
    fig_coverage_crps()
    fig_backtest_grid("FAD")
    fig_backtest_grid("DFF")
    fig_cd_diagram()
    for t in ("FAD", "DFF"):
        forecast_vs_actual_rows(t).to_csv(REP / f"forecast_vs_actual_{t}.csv", index=False)
        print(f"tabla {t} -> reports/forecast_vs_actual_{t}.csv")
    fig_error_heatmap()  # DESPUÉS de regenerar los CSV que lee (antes iba un run desfasado)
    print("Figuras en", FIG)
