"""Monitor de drift de ML (distinto del guardián de consistencia, que vigila CIFRAS).

Vigila tres derivas y emite ``reports/drift_report.json`` + un resumen legible. NO es un gate
(no rompe el build): es observabilidad. En la Action del boletín, si marca drift, el correo
SES lo destaca para que un humano lo revise.

1. DESEMPEÑO — MASE del último vintage calificado del ledger prospectivo vs el baseline de los
   vintages previos. Un salto = el modelo desplegado se está degradando en el mundo real.
2. COBERTURA — cov95 del último vintage vs el nominal 0.95. Caída = intervalos mal calibrados.
3. DATOS — el último boletín introduce un movimiento (avance/retrogresión) sin precedente para
   una serie (|delta| > K·MAD histórica). El modelo deberá tolerarlo; conviene saberlo.

    ante/bin/python experiments/check_drift.py            (o `make drift`)
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
PANEL = ROOT / "data" / "processed" / "visa_panel_long.csv"

# Umbrales (el objetivo es señalar un cambio SISTÉMICO, no alarmar por movimientos rutinarios).
# DATA_K subió de 5 a 8 y la ALARMA de datos ahora se gatea por conteo: avances de 150+ días en
# una F1 son normales, así que 1-5 series marcadas NO disparan "drift" (solo se LISTAN); la
# bandera se enciende si >=DATA_MIN_FLAGGED series se mueven sin precedente a la vez (evento
# sistémico) o si hay drift de desempeño/cobertura. Ajustado tras el audit (fatiga de alertas).
PERF_RATIO = 1.5  # MASE del último vintage > 1.5× el baseline -> drift de desempeño
COV95_FLOOR = 0.85  # cov95 del último vintage < 0.85 -> drift de cobertura
DATA_K = 8.0  # |delta del último mes| > 8× MAD histórica -> movimiento sin precedente
DATA_MIN_FLAGGED = 8  # nº de series con movimiento sin precedente para considerarlo SISTÉMICO


def _performance_and_coverage() -> dict:
    sc = REPORTS / "forecast_scorecard.csv"
    if not sc.exists():
        return {"status": "sin_ledger"}
    df = pd.read_csv(sc)
    by = df.groupby("origin").agg(n=("scaled_err", "size"), mase=("scaled_err", "mean"), cov95=("in95", "mean"))
    scored = by[by.n > 0].sort_index()
    if len(scored) < 2:
        return {"status": "insuficiente", "n_vintages": int(len(scored))}
    latest = scored.iloc[-1]
    baseline = scored.iloc[:-1].mase.mean()
    perf_ratio = float(latest.mase / baseline) if baseline else float("nan")
    return {
        "status": "ok",
        "latest_vintage": scored.index[-1],
        "latest_mase": round(float(latest.mase), 4),
        "baseline_mase": round(float(baseline), 4),
        "perf_ratio": round(perf_ratio, 3),
        "latest_cov95": round(float(latest.cov95), 3),
        "performance_drift": bool(perf_ratio > PERF_RATIO),
        "coverage_drift": bool(latest.cov95 < COV95_FLOOR),
    }


def _data_drift() -> dict:
    if not PANEL.exists():
        return {"status": "sin_panel", "flagged": []}
    df = pd.read_csv(PANEL, parse_dates=["bulletin_date"])
    f = df[df.status == "F"].sort_values("bulletin_date")
    latest_month = f.bulletin_date.max()
    flagged = []
    for (country, category, table), g in f.groupby(["country", "category", "table"]):
        g = g.sort_values("bulletin_date")
        if len(g) < 12 or g.bulletin_date.max() != latest_month:
            continue
        deltas = g.days_since_base.diff().dropna()
        if len(deltas) < 6:
            continue
        hist, last = deltas.iloc[:-1], deltas.iloc[-1]
        mad = (hist - hist.median()).abs().median()
        if mad > 0 and abs(last - hist.median()) > DATA_K * mad:
            flagged.append(
                {
                    "series": f"{country}/{category}/{table}",
                    "delta_days": int(last),
                    "hist_median_delta": int(hist.median()),
                    "mad": round(float(mad), 1),
                }
            )
    return {"status": "ok", "latest_month": str(latest_month.date()), "flagged": flagged}


def check() -> dict:
    perf = _performance_and_coverage()
    data = _data_drift()
    systemic_data = len(data.get("flagged", [])) >= DATA_MIN_FLAGGED
    drift = bool(perf.get("performance_drift") or perf.get("coverage_drift") or systemic_data)
    report = {"drift_detected": drift, "performance": perf, "data": data}
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "drift_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def _summary(r: dict) -> str:
    p, d = r["performance"], r["data"]
    lines = [f"DRIFT {'⚠️ DETECTADO' if r['drift_detected'] else 'OK — sin novedad'}"]
    if p.get("status") == "ok":
        lines.append(
            f"  desempeño: vintage {p['latest_vintage']} MASE {p['latest_mase']} vs baseline "
            f"{p['baseline_mase']} (×{p['perf_ratio']}) · cov95 {p['latest_cov95']}"
            f"{'  ⚠️' if p['performance_drift'] or p['coverage_drift'] else ''}"
        )
    else:
        lines.append(f"  desempeño: {p.get('status')}")
    n = len(d.get("flagged", []))
    lines.append(f"  datos ({d.get('latest_month', '?')}): {n} serie(s) con movimiento sin precedente")
    for fl in d.get("flagged", [])[:8]:
        lines.append(f"    · {fl['series']}: Δ{fl['delta_days']}d (mediana {fl['hist_median_delta']}, MAD {fl['mad']})")
    return "\n".join(lines)


def main() -> int:
    r = check()
    print(_summary(r))
    return 0  # monitor, no gate: nunca rompe el build


if __name__ == "__main__":
    raise SystemExit(main())
