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

Prediction interval (AN1/AN2/AN4):
  • 1-step half-width: split conformal (``intervals.conformal``) calibrated on the
    hold-out residuals of the deployed ensemble, **F-only** (``calib_dates`` = the raw
    F index; interpolated C/U months are NOT scored nor calibrated on — B1). The
    hold-out MASE/coverage in the meta use the same mask and the same naive scale as
    ``walkforward.backtest``.
  • Horizon growth: empirical per-horizon quantiles ``q_{table, level, h}`` from the
    prospective ledger (``reports/prospective/pi_scale_by_h.json``, derived by
    ``experiments/derive_band80_ratio.py`` on a disjoint vintage split). Documented
    fallback: if the JSON is missing or has no cell for (table, level, h), the band
    reverts to the legacy sqrt(h) heuristic with ``config.BAND80_RATIO`` for the 80 %
    band. Real multi-step coverage is still measured by ``score_forecasts.py``.
  • Per-series ACI (Gibbs & Candès): if the prospective ledger already scored >= 8
    forecasts of a series, the conformal level is adapted from its hit history
    (``intervals.aci_alpha``); otherwise the nominal ``config.ALPHA`` is used. The
    gamma comes from ``reports/eval/aci_gamma.json`` (written by
    ``experiments/improve_conformal.py``), default 0.05.

Salidas (tidy, versionadas en git como el resto de reports/):
  • reports/prospective/web_forecasts.csv       — country,category,table,date,days,lo80,hi80,lo95,hi95
  • reports/prospective/web_forecasts_meta.json — método + métricas hold-out por serie (procedencia)

Tracking MLflow vía ``tracking.log_run`` (experimento "web_forecasts") es para **desarrollo
local**; en CI el staging es efímero — el registro DURABLE de procedencia es el CSV/JSON
commiteado en git + el git_sha que ``tracking`` graba en cada record.

Corre en ``ante`` desde la raíz:  ante/bin/python experiments/generate_web_forecasts.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from darts import TimeSeries

from vp_data import tracking
from vp_model import champion, config, dataset, intervals, metrics, models

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
HORIZON = 12
ACI_MIN_HITS = 8  # minimum scored ledger rows for a series before ACI kicks in (AN4)
ACI_GAMMA_DEFAULT = 0.05
log = config.get_logger("web_forecasts")


def _load_pi_scales() -> dict | None:
    """Per-horizon band scales q_{table, level, h} (AN2); None -> sqrt(h) fallback."""
    path = REPORTS / "prospective" / "pi_scale_by_h.json"
    if not path.exists():
        log.warning("no %s — bands fall back to sqrt(h) growth (run derive_band80_ratio)", path.name)
        return None
    return json.loads(path.read_text())["scales"]


def _load_aci_gamma() -> dict[str, float]:
    """ACI step size per table, selected on calibration vintages by improve_conformal (AN4)."""
    path = REPORTS / "eval" / "aci_gamma.json"
    if not path.exists():
        return {t: ACI_GAMMA_DEFAULT for t in config.TABLES}
    raw = json.loads(path.read_text())
    return {t: float(raw.get(t, ACI_GAMMA_DEFAULT)) for t in config.TABLES}


def _ledger_hits() -> dict[tuple[str, str, str], list[int]]:
    """Chronological in95 hit history per series from the prospective scorecard (AN4)."""
    path = REPORTS / "prospective" / "forecast_scorecard.csv"
    if not path.exists():
        return {}
    sc = pd.read_csv(path).sort_values(["target", "origin", "h"])
    return {
        (c, cat, t): g["in95"].astype(int).tolist() for (c, cat, t), g in sc.groupby(["country", "category", "table"])
    }


def _band_halfwidths(h: int, half95_1step: float, table: str, scales: dict | None) -> tuple[float, float, str]:
    """(half80, half95, method) at horizon ``h`` from the 1-step conformal half-width.

    Primary path (AN2): empirical ledger quantiles ``q_{table, level, h}``. Documented
    fallback when the JSON or the (table, level, h) cell is missing (e.g. h beyond the
    calibrated range, or a cell below the min-n floor): legacy sqrt(h) random-walk
    growth with the scalar ``config.BAND80_RATIO`` for the 80 % band.
    """
    if scales is not None:
        t = scales.get(table, {})
        q80, q95 = t.get("80", {}).get(str(h)), t.get("95", {}).get(str(h))
        if q80 is not None and q95 is not None:
            return half95_1step * float(q80), half95_1step * float(q95), "q_h"
    grow = math.sqrt(h)
    return half95_1step * config.BAND80_RATIO * grow, half95_1step * grow, "sqrt_h"


def _holdout_preds(model_set: tuple[str, ...], country: str, category: str, table: str, as_of: str | None = None):
    """(serie darts, serie F cruda, dict modelo->pred 1-paso del hold-out).

    Walk-forward de 1 paso, leakage-free, **solo sobre los 24 meses de hold-out**
    (los modelos locales de darts exigen ``retrain=True``; 24 reentrenamientos por
    modelo es barato y es la ventana que calibra el conforme y da procedencia).

    ``as_of`` (YYYY-MM) trunca la serie a ese mes inclusive para generar una añada
    HISTÓRICA leakage-free (origen del pronóstico) y poder medirla contra los reales
    ya observados — la base de la evaluación prospectiva. The raw F-only series is
    returned alongside because its index is the B1 mask (calibration + scoring must
    ignore the months ``to_timeseries`` interpolates).
    """
    raw = dataset.load_series(country, category, table).astype("float64")
    ts = models.to_timeseries(raw)
    if as_of is not None:
        cut = pd.Timestamp(as_of) + pd.offsets.MonthBegin(1)
        if not (ts.start_time() < cut <= ts.end_time() + ts.freq):  # serie sin datos en ese origen
            raise ValueError(f"as_of={as_of} fuera del rango de la serie")
        ts = ts.drop_after(cut)
        raw = raw[raw.index < cut]
    if len(ts) < config.MIN_TRAIN[table] + config.HOLDOUT + config.MIN_BACKTEST_BUFFER:
        raise ValueError(f"serie demasiado corta ({len(ts)})")
    split = ts.time_index[-config.HOLDOUT]
    preds: dict[str, TimeSeries] = {}
    for name in model_set:
        m = models.build_model(name, table=table)  # tuned per-table params for GBMs (Wave-1)
        preds[name] = m.historical_forecasts(  # type: ignore[attr-defined]
            ts, start=split, forecast_horizon=1, stride=1, retrain=True, last_points_only=True, verbose=False
        )
    return ts, raw, preds


def _ensemble_point(values: list[np.ndarray]) -> np.ndarray:
    """Mediana elemento a elemento del conjunto (robusta, à la M-competitions)."""
    return np.median(np.vstack(values), axis=0)


def _series_forecast(
    country: str,
    category: str,
    table: str,
    as_of: str | None,
    prod: dict[str, tuple[str, ...]],
    pi_scales: dict | None,
    aci_gamma: dict[str, float],
    hits: dict[tuple[str, str, str], list[int]],
) -> tuple[list[dict], dict] | None:
    """Error boundary: cualquier fallo de una serie/modelo (serie corta, ``as_of`` fuera
    de rango, error numérico de SARIMA, etc.) la OMITE sin abortar la añada completa."""
    try:
        return _compute_series_forecast(country, category, table, as_of, prod, pi_scales, aci_gamma, hits)
    except Exception as e:  # noqa: BLE001 — robustez: una serie que falla no tumba la corrida
        log.info("skip %s/%s/%s: %s", country, category, table, e)
        return None


def _compute_series_forecast(
    country: str,
    category: str,
    table: str,
    as_of: str | None,
    prod: dict[str, tuple[str, ...]],
    pi_scales: dict | None,
    aci_gamma: dict[str, float],
    hits: dict[tuple[str, str, str], list[int]],
) -> tuple[list[dict], dict]:
    model_set = prod[table]
    ts, raw, hold_preds = _holdout_preds(model_set, country, category, table, as_of)
    origin = ts.end_time().strftime("%Y-%m")  # mes desde el que se pronostica (la "añada")
    fdates = raw.index  # B1 mask: real F observations only (AN1)

    # pronóstico ensamble del hold-out (mediana de los modelos en las fechas comunes)
    common = hold_preds[model_set[0]].time_index
    for p in hold_preds.values():
        common = common.intersection(p.time_index)
    actual = ts.slice_intersect(hold_preds[model_set[0]]).to_series().reindex(common)
    ens_hold = _ensemble_point([p.to_series().reindex(common).to_numpy() for p in hold_preds.values()])
    ens_hold_ts = TimeSeries.from_series(pd.Series(ens_hold, index=common))
    actual_ts = TimeSeries.from_series(actual)

    # AN4: per-series adaptive level from the prospective hit history (>= ACI_MIN_HITS
    # scored ledger rows), else the nominal alpha. A miss streak lowers alpha_eff ->
    # wider next-vintage bands; the live vintage (as_of=None) is the one being adapted.
    # NOTE (interplay with q_h): the hit history pools ALL horizons (h1-only histories
    # are too short: <=3 per series today), so ACI also reacts to multi-step misses that
    # the q_h scales correct on average — a deliberate belt-and-suspenders overlap. The
    # tempering knob is gamma (grid-selected on calibration vintages by
    # improve_conformal -> aci_gamma.json); once q_h bands enter the ledger, the online
    # hit stream self-corrects. alpha_eff is recorded in the meta for auditability.
    hit_hist = hits.get((country, category, table), []) if as_of is None else []
    alpha_eff = (
        intervals.aci_alpha(hit_hist, alpha0=config.ALPHA, gamma=aci_gamma[table])
        if len(hit_hist) >= ACI_MIN_HITS
        else config.ALPHA
    )

    # 1-step conformal half-width at alpha_eff, calibrated on F-only hold-out residuals
    # of the deployed ensemble (AN1: without calib_dates the interpolated C/U months
    # shrank the residuals and the bands).
    half95 = (
        (
            intervals.conformal(ens_hold_ts, actual_ts, ens_hold_ts, alpha=alpha_eff, calib_dates=fdates).upper
            - ens_hold_ts
        )
        .values()
        .flatten()[0]
    )

    # métricas de procedencia (hold-out) — F-only + shared naive scale (same recipe as
    # walkforward.backtest; without the mask the meta MASE/coverage were contaminated).
    split = ts.time_index[-config.HOLDOUT]
    scale = metrics.naive_scale_before(raw, split)
    insample = ts.split_before(split)[0]
    mt = metrics.compute(actual_ts, ens_hold_ts, insample, dates=fdates, scale=scale)
    lo95_h = ens_hold_ts - float(half95)
    hi95_h = ens_hold_ts + float(half95)
    cov95 = metrics.pi_coverage(actual_ts, lo95_h, hi95_h, dates=fdates)
    n_f_holdout = int(common.isin(fdates).sum())

    # pronóstico FUTURO: ajustar cada modelo en TODA la serie y predecir 12 meses
    fut: list[np.ndarray] = []
    for name in model_set:
        m = models.build_model(name, table=table)  # tuned per-table params for GBMs (Wave-1)
        m.fit(ts)  # theta/ets/sarima no requieren covariables
        fut.append(m.predict(HORIZON).to_series().to_numpy())
    point = _ensemble_point(fut)
    future_idx = pd.date_range(ts.end_time() + ts.freq, periods=HORIZON, freq=ts.freq)

    rows = []
    band_methods = set()
    for h, (d, pv) in enumerate(zip(future_idx, point, strict=True), start=1):
        half80_h, half95_h, band_method = _band_halfwidths(h, float(half95), table, pi_scales)
        band_methods.add(band_method)
        rows.append(
            {
                "origin": origin,
                "h": h,
                "country": country,
                "category": category,
                "table": table,
                "date": d.strftime("%Y-%m-%d"),
                "days": int(round(pv)),
                "lo80": int(round(pv - half80_h)),
                "hi80": int(round(pv + half80_h)),
                "lo95": int(round(pv - half95_h)),
                "hi95": int(round(pv + half95_h)),
            }
        )
    # AN7: a per-series hold-out coverage without its n would overstate precision; the
    # Jeffreys CI is emitted alongside (n is small by construction — 24-month hold-out).
    cov_ci = intervals.jeffreys_ci(int(round(cov95 * n_f_holdout)), n_f_holdout) if n_f_holdout else (None, None)
    meta = {
        "n_obs": len(ts),
        "n_f_obs": int(len(raw)),
        "last_month": ts.end_time().strftime("%Y-%m"),
        "models": list(model_set),
        "mase": round(float(mt.get("mase", float("nan"))), 4),
        "smape": round(float(mt.get("smape", float("nan"))), 4),
        "cov95_holdout": round(float(cov95), 4),
        "cov95_holdout_n": n_f_holdout,
        "cov95_holdout_ci95": [round(c, 3) for c in cov_ci] if cov_ci[0] is not None else None,
        "alpha_eff": round(float(alpha_eff), 4),
        "band_method": sorted(band_methods),
        "half95_1step_days": int(round(half95)),
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
        tags={"kind": "web_forecast", "pi": "conformal_qh" if "q_h" in band_methods else "conformal_sqrt_h"},
    )
    return rows, {f"{country}/{category}/{table}": meta}


WEB_COLS = ["country", "category", "table", "date", "days", "lo80", "hi80", "lo95", "hi95"]
LOG_COLS = ["origin", "h", *WEB_COLS]
LOG_KEYS = ["origin", "country", "category", "table", "date"]


def _append_log(rows: list[dict]) -> Path:
    """Anexa la añada al ledger append-only ``reports/prospective/forecast_log.csv`` (idempotente
    por (origin, serie, fecha-objetivo)). Es el registro inmutable de lo que
    predijimos y desde cuándo — base de la evaluación prospectiva (``score_forecasts``).

    C3: ``keep="first"`` — un pronóstico ya congelado NUNCA se sobrescribe. Con
    ``keep="last"`` un re-run (código/semilla distinta) reemplazaba añadas ya
    archivadas e invalidaba la evaluación prospectiva."""
    log_path = REPORTS / "prospective" / "forecast_log.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows)[LOG_COLS]
    combined = pd.concat([pd.read_csv(log_path), new], ignore_index=True) if log_path.exists() else new
    combined = combined.drop_duplicates(subset=LOG_KEYS, keep="first").sort_values(LOG_KEYS)
    combined.to_csv(log_path, index=False)
    log.info("ledger -> %s (%d filas, %d añadas)", log_path, len(combined), combined["origin"].nunique())
    return log_path


def run(as_of: str | None = None) -> tuple[Path, Path]:
    import warnings

    warnings.filterwarnings("ignore")  # AP5: scoped to the run, not an import side effect
    config.seed_everything()  # reproducibilidad: misma semilla para todo lo estocástico
    # Receta de producción por tabla — leída del MANIFIESTO campeón (champion_manifest.json),
    # que es la receta desplegada versionada. El harness campeón-retador
    # (experiments/run_champion_challenger.py --promote) es lo ÚNICO que la cambia, de forma
    # auditada. Punto = mediana del conjunto (1 elemento = ese modelo). Fallback a la receta
    # histórica si el manifiesto no existe. (AP5: loaded here, not at import time.)
    prod: dict[str, tuple[str, ...]] = {t: r.models for t, r in champion.load_manifest().items()}
    pi_scales = _load_pi_scales()
    aci_gamma = _load_aci_gamma()
    hits = _ledger_hits()
    all_rows: list[dict] = []
    all_meta: dict = {}
    for table in config.TABLES:
        for block in ("family", "employment"):
            cat = dataset.list_series(table=table, block=block, countries=config.PILOT_COUNTRIES)
            for r in cat.itertuples():
                out = _series_forecast(r.country, r.category, table, as_of, prod, pi_scales, aci_gamma, hits)
                if out is None:
                    continue
                rows, meta = out
                all_rows += rows
                all_meta.update(meta)
                log.info("✓ %s/%s/%s (%d series acumuladas)", table, r.country, r.category, len(all_meta))

    # C2: gate de salida — un env roto a medias (dep faltante, BD vieja) produce una
    # añada casi vacía vía los error-boundaries por serie. NO publicar, NO archivar:
    # el ledger es inmutable (C3) y una añada parcial congelada lo contamina para siempre.
    csv_path = REPORTS / "prospective" / "web_forecasts.csv"
    meta_path = REPORTS / "prospective" / "web_forecasts_meta.json"
    expected = json.loads(meta_path.read_text())["n_series"] if meta_path.exists() else 0
    if expected and len(all_meta) < 0.9 * expected:
        raise SystemExit(
            f"ABORT: solo {len(all_meta)} series pronosticadas (<90% de las {expected} del run previo) "
            "— entorno roto a medias; no se publica ni se archiva la añada"
        )

    _append_log(all_rows)  # archiva la añada (cualquier as_of)
    # La añada en vivo (as_of=None) es además la que sirve la web; el meta describe el
    # CSV vivo, así que un backfill histórico NO debe reescribirlo (C3).
    if as_of is None:
        pd.DataFrame(all_rows)[WEB_COLS].to_csv(csv_path, index=False)
        # método derivado del manifiesto campeón (prod), no prosa congelada (C3)
        pretty = {"theta": "Theta", "ets": "ETS", "sarima": "SARIMA", "arima": "ARIMA", "kalman": "Kalman"}
        band_txt = (
            "bandas por cuantil empírico por horizonte (ledger prospectivo)"
            if pi_scales is not None
            else "ensanchado por √h"
        )
        method = {
            t: (("Mediana de " if len(prod[t]) > 1 else "") + " + ".join(pretty.get(m, m) for m in prod[t]))
            + f" · intervalo conforme (95 %/80 %) {band_txt}"
            for t in config.TABLES
        }

        # Literal NaN is invalid JSON — the browser's JSON.parse dies and takes the
        # whole forecasts/scorecard section with it (caught live by the web render
        # check). Sanitize to null and make json.dumps refuse any future NaN.
        def _no_nan(obj):
            if isinstance(obj, dict):
                return {k: _no_nan(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_no_nan(v) for v in obj]
            if isinstance(obj, float) and obj != obj:
                return None
            return obj

        meta_path.write_text(
            json.dumps(
                _no_nan(
                    {
                        "method": method,
                        "horizon_months": HORIZON,
                        "base_date": "1975-01-01",
                        "n_series": len(all_meta),
                        "series": all_meta,
                    }
                ),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
        log.info("escrito -> %s (%d filas, %d series)", csv_path, len(all_rows), len(all_meta))
    else:
        log.info(
            "añada histórica %s archivada en el ledger (%d series); web_forecasts.csv intacto", as_of, len(all_meta)
        )
    return csv_path, meta_path


if __name__ == "__main__":
    import sys

    # Uso: python experiments/generate_web_forecasts.py [YYYY-MM]
    # Sin arg → añada en vivo (sirve la web). Con arg → añada histórica para evaluación.
    run(sys.argv[1] if len(sys.argv) > 1 else None)
