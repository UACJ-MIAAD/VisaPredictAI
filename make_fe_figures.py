"""Suite de Feature Engineering de calidad publicación (PI-I §1.2.2).

Figuras que un FE serio exige (al estilo EpiForecast: transformaciones + codificaciones +
importancia de características):
  fe_differencing.pdf   serie en niveles vs. primera diferencia (por qué diferenciar)
  fe_calendar.pdf       codificación cíclica del año fiscal (seno/coseno) + círculo
  fe_importance.pdf     importancia de características de un árbol (LightGBM) sobre Δy

Salida en reports/latex/Figures/. Corre en `ante`:  ante/bin/python make_fe_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import seaborn as sns  # noqa: E402

ROOT = Path(__file__).resolve().parent
FIG = ROOT / "reports" / "latex" / "Figures"
from vp_model.palette import BLUE, GOLD, GRAY, GRID, MID  # noqa: E402

sns.set_theme(style="whitegrid", font="serif", rc={"axes.edgecolor": MID, "grid.color": GRID})
plt.rcParams.update({"savefig.bbox": "tight", "savefig.dpi": 300, "font.size": 9})


def _series(country="mexico", category="F3", table="FAD"):
    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv", parse_dates=["bulletin_date"])
    s = d[(d.status == "F") & (d.country == country) & (d.category == category) & (d.table == table)]
    return s.sort_values("bulletin_date")


def fig_differencing() -> None:
    s = _series("all_chargeability", "F2A").copy()
    s["years"] = 1975 + s.days_since_base / 365.25
    s["delta"] = s.days_since_base.diff()
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.2, 4.4), sharex=True)
    a1.plot(s.bulletin_date, s.years, color=BLUE, lw=1.4)
    a1.set_ylabel("Fecha de prioridad (año)")
    a1.set_title("(a) Serie en niveles: tendencia fuerte (no estacionaria)", fontsize=9.5, color=BLUE)
    a2.fill_between(s.bulletin_date, 0, s.delta.clip(-120, 260), color=GOLD, alpha=0.9, step="mid")
    a2.axhline(0, color=GRAY, lw=0.8)
    a2.set_ylim(-130, 270)
    a2.set_ylabel("Avance mensual (días)")
    a2.set_title("(b) Primera diferencia: estacionaria y extrapolable", fontsize=9.5, color=BLUE)
    a2.tick_params(axis="x", labelrotation=20)
    fig.suptitle("Diferenciación: All Chargeability / F2A / FAD", fontsize=10, color=BLUE, y=0.99)
    fig.tight_layout()
    fig.savefig(FIG / "fe_differencing.pdf")
    plt.close(fig)
    print("fe_differencing OK")


def fig_calendar() -> None:
    """Codificación cíclica del mes en el año fiscal (inicia en octubre)."""
    months = ["Oct", "Nov", "Dic", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep"]
    k = np.arange(12)
    ang = 2 * np.pi * k / 12
    sin, cos = np.sin(ang), np.cos(ang)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.4, 3.4))
    xx = np.linspace(0, 12, 300)
    a1.plot(xx, np.sin(2 * np.pi * xx / 12), color=BLUE, lw=1.6, label=r"$\sin(2\pi m/12)$")
    a1.plot(xx, np.cos(2 * np.pi * xx / 12), color=GOLD, lw=1.6, label=r"$\cos(2\pi m/12)$")
    a1.scatter(k, sin, color=BLUE, s=22, zorder=5)
    a1.scatter(k, cos, color=GOLD, s=22, zorder=5)
    a1.set_xticks(k, months, fontsize=7)
    a1.set_xlabel("Mes del año fiscal")
    a1.set_ylabel("Valor de la codificación")
    a1.set_title("(a) Componentes seno/coseno", fontsize=9.5, color=BLUE)
    # centro-superior (Abr–Jun): ambas curvas están en la mitad baja ahí, zona libre
    a1.set_ylim(-1.12, 1.18)
    a1.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.6, 1.0), framealpha=0.92)
    # círculo: meses equiespaciados; Dic y Ene quedan adyacentes
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
    a2.set_title("(b) Meses sobre el círculo unitario", fontsize=9.5, color=BLUE)
    fig.suptitle("Codificación cíclica del calendario fiscal", fontsize=10, color=BLUE, y=1.02)
    fig.tight_layout()
    fig.savefig(FIG / "fe_calendar.pdf")
    plt.close(fig)
    print("fe_calendar OK")


def fig_importance() -> None:
    """Importancia de características de un LightGBM entrenado sobre Δy (lags + calendario)."""
    import lightgbm as lgb

    d = pd.read_csv(ROOT / "data" / "processed" / "visa_panel_long.csv", parse_dates=["bulletin_date"])
    f = d[(d.status == "F") & (d.block == "family") & (d.table == "FAD")].sort_values("bulletin_date").copy()
    f["delta"] = f.groupby(["country", "category"])["days_since_base"].diff()
    fiscal = (f.bulletin_date.dt.month - 10) % 12
    f["mes_sin"] = np.sin(2 * np.pi * fiscal / 12)
    f["mes_cos"] = np.cos(2 * np.pi * fiscal / 12)
    lags = [1, 2, 3, 6, 12]
    for lg in lags:
        f[f"rezago_{lg}"] = f.groupby(["country", "category"])["delta"].shift(lg)
    feats = [f"rezago_{lg}" for lg in lags] + ["mes_sin", "mes_cos"]
    data = f.dropna(subset=[*feats, "delta"])
    model = lgb.LGBMRegressor(n_estimators=300, max_depth=4, learning_rate=0.05, verbose=-1)
    model.fit(data[feats], data["delta"])
    imp = pd.Series(model.feature_importances_, index=feats).sort_values()
    nice = {f"rezago_{lg}": f"Avance en $t-{lg}$" for lg in lags}
    nice |= {"mes_sin": "Calendario (seno)", "mes_cos": "Calendario (coseno)"}
    fig, ax = plt.subplots(figsize=(7.0, 3.4))
    cols = [GOLD if "Calendario" in nice[k] else BLUE for k in imp.index]
    ax.barh([nice[k] for k in imp.index], imp.to_numpy(), color=cols, edgecolor="white")
    ax.set_xlabel("Importancia (ganancia, LightGBM)")
    ax.set_title("Importancia de características para el avance mensual (FAD)", fontsize=10, color=BLUE)
    fig.tight_layout()
    fig.savefig(FIG / "fe_importance.pdf")
    plt.close(fig)
    print("fe_importance OK")


if __name__ == "__main__":
    fig_differencing()
    fig_calendar()
    fig_importance()
    print("Suite FE en", FIG)
