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
Tracking MLflow (experimento "web_forecast_scoring") es para desarrollo local; el registro
DURABLE es el scorecard commiteado en git (en CI el staging MLflow es efímero).

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


def _score_rows(fc: pd.DataFrame, actuals: dict, scale_for) -> tuple[list[dict], int]:
    """Filas evaluables (objetivo ya realizado) + conteo de pendientes. Lógica pura,
    separada de la E/S para poder probarla con datos sintéticos (ver ``demo``)."""
    scored, pending = [], 0
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
    return scored, pending


def run() -> Path | None:
    log_path = REPORTS / "forecast_log.csv"
    if not log_path.exists():
        log.warning("no hay ledger %s — corre generate_web_forecasts primero", log_path)
        return None
    fc = pd.read_csv(log_path)
    actuals = dataset.actuals_F()

    # escala naïve in-sample hasta el origen, cacheada por (serie, origen) — leakage-free.
    scale_cache: dict[tuple[str, str, str, str], float] = {}

    def scale_for(country: str, category: str, table: str, origin: str) -> float:
        key = (country, category, table, origin)
        if key not in scale_cache:
            try:
                s = dataset.load_series(country, category, table)
                cutoff = pd.Timestamp(origin) + pd.offsets.MonthBegin(1)  # incluye el mes de origen
                scale_cache[key] = metrics.naive_scale_before(s, cutoff)
            except Exception as e:  # noqa: BLE001
                # B4: el fallback silencioso scale=1.0 convertía el MASE prospectivo en
                # días crudos (~10³) y fluía a key_facts→web/LaTeX/paper sin señal.
                # NaN excluye la fila del MASE (pandas mean omite NaN) sin perder su
                # cobertura; el conteo se reporta en el meta como n_no_scale.
                log.warning("sin escala para %s/%s/%s@%s: %s — fila sin MASE", country, category, table, origin, e)
                scale_cache[key] = float("nan")
        return scale_cache[key]

    scored, pending = _score_rows(fc, actuals, scale_for)

    sdf = pd.DataFrame(scored)
    sdf.to_csv(REPORTS / "forecast_scorecard.csv", index=False)
    n_no_scale = int(sdf["scaled_err"].isna().sum()) if len(sdf) else 0
    if n_no_scale:
        log.warning("%d fila(s) evaluable(s) sin escala naïve válida (excluidas del MASE)", n_no_scale)

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
    # cov80 HELD-OUT: cobertura de la banda 80 % sobre las añadas NO usadas para calibrar
    # BAND80_RATIO → out-of-sample, no circular (overall.cov80 sí incluye calibración).
    heldout = sdf[~sdf["origin"].isin(config.BAND80_CAL_VINTAGES)] if len(sdf) else sdf
    # n efectivo por añada: muchas añadas (orígenes con último-F antiguo) NO aportan filas
    # evaluables (sus meses-objetivo caen en régimen C/U) → honestidad: el grueso del n
    # viene de pocas añadas recientes. Se reporta el desglose para no inflar la amplitud.
    scored_by_vintage = {o: int((sdf["origin"] == o).sum()) for o in sorted(fc["origin"].unique())} if len(sdf) else {}
    meta = {
        "what": "evaluación prospectiva (pronóstico congelado vs corte realmente publicado)",
        "caveat": "backfill leakage-free; NO equivale a haber servido los pronósticos en tiempo real",
        "n_scored": int(len(sdf)),
        "n_no_scale": n_no_scale,
        "n_pending": int(pending),
        "n_vintages_total": int(fc["origin"].nunique()),
        "n_vintages_effective": int(sum(1 for c in scored_by_vintage.values() if c > 0)),
        "scored_by_vintage": scored_by_vintage,
        "vintages": sorted(fc["origin"].unique().tolist()),
        "overall": overall,
        "by_horizon": by_h,
        "by_table": by_table,
        "band80_calibration": {
            "cal_vintages": list(config.BAND80_CAL_VINTAGES),
            "ratio": config.BAND80_RATIO,
            "n_heldout": int(len(heldout)),
            "cov80_heldout": round(float(heldout["in80"].mean()), 3) if len(heldout) else None,
            "note": "BAND80_RATIO se calibra en cal_vintages; cov80_heldout es la cobertura 80 % OUT-OF-SAMPLE (overall.cov80 incluye la calibración y es optimista).",
        },
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


def demo() -> None:
    """Self-check de la lógica de scoring con datos sintéticos (sin BD ni modelos)."""
    fc = pd.DataFrame(
        [
            # objetivo realizado, real dentro de ambas bandas, error 10 d, escala 100 → MASE 0.1
            {
                "origin": "2024-01",
                "h": 1,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2024-02-01",
                "days": 1000,
                "lo80": 950,
                "hi80": 1050,
                "lo95": 900,
                "hi95": 1100,
            },
            # objetivo realizado, real FUERA de la banda 80 pero dentro de 95
            {
                "origin": "2024-01",
                "h": 2,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2024-03-01",
                "days": 1000,
                "lo80": 990,
                "hi80": 1010,
                "lo95": 900,
                "hi95": 1100,
            },
            # objetivo aún no realizado → pendiente
            {
                "origin": "2024-01",
                "h": 3,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2099-01-01",
                "days": 1000,
                "lo80": 950,
                "hi80": 1050,
                "lo95": 900,
                "hi95": 1100,
            },
        ]
    )
    actuals = {
        ("mexico", "F1", "FAD", "2024-02-01"): 1010.0,  # |error|=10
        ("mexico", "F1", "FAD", "2024-03-01"): 1060.0,  # |error|=60, fuera de [990,1010], dentro de [900,1100]
    }
    scored, pending = _score_rows(fc, actuals, lambda *_: 100.0)
    assert pending == 1, pending
    assert len(scored) == 2, len(scored)
    assert scored[0]["abs_err"] == 10 and abs(scored[0]["scaled_err"] - 0.1) < 1e-9
    assert scored[0]["in80"] == 1 and scored[0]["in95"] == 1
    assert scored[1]["in80"] == 0 and scored[1]["in95"] == 1  # cobertura 80 distingue de 95
    print("OK — score_forecasts: pendientes y cobertura 80/95 + MASE correctos")


if __name__ == "__main__":
    import sys

    (demo if "--demo" in sys.argv else run)()
