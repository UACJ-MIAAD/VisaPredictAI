"""Evaluación PROSPECTIVA (en tiempo real) de los pronósticos congelados.

El MASE del entregable es *retrospectivo* (hold-out: el modelo "predice" meses ya
conocidos). Esto es lo contrario: toma cada pronóstico **congelado** en
``reports/forecast_log.csv`` (lo que predijimos y desde qué mes — la "añada") y lo
compara con el **corte realmente publicado** después, conforme llegan los boletines.
Es la única medida honesta de qué tan bueno es el pronóstico a 12 meses en el mundo real.

Por cada fila del ledger cuyo mes-objetivo ya tiene un corte real (estado F en el panel):
  • error = predicho − real (días);   |error|;   error escalado (MASE) por la escala
    naïve estacional in-sample hasta el origen (leakage-free, misma def. que el .tex);
  • cobertura: ¿el real cayó dentro de la banda 80 % / 95 %?

Agrega global, por horizonte h=1..12 y por tabla. Salidas:
  • reports/forecast_scorecard.csv       — una fila por predicción ya evaluable
  • reports/forecast_scorecard_meta.json — agregados (MAE/MASE/cobertura, n)
y registra los agregados en MLflow (experimento "web_forecast_scoring").

Al inicio de una añada nada está realizado aún → n=0 (correcto): la medición se
acumula mes a mes. Corre en ``ante``:  ante/bin/python experiments/score_forecasts.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import tracking
from vp_model import config, dataset, metrics

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
log = config.get_logger("score_forecasts")


def _actuals() -> dict[tuple[str, str, str, str], float]:
    """Cortes reales (estado F) del panel: (país, categoría, tabla, 'YYYY-MM-01') -> días."""
    con = dataset._connect()
    try:
        df = con.execute('SELECT country, category, "table", bulletin_date, days_since_base FROM mart_training_F').df()
    finally:
        con.close()
    out: dict[tuple[str, str, str, str], float] = {}
    for r in df.itertuples():
        out[(r.country, r.category, r.table, pd.Timestamp(r.bulletin_date).strftime("%Y-%m-%d"))] = float(
            r.days_since_base
        )
    return out


def run() -> Path | None:
    log_path = REPORTS / "forecast_log.csv"
    if not log_path.exists():
        log.warning("no hay ledger %s — corre generate_web_forecasts primero", log_path)
        return None
    fc = pd.read_csv(log_path)
    actuals = _actuals()

    # escala naïve in-sample hasta el origen, cacheada por (serie, origen) — leakage-free.
    scale_cache: dict[tuple[str, str, str, str], float] = {}

    def scale_for(country: str, category: str, table: str, origin: str) -> float:
        key = (country, category, table, origin)
        if key not in scale_cache:
            try:
                s = dataset.load_series(country, category, table)
                cutoff = pd.Timestamp(origin) + pd.offsets.MonthBegin(1)  # incluye el mes de origen
                scale_cache[key] = metrics.naive_scale_before(s, cutoff)
            except Exception:  # noqa: BLE001
                scale_cache[key] = 1.0
        return scale_cache[key]

    scored = []
    pending = 0
    for r in fc.itertuples():
        actual = actuals.get((r.country, r.category, r.table, r.date))
        if actual is None:  # mes-objetivo aún no publicado, o no es estado F → no evaluable todavía
            pending += 1
            continue
        sc = scale_for(r.country, r.category, r.table, r.origin)
        abs_err = abs(r.days - actual)
        scored.append(
            {
                "origin": r.origin,
                "h": r.h,
                "country": r.country,
                "category": r.category,
                "table": r.table,
                "target": r.date,
                "pred": r.days,
                "actual": actual,
                "error": r.days - actual,
                "abs_err": abs_err,
                "scaled_err": abs_err / sc,
                "in80": int(r.lo80 <= actual <= r.hi80),
                "in95": int(r.lo95 <= actual <= r.hi95),
            }
        )

    sdf = pd.DataFrame(scored)
    sdf.to_csv(REPORTS / "forecast_scorecard.csv", index=False)

    def agg(d: pd.DataFrame) -> dict:
        return {
            "n": int(len(d)),
            "mae_days": round(float(d["abs_err"].mean()), 1),
            "mase": round(float(d["scaled_err"].mean()), 4),
            "cov80": round(float(d["in80"].mean()), 3),
            "cov95": round(float(d["in95"].mean()), 3),
        }

    overall = agg(sdf) if len(sdf) else {"n": 0}
    by_h = {int(h): agg(g) for h, g in sdf.groupby("h")} if len(sdf) else {}
    by_table = {t: agg(g) for t, g in sdf.groupby("table")} if len(sdf) else {}
    meta = {
        "what": "evaluación prospectiva (pronóstico congelado vs corte realmente publicado)",
        "n_scored": int(len(sdf)),
        "n_pending": int(pending),
        "vintages": sorted(fc["origin"].unique().tolist()),
        "overall": overall,
        "by_horizon": by_h,
        "by_table": by_table,
    }
    (REPORTS / "forecast_scorecard_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    if len(sdf):
        tracking.log_run(
            "web_forecast_scoring",
            "overall",
            params={"n_vintages": fc["origin"].nunique(), "scope": "prospective"},
            metrics={
                "mae_days": overall["mae_days"],
                "mase": overall["mase"],
                "cov95": overall["cov95"],
                "n": overall["n"],
            },
            tags={"kind": "prospective_score"},
        )
        for h, a in by_h.items():
            tracking.log_run(
                "web_forecast_scoring",
                f"h{h:02d}",
                params={"horizon": h, "scope": "prospective"},
                metrics={"mae_days": a["mae_days"], "mase": a["mase"], "cov95": a["cov95"], "n": a["n"]},
                tags={"kind": "prospective_score"},
            )
        log.info(
            "PROSPECTIVO: n=%d · MAE %.0f d · MASE %.3f · cob95 %.0f%%",
            overall["n"],
            overall["mae_days"],
            overall["mase"],
            overall["cov95"] * 100,
        )
    else:
        log.info("PROSPECTIVO: 0 objetivos realizados aún (%d pendientes) — se acumula con cada boletín", pending)
    return REPORTS / "forecast_scorecard.csv"


if __name__ == "__main__":
    run()
