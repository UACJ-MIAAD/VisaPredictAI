"""Monitor de drift de ML (distinto del guardián de consistencia, que vigila CIFRAS).

Vigila tres derivas y emite ``reports/governance/drift_report.json`` + un resumen legible. NO es un gate
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
COVERAGE_MIN_N = 30  # AO7: piso de n para evaluar cobertura (con n chico, cov95 es ruido binomial)
DATA_K = 8.0  # |delta del último mes| > 8× MAD histórica -> movimiento sin precedente
DATA_MIN_FLAGGED = 8  # nº de series con movimiento sin precedente para considerarlo SISTÉMICO
RECAMPAIGN_STREAK = 3  # AO8: performance_drift N vintages seguidos -> "re-campaign due" (gate humano)


def _performance_and_coverage() -> dict:
    """Performance/coverage drift on the prospective scorecard, horizon-matched (AO7).

    The latest vintage only has SHORT horizons realized while older vintages carry
    h up to 12; since error grows ~sqrt(h), comparing the young vintage against the
    all-horizon baseline biased ``perf_ratio`` optimistic. Fix: restrict the baseline
    to the SAME horizon mix (the h values realized in the latest vintage).
    """
    sc = REPORTS / "prospective" / "forecast_scorecard.csv"
    if not sc.exists():
        return {"status": "sin_ledger"}
    df = pd.read_csv(sc)
    by = df.groupby("origin").agg(n=("scaled_err", "size"), mase=("scaled_err", "mean"), cov95=("in95", "mean"))
    scored = by[by.n > 0].sort_index()
    if len(scored) < 2:
        return {"status": "insuficiente", "n_vintages": int(len(scored))}
    latest_vintage = scored.index[-1]
    latest_rows = df[df.origin == latest_vintage]
    horizons = sorted(latest_rows.h.unique().tolist())
    base_rows = df[(df.origin != latest_vintage) & df.origin.isin(scored.index) & df.h.isin(horizons)]
    latest_mase = float(latest_rows.scaled_err.mean())
    baseline = float(base_rows.scaled_err.mean()) if len(base_rows) else float("nan")
    perf_ratio = latest_mase / baseline if baseline and baseline == baseline else float("nan")
    latest_n = int(len(latest_rows))
    coverage_evaluated = latest_n >= COVERAGE_MIN_N
    latest_cov95 = float(latest_rows.in95.mean())
    return {
        "status": "ok",
        "latest_vintage": latest_vintage,
        "latest_n": latest_n,
        "latest_mase": round(latest_mase, 4),
        "baseline_mase": round(baseline, 4) if baseline == baseline else None,
        "baseline_n": int(len(base_rows)),
        "horizons_matched": horizons,
        "perf_ratio": round(perf_ratio, 3) if perf_ratio == perf_ratio else None,
        "latest_cov95": round(latest_cov95, 3),
        "coverage_evaluated": coverage_evaluated,
        "performance_drift": bool(perf_ratio == perf_ratio and perf_ratio > PERF_RATIO),
        # AO7: with n < COVERAGE_MIN_N a low cov95 is likely binomial noise, not drift.
        "coverage_drift": bool(coverage_evaluated and latest_cov95 < COV95_FLOOR),
    }


def _update_history(perf: dict) -> dict:
    """Persist per-vintage drift history and derive the re-campaign trigger (AO8).

    One record per vintage (keyed by ``latest_vintage``; re-checks of the same vintage
    with more realized rows UPDATE its record). ``recampaign_due`` = the last
    RECAMPAIGN_STREAK distinct vintages all flagged performance_drift. The trigger only
    OPENS AN ISSUE downstream (human gate) — it never launches a campaign.
    """
    if perf.get("status") != "ok":
        return {"consecutive_perf_drift": 0, "recampaign_due": False}
    history = REPORTS / "governance" / "drift_history.jsonl"  # via global: tests repoint REPORTS
    records: dict[str, dict] = {}
    if history.exists():
        for line in history.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                records[rec["vintage"]] = rec
    records[str(perf["latest_vintage"])] = {
        "vintage": str(perf["latest_vintage"]),
        "performance_drift": bool(perf["performance_drift"]),
        "perf_ratio": perf["perf_ratio"],
        "latest_n": perf["latest_n"],
    }
    ordered = [records[k] for k in sorted(records)]
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered))
    streak = 0
    for rec in reversed(ordered):
        if not rec["performance_drift"]:
            break
        streak += 1
    return {"consecutive_perf_drift": streak, "recampaign_due": streak >= RECAMPAIGN_STREAK}


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
    perf.update(_update_history(perf))  # AO8: streak + re-campaign trigger (human gate)
    data = _data_drift()
    systemic_data = len(data.get("flagged", [])) >= DATA_MIN_FLAGGED
    drift = bool(perf.get("performance_drift") or perf.get("coverage_drift") or systemic_data)
    report = {"drift_detected": drift, "performance": perf, "data": data}
    (REPORTS / "governance").mkdir(parents=True, exist_ok=True)
    (REPORTS / "governance" / "drift_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def _summary(r: dict) -> str:
    p, d = r["performance"], r["data"]
    lines = [f"DRIFT {'⚠️ DETECTADO' if r['drift_detected'] else 'OK — sin novedad'}"]
    if p.get("status") == "ok":
        cov = f"cov95 {p['latest_cov95']}" if p["coverage_evaluated"] else f"cov95 n/e (n={p['latest_n']}<30)"
        lines.append(
            f"  desempeño: vintage {p['latest_vintage']} (n={p['latest_n']}, h={p['horizons_matched']}) "
            f"MASE {p['latest_mase']} vs baseline {p['baseline_mase']} (×{p['perf_ratio']}) · {cov}"
            f"{'  ⚠️' if p['performance_drift'] or p['coverage_drift'] else ''}"
        )
        if p.get("recampaign_due"):
            lines.append(f"  ⚠️ RE-CAMPAIGN DUE: performance_drift {p['consecutive_perf_drift']} vintages seguidos")
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
