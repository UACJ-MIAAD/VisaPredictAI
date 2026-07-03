"""Figuras del paper MICAI — generadas desde el pipeline real (cero placeholders).

Salida: reports/paper_micai/Figures/*.pdf (vectorial, 300 dpi, estilo LNCS B/N-safe:
estilos de línea + marcadores distintos, no solo color, para que impriman en gris).

Fuentes:
  • reports/prospective/forecast_scorecard_meta.json  — evaluación prospectiva por horizonte/tabla
  • reports/prospective/web_forecasts.csv             — pronósticos congelados (fan-chart)
  • data/processed/visa_panel_long.csv    — histórico real (fan-chart)

Uso (desde la raíz):  ante/bin/python reports/paper_micai/make_paper_figures.py   (o `make paper-figures`)
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from vp_model.palette import BLUE, GRID, INK, MID, WINE

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports"
FIGS = Path(__file__).resolve().parent / "Figures"
FIGS.mkdir(exist_ok=True)
DAYS_Y = 365.25
BASE_YEAR = 1975  # t0 = 1975-01-01

plt.rcParams.update(
    {
        "font.family": "serif",
        "font.size": 9,
        "axes.edgecolor": MID,
        "axes.linewidth": 0.8,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "savefig.dpi": 300,
        "savefig.bbox": "tight",
        "figure.dpi": 150,
    }
)


def _save(fig, name: str) -> None:
    fig.savefig(FIGS / f"{name}.pdf")
    plt.close(fig)
    print(f"  ✓ {name}.pdf")


def fig_prospective_horizon(meta: dict) -> None:
    """Error prospectivo (MAE días + MASE) vs horizonte — la figura de la contribución."""
    bh = meta["by_horizon"]
    h = sorted(int(k) for k in bh)
    mae = [bh[str(k)]["mae_days"] for k in h]
    mase = [bh[str(k)]["mase"] for k in h]
    fig, ax1 = plt.subplots(figsize=(3.3, 2.4))
    ax1.plot(h, mae, "-o", color=BLUE, ms=3.5, lw=1.4, label="MAE (days)")
    ax1.set_xlabel("Forecast horizon $h$ (months)")
    ax1.set_ylabel("MAE (days)", color=BLUE)
    ax1.tick_params(axis="y", labelcolor=BLUE)
    ax1.set_xticks(h)
    ax2 = ax1.twinx()
    ax2.grid(False)
    ax2.plot(h, mase, "--s", color=WINE, ms=3, lw=1.3, label="MASE")
    ax2.axhline(1.0, color=INK, ls=":", lw=1.0)
    ax2.text(1.2, 1.02, "MASE = 1 (seasonal naïve)", fontsize=6.5, color=INK, va="bottom")
    ax2.set_ylabel("MASE", color=WINE)
    ax2.tick_params(axis="y", labelcolor=WINE)
    ax2.set_ylim(0, max(1.05, max(mase) * 1.1))
    _save(fig, "fig_prospective_horizon")


def fig_calibration(meta: dict) -> None:
    """Cobertura empírica de las bandas 80%/95% vs horizonte vs nominal (honestidad)."""
    bh = meta["by_horizon"]
    h = sorted(int(k) for k in bh)
    c80 = [bh[str(k)]["cov80"] for k in h]
    c95 = [bh[str(k)]["cov95"] for k in h]
    fig, ax = plt.subplots(figsize=(3.3, 2.4))
    ax.plot(h, c95, "-o", color=BLUE, ms=3.5, lw=1.4, label="95% band (empirical)")
    ax.plot(h, c80, "--s", color=WINE, ms=3, lw=1.3, label="80% band (empirical)")
    ax.axhline(0.95, color=BLUE, ls=":", lw=0.9)
    ax.axhline(0.80, color=WINE, ls=":", lw=0.9)
    ax.set_xlabel("Forecast horizon $h$ (months)")
    ax.set_ylabel("Empirical coverage")
    ax.set_xticks(h)
    ax.set_ylim(0.5, 1.02)
    ax.legend(fontsize=6.8, loc="lower left", framealpha=0.9)
    _save(fig, "fig_calibration")


def fig_fanchart(country: str = "mexico", category: str = "F2A", table: str = "FAD") -> None:
    """Fan-chart de una serie piloto: histórico real + pronóstico congelado + bandas 80/95."""
    panel = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv")
    fc = pd.read_csv(REPORTS / "prospective" / "web_forecasts.csv")
    s = panel[
        (panel.country == country) & (panel.category == category) & (panel.table == table) & (panel.status == "F")
    ].copy()
    s = s.dropna(subset=["days_since_base"]).sort_values("bulletin_date")
    s["t"] = pd.to_datetime(s["bulletin_date"]).dt.year + (pd.to_datetime(s["bulletin_date"]).dt.month - 1) / 12
    s["yr"] = BASE_YEAR + s["days_since_base"] / DAYS_Y
    s = s[s["t"] >= s["t"].max() - 6]  # zoom a los últimos ~6 años
    f = fc[(fc.country == country) & (fc.category == category) & (fc.table == table)].copy()
    f["t"] = pd.to_datetime(f["date"]).dt.year + (pd.to_datetime(f["date"]).dt.month - 1) / 12
    for c in ("days", "lo80", "hi80", "lo95", "hi95"):
        f[c] = BASE_YEAR + f[c] / DAYS_Y

    fig, ax = plt.subplots(figsize=(3.3, 2.4))
    ax.fill_between(f["t"], f["lo95"], f["hi95"], color=BLUE, alpha=0.12, lw=0, label="95% band")
    ax.fill_between(f["t"], f["lo80"], f["hi80"], color=BLUE, alpha=0.24, lw=0, label="80% band")
    ax.plot(s["t"], s["yr"], "-", color=INK, lw=1.4, label="Observed")
    ax.plot(f["t"], f["days"], "--", color=WINE, lw=1.5, label="Forecast")
    split = s["t"].max()
    ax.axvline(split, color=MID, ls=":", lw=0.9)
    ax.set_xlabel("Bulletin date")
    ax.set_ylabel("Priority-date year")
    ax.legend(fontsize=6.5, loc="upper left", framealpha=0.9)
    ax.set_title(f"{country.title()} · {category} · {table}", fontsize=8)
    _save(fig, "fig_fanchart")


def main() -> None:
    meta = json.loads((REPORTS / "prospective" / "forecast_scorecard_meta.json").read_text())
    print(f"figuras -> {FIGS}")
    fig_prospective_horizon(meta)
    fig_calibration(meta)
    fig_fanchart()


if __name__ == "__main__":
    main()
