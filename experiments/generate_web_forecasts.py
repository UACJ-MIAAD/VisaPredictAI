"""Genera los pronósticos FUTUROS por serie para el demostrador web (visapredictai.com).

A diferencia de ``export_forecasts.py`` / ``persist_forecasts.py`` (que guardan el
*hold-out* para comparar/ensamblar), aquí se produce el pronóstico genuino a 12 meses
**más allá del último boletín**, con bandas de predicción al 80 % / 95 %, para cada serie
piloto país × categoría × tabla. Es lo que la app muestra cuando el usuario pide
"el pronóstico de F2A": pronósticos de los **modelos de producción**, no la línea base
de deriva del navegador (que queda solo como respaldo para series sin pronóstico real).

Modelo de producción por tabla (coincide con los ganadores del entregable):
  • FAD → mediana de {Theta, ETS, SARIMA}  (el ensamble que supera al global en FAD)
  • DFF → SARIMA                            (imbatible en DFF)

Intervalo de predicción: **conforme dividido** (split conformal, ``intervals.conformal``,
mecanismo model-agnostic del proyecto) calibrado sobre los residuales de 1 paso del
hold-out, ensanchado a lo largo del horizonte por √h (crecimiento tipo random-walk del
error acumulado). Mecanismo y ganadores alineados con el .tex (§4.3.2).

Salidas (tidy, versionadas en git como el resto de reports/):
  • reports/web_forecasts.csv       — country,category,table,date,days,lo80,hi80,lo95,hi95
  • reports/web_forecasts_meta.json — método + métricas hold-out por serie (procedencia)

Cada serie se registra en MLflow vía ``tracking.log_run`` (experimento "web_forecasts").

Corre en ``ante`` desde la raíz:  ante/bin/python experiments/generate_web_forecasts.py
"""

from __future__ import annotations

import json
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from darts import TimeSeries

import tracking
from vp_model import config, dataset, intervals, metrics, models

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
HORIZON = 12
# modelo(s) de producción por tabla — punto = mediana del conjunto (1 elem = ese modelo).
PROD: dict[str, tuple[str, ...]] = {"FAD": ("theta", "ets", "sarima"), "DFF": ("sarima",)}
log = config.get_logger("web_forecasts")


def _holdout_preds(model_set: tuple[str, ...], country: str, category: str, table: str):
    """(serie, dict modelo->pred 1-paso del hold-out). Lanza si la serie es muy corta.

    Walk-forward de 1 paso, leakage-free, **solo sobre los 24 meses de hold-out**
    (los modelos locales de darts exigen ``retrain=True``; 24 reentrenamientos por
    modelo es barato y es la ventana que calibra el conforme y da procedencia).
    """
    ts = models.to_timeseries(dataset.load_series(country, category, table))
    if len(ts) < config.MIN_TRAIN[table] + config.HOLDOUT + config.MIN_BACKTEST_BUFFER:
        raise ValueError(f"serie demasiado corta ({len(ts)})")
    split = ts.time_index[-config.HOLDOUT]
    preds: dict[str, TimeSeries] = {}
    for name in model_set:
        m = models.build_model(name)
        preds[name] = m.historical_forecasts(  # type: ignore[attr-defined]
            ts, start=split, forecast_horizon=1, stride=1, retrain=True, last_points_only=True, verbose=False
        )
    return ts, preds


def _ensemble_point(values: list[np.ndarray]) -> np.ndarray:
    """Mediana elemento a elemento del conjunto (robusta, à la M-competitions)."""
    return np.median(np.vstack(values), axis=0)


def _series_forecast(country: str, category: str, table: str) -> tuple[list[dict], dict] | None:
    model_set = PROD[table]
    try:
        ts, hold_preds = _holdout_preds(model_set, country, category, table)
    except Exception as e:  # noqa: BLE001 — serie corta / sin F suficientes → respaldo web
        log.info("skip %s/%s/%s: %s", country, category, table, e)
        return None

    # pronóstico ensamble del hold-out (mediana de los modelos en las fechas comunes)
    common = hold_preds[model_set[0]].time_index
    for p in hold_preds.values():
        common = common.intersection(p.time_index)
    actual = ts.slice_intersect(hold_preds[model_set[0]]).to_series().reindex(common)
    ens_hold = _ensemble_point([p.to_series().reindex(common).to_numpy() for p in hold_preds.values()])
    ens_hold_ts = TimeSeries.from_series(pd.Series(ens_hold, index=common))
    actual_ts = TimeSeries.from_series(actual)

    # semianchos conformes de 1 paso (95 % y 80 %) sobre el hold-out del ensamble
    half95 = (
        (intervals.conformal(ens_hold_ts, actual_ts, ens_hold_ts, alpha=0.05).upper - ens_hold_ts).values().flatten()[0]
    )
    half80 = (
        (intervals.conformal(ens_hold_ts, actual_ts, ens_hold_ts, alpha=0.20).upper - ens_hold_ts).values().flatten()[0]
    )

    # métricas de procedencia (hold-out)
    insample = ts.split_before(ts.time_index[-config.HOLDOUT])[0]
    mt = metrics.compute(actual_ts, ens_hold_ts, insample)
    lo95_h = ens_hold_ts - float(half95)
    hi95_h = ens_hold_ts + float(half95)
    cov95 = metrics.pi_coverage(actual_ts, lo95_h, hi95_h)

    # pronóstico FUTURO: ajustar cada modelo en TODA la serie y predecir 12 meses
    fut: list[np.ndarray] = []
    for name in model_set:
        m = models.build_model(name)
        m.fit(ts)  # theta/ets/sarima no requieren covariables
        fut.append(m.predict(HORIZON).to_series().to_numpy())
    point = _ensemble_point(fut)
    future_idx = pd.date_range(ts.end_time() + ts.freq, periods=HORIZON, freq=ts.freq)

    rows = []
    for h, (d, pv) in enumerate(zip(future_idx, point, strict=True), start=1):
        grow = math.sqrt(h)  # error acumulado tipo random-walk
        rows.append(
            {
                "country": country,
                "category": category,
                "table": table,
                "date": d.strftime("%Y-%m-%d"),
                "days": int(round(pv)),
                "lo80": int(round(pv - half80 * grow)),
                "hi80": int(round(pv + half80 * grow)),
                "lo95": int(round(pv - half95 * grow)),
                "hi95": int(round(pv + half95 * grow)),
            }
        )
    meta = {
        "n_obs": len(ts),
        "last_month": ts.end_time().strftime("%Y-%m"),
        "models": list(model_set),
        "mase": round(float(mt.get("mase", float("nan"))), 4),
        "smape": round(float(mt.get("smape", float("nan"))), 4),
        "cov95_holdout": round(float(cov95), 4),
        "half95_1step_days": int(round(half95)),
        "half80_1step_days": int(round(half80)),
    }
    tracking.log_run(
        "web_forecasts",
        f"{table}/{country}/{category}",
        params={
            "country": country,
            "category": category,
            "table": table,
            "models": "+".join(model_set),
            "horizon": HORIZON,
        },
        metrics={"mase": meta["mase"], "smape": meta["smape"], "cov95": meta["cov95_holdout"], "n_obs": len(ts)},
        tags={"kind": "web_forecast", "pi": "conformal_sqrt_h"},
    )
    return rows, {f"{country}/{category}/{table}": meta}


def run() -> tuple[Path, Path]:
    all_rows: list[dict] = []
    all_meta: dict = {}
    for table in config.TABLES:
        for block in ("family", "employment"):
            cat = dataset.list_series(table=table, block=block, countries=config.PILOT_COUNTRIES)
            for r in cat.itertuples():
                out = _series_forecast(r.country, r.category, table)
                if out is None:
                    continue
                rows, meta = out
                all_rows += rows
                all_meta.update(meta)
                log.info("✓ %s/%s/%s (%d series acumuladas)", table, r.country, r.category, len(all_meta))

    csv_path = REPORTS / "web_forecasts.csv"
    pd.DataFrame(all_rows).to_csv(csv_path, index=False)
    meta_path = REPORTS / "web_forecasts_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "method": {
                    "FAD": "Mediana de Theta + ETS + SARIMA · intervalo conforme (95 %/80 %) ensanchado por √h",
                    "DFF": "SARIMA · intervalo conforme (95 %/80 %) ensanchado por √h",
                },
                "horizon_months": HORIZON,
                "base_date": "1975-01-01",
                "n_series": len(all_meta),
                "series": all_meta,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    log.info("escrito -> %s (%d filas, %d series)", csv_path, len(all_rows), len(all_meta))
    return csv_path, meta_path


if __name__ == "__main__":
    run()
