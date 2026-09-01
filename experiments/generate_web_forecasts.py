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

Cono de coherencia (AL5/F1): la añada completa se PROYECTA al cono de orden
(país ≤ all_chargeability, luego FAD ≤ DFF) ANTES de serializarse — tanto el ledger
como el CSV que sirve la web congelan la añada YA proyectada. El punto se proyecta
(isotónica min/max, ``vp_model.cone``) y las bandas se desplazan con él preservando
su ancho calibrado; los contadores pre/post quedan en el meta JSON
(``cone_violations_pre``/``cone_violations_post``) para el correo SES y los gates.

Salidas (tidy, versionadas en git como el resto de reports/):
  • reports/prospective/web_forecasts.csv       — country,category,table,date,days,lo80,hi80,lo95,hi95
  • reports/prospective/web_forecasts_meta.json — método + métricas hold-out por serie (procedencia)

Tracking MLflow vía ``tracking.log_run`` (experimento "web_forecasts") es para **desarrollo
local**; en CI el staging es efímero — el registro DURABLE de procedencia es el CSV/JSON
commiteado en git + el git_sha que ``tracking`` graba en cada record.

Corre en ``ante`` desde la raíz:  ante/bin/python experiments/generate_web_forecasts.py
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from darts import TimeSeries

from vp_data import tracking
from vp_model import champion, cone, config, dataset, intervals, ledger, metrics, models

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
HORIZON = 12
ACI_MIN_HITS = 8  # minimum scored ledger rows for a series before ACI kicks in (AN4)
ACI_GAMMA_DEFAULT = 0.05
log = config.get_logger("web_forecasts")

# Bootstrap de la primera añada que separa catálogo de universo modelable. Después
# del primer éxito, ``web_forecasts_meta.json`` conserva las claves completas y la
# comparación es por inclusión de sets, no solo por conteo. Los pisos evitan que la
# migración inicial selle como autoridad un parser ya contraído.
BOOTSTRAP_MIN_CATALOG = {"FAD": 105, "DFF": 89}
BOOTSTRAP_MIN_ELIGIBLE = {"FAD": 56, "DFF": 41}
ELIGIBILITY_SCHEMA = 1
SARIMA_STABILIZATION = "sarima_relaxed_stationarity_invertibility"


@dataclass(frozen=True)
class SeriesEligibility:
    """Clasificación estructural previa e independiente del ajuste del modelo."""

    key: str
    table: str
    n_obs: int
    min_required: int
    eligible: bool
    reason: str | None


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


def _prepare_series(country: str, category: str, table: str, as_of: str | None = None):
    """Carga y regulariza una serie; devuelve ``(ts, raw, razón estructural)``.

    Es la fuente única del criterio de suficiencia que comparten el universo esperado
    y el ajuste real. Los errores de lectura/parseo se propagan: solo ausencia legítima
    de F, origen fuera de rango e historia corta son inelegibilidad estructural.
    """

    try:
        raw = dataset.load_series(country, category, table).astype("float64")
    except KeyError as exc:
        # ``dataset.load_series`` reserva KeyError para la serie catalogada sin una
        # sola observación F. No se engloban errores de parseo/DB: esos se propagan.
        if "Serie vacía:" not in str(exc):
            raise
        raw = pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="month"))
    minimum = config.MIN_TRAIN[table] + config.HOLDOUT + config.MIN_BACKTEST_BUFFER
    if raw.empty:
        return None, raw, "no_f_observations"
    ts = models.to_timeseries(raw)
    if as_of is not None:
        cut = pd.Timestamp(as_of) + pd.offsets.MonthBegin(1)
        if not (ts.start_time() < cut <= ts.end_time() + ts.freq):
            return None, raw, "as_of_out_of_range"
        ts = ts.drop_after(cut)
        raw = raw[raw.index < cut]
    if len(ts) < minimum:
        return ts, raw, "too_short"
    return ts, raw, None


def _series_eligibility(country: str, category: str, table: str, as_of: str | None = None) -> SeriesEligibility:
    ts, _raw, reason = _prepare_series(country, category, table, as_of)
    n_obs = len(ts) if ts is not None else 0
    minimum = config.MIN_TRAIN[table] + config.HOLDOUT + config.MIN_BACKTEST_BUFFER
    return SeriesEligibility(
        key=f"{country}/{category}/{table}",
        table=table,
        n_obs=n_obs,
        min_required=minimum,
        eligible=reason is None,
        reason=reason,
    )


def _build_model(name: str, table: str, *, relaxed_sarima: bool):
    if name == "sarima" and relaxed_sarima:
        return models.build_relaxed_sarima()
    return models.build_model(name, table=table)


def _holdout_preds(
    model_set: tuple[str, ...],
    country: str,
    category: str,
    table: str,
    as_of: str | None = None,
    *,
    relaxed_sarima: bool = False,
):
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
    ts, raw, structural_reason = _prepare_series(country, category, table, as_of)
    if structural_reason is not None or ts is None:
        raise ValueError(f"serie estructuralmente no elegible ({structural_reason})")
    split = ts.time_index[-config.HOLDOUT]
    preds: dict[str, TimeSeries] = {}
    for name in model_set:
        m = _build_model(name, table, relaxed_sarima=relaxed_sarima)
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
    """Error boundary observable, con un único segundo intento numérico gobernado.

    La elegibilidad estructural ya se resolvió antes de llegar aquí. Un ``LinAlgError``
    de una receta que contiene SARIMA se reintenta relajando únicamente la
    inicialización de estacionariedad/invertibilidad; cualquier otro fallo permanece
    ausente y el gate de igualdad decide fail-closed o excepción nominal.
    """
    key = f"{country}/{category}/{table}"
    relaxed_sarima = False
    while True:
        try:
            result = _compute_series_forecast(
                country,
                category,
                table,
                as_of,
                prod,
                pi_scales,
                aci_gamma,
                hits,
                relaxed_sarima=relaxed_sarima,
            )
            if relaxed_sarima:
                rows, meta = result
                meta[key]["numerical_stabilization"] = SARIMA_STABILIZATION
                return rows, meta
            return result
        except np.linalg.LinAlgError as exc:
            if relaxed_sarima or "sarima" not in prod[table]:
                log.warning("skip %s: %s: %s", key, type(exc).__name__, exc)
                return None
            relaxed_sarima = True
            log.warning("retry %s con %s tras %s: %s", key, SARIMA_STABILIZATION, type(exc).__name__, exc)
        except Exception as exc:  # noqa: BLE001 — robustez: una serie que falla no tumba la corrida
            log.warning("skip %s: %s: %s", key, type(exc).__name__, exc)
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
    *,
    relaxed_sarima: bool = False,
) -> tuple[list[dict], dict]:
    model_set = prod[table]
    ts, raw, hold_preds = _holdout_preds(
        model_set,
        country,
        category,
        table,
        as_of,
        relaxed_sarima=relaxed_sarima,
    )
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
        m = _build_model(name, table, relaxed_sarima=relaxed_sarima)
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
                # Era tag for the immutable ledger: derive_band80_ratio must never
                # de-standardize q_h rows by sqrt(h) (audit: era-mixing bomb).
                "band_method": band_method,
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


def _project_rows(rows: list[dict]) -> tuple[list[dict], dict]:
    """AL5/F1: proyecta la añada al cono (país≤AllCharg, FAD≤DFF) antes de serializar.

    Single-source en ``vp_model.cone`` (la misma proyección que audita
    ``apply_cone_constraints``): punto proyectado por isotónica min/max y bandas
    DESPLAZADAS con el punto (ancho calibrado preservado). Los valores publicados
    son enteros (días); la proyección mueve un entero a otro entero, así que el
    redondeo de vuelta es sin pérdida y, sin violaciones, el passthrough es
    byte-estable. Devuelve ``(filas, contadores pre/post)`` para el meta/SES.
    """
    frame, counters = cone.project(pd.DataFrame(rows))
    if counters["cone_violations_pre"]:  # _shift_row pudo promover a float; restaurar int
        for col in ("days", *cone.BAND_COLS):
            frame[col] = frame[col].round().astype("int64")
    return frame.to_dict("records"), counters


def _keys_by_table(keys: set[str]) -> dict[str, set[str]]:
    return {table: {key for key in keys if key.endswith(f"/{table}")} for table in config.TABLES}


def _keys_digest(keys: set[str]) -> str:
    payload = json.dumps(sorted(keys), ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _forecast_universe(
    as_of: str | None = None,
) -> tuple[dict[str, tuple[str, str, str]], set[str], dict[str, SeriesEligibility]]:
    """Devuelve catálogo, elegibles e inelegibles antes de ajustar ningún modelo."""

    catalogue: dict[str, tuple[str, str, str]] = {}
    eligible: set[str] = set()
    structural: dict[str, SeriesEligibility] = {}
    for table in config.TABLES:
        for block in ("family", "employment"):
            cat = dataset.list_series(table=table, block=block, countries=config.PILOT_COUNTRIES)
            for row in cat.itertuples():
                key = f"{row.country}/{row.category}/{table}"
                if key in catalogue:
                    raise SystemExit(f"ABORT (catálogo duplicado): {key}")
                catalogue[key] = (row.country, row.category, table)
                item = _series_eligibility(row.country, row.category, table, as_of)
                if item.eligible:
                    eligible.add(key)
                else:
                    structural[key] = item
    return catalogue, eligible, structural


def _eligibility_payload(
    catalogue: set[str],
    eligible: set[str],
    structural: dict[str, SeriesEligibility],
) -> dict:
    """Snapshot derivado que vuelve ruidosa toda contracción en la próxima añada."""

    cat_by = _keys_by_table(catalogue)
    eligible_by = _keys_by_table(eligible)
    return {
        "schema_version": ELIGIBILITY_SCHEMA,
        "criterion": {
            table: config.MIN_TRAIN[table] + config.HOLDOUT + config.MIN_BACKTEST_BUFFER for table in config.TABLES
        },
        "catalogue": {
            table: {"count": len(cat_by[table]), "keys": sorted(cat_by[table]), "sha256": _keys_digest(cat_by[table])}
            for table in config.TABLES
        },
        "eligible": {
            table: {
                "count": len(eligible_by[table]),
                "keys": sorted(eligible_by[table]),
                "sha256": _keys_digest(eligible_by[table]),
            }
            for table in config.TABLES
        },
        # No se mantiene una allowlist de las series estructurales: se vuelven a
        # derivar del panel y solo se conserva el censo por causa para auditoría.
        "structural_ineligible": {
            table: {
                "count": sum(item.table == table for item in structural.values()),
                "reasons": dict(
                    sorted(Counter(item.reason for item in structural.values() if item.table == table).items())
                ),
            }
            for table in config.TABLES
        },
    }


def _validated_key_group(raw: object, *, label: str) -> set[str]:
    if not isinstance(raw, dict) or set(raw) != {"count", "keys", "sha256"}:
        raise ValueError(f"eligibility anterior: {label} con schema abierto/incompleto")
    keys = raw["keys"]
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
        raise ValueError(f"eligibility anterior: {label}.keys inválidas/duplicadas")
    key_set = set(keys)
    if type(raw["count"]) is not int or raw["count"] != len(key_set):
        raise ValueError(f"eligibility anterior: {label}.count no coincide")
    if raw["sha256"] != _keys_digest(key_set):
        raise ValueError(f"eligibility anterior: {label}.sha256 no re-deriva")
    return key_set


def _previous_universe(meta_path: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Carga el baseline anterior; el meta legado usa sus series publicadas como piso."""

    empty: dict[str, set[str]] = {table: set() for table in config.TABLES}
    if not meta_path.exists():
        return empty, {table: set() for table in config.TABLES}
    raw = json.loads(meta_path.read_text())
    eligibility = raw.get("eligibility")
    if eligibility is None:
        series = raw.get("series")
        if not isinstance(series, dict) or not all(isinstance(key, str) for key in series):
            raise ValueError("meta anterior sin eligibility ni series válidas para migrar")
        previous = _keys_by_table(set(series))
        return {table: set() for table in config.TABLES}, previous
    if not isinstance(eligibility, dict) or set(eligibility) != {
        "schema_version",
        "criterion",
        "catalogue",
        "eligible",
        "structural_ineligible",
    }:
        raise ValueError("eligibility anterior con schema abierto/incompleto")
    if type(eligibility["schema_version"]) is not int or eligibility["schema_version"] != ELIGIBILITY_SCHEMA:
        raise ValueError("eligibility anterior con schema_version inválido")
    criterion = eligibility["criterion"]
    catalogue_raw = eligibility["catalogue"]
    eligible_raw = eligibility["eligible"]
    structural_raw = eligibility["structural_ineligible"]
    if not all(isinstance(part, dict) for part in (criterion, catalogue_raw, eligible_raw, structural_raw)):
        raise ValueError("eligibility anterior con secciones no-objeto")
    expected_tables = set(config.TABLES)
    if any(set(part) != expected_tables for part in (criterion, catalogue_raw, eligible_raw, structural_raw)):
        raise ValueError("eligibility anterior sin las tablas canónicas exactas")
    for table in config.TABLES:
        expected_minimum = config.MIN_TRAIN[table] + config.HOLDOUT + config.MIN_BACKTEST_BUFFER
        if type(criterion[table]) is not int or criterion[table] != expected_minimum:
            raise ValueError(f"eligibility anterior: criterion.{table} no coincide con la política actual")
    previous_catalogue = {
        table: _validated_key_group(catalogue_raw[table], label=f"catalogue.{table}") for table in config.TABLES
    }
    previous_eligible = {
        table: _validated_key_group(eligible_raw[table], label=f"eligible.{table}") for table in config.TABLES
    }
    for table in config.TABLES:
        if not previous_eligible[table] <= previous_catalogue[table]:
            raise ValueError(f"eligibility anterior: eligible.{table} no es subconjunto del catálogo")
        if any(not key.endswith(f"/{table}") for key in previous_catalogue[table]):
            raise ValueError(f"eligibility anterior: catalogue.{table} contiene claves de otra tabla")
        group = structural_raw[table]
        if not isinstance(group, dict) or set(group) != {"count", "reasons"}:
            raise ValueError(f"eligibility anterior: structural_ineligible.{table} con schema inválido")
        count, reasons = group["count"], group["reasons"]
        if (
            type(count) is not int
            or count < 0
            or not isinstance(reasons, dict)
            or not all(
                isinstance(reason, str) and reason and type(n) is int and n >= 0 for reason, n in reasons.items()
            )
            or sum(reasons.values()) != count
        ):
            raise ValueError(f"eligibility anterior: structural_ineligible.{table} no re-deriva")
        if len(previous_catalogue[table]) != len(previous_eligible[table]) + count:
            raise ValueError(f"eligibility anterior: censo {table} no particiona el catálogo")
    return previous_catalogue, previous_eligible


def _universe_problems(
    catalogue: set[str],
    eligible: set[str],
    previous_catalogue: dict[str, set[str]],
    previous_eligible: dict[str, set[str]],
) -> list[str]:
    cat_by = _keys_by_table(catalogue)
    eligible_by = _keys_by_table(eligible)
    problems: list[str] = []
    for table in config.TABLES:
        cat_floor = BOOTSTRAP_MIN_CATALOG.get(table, 0)
        eligible_floor = BOOTSTRAP_MIN_ELIGIBLE.get(table, 0)
        if len(cat_by[table]) < cat_floor:
            problems.append(f"{table}: catálogo {len(cat_by[table])} < piso gobernado {cat_floor}")
        if len(eligible_by[table]) < eligible_floor:
            problems.append(f"{table}: elegibles {len(eligible_by[table])} < piso gobernado {eligible_floor}")
        removed_catalogue = sorted(previous_catalogue.get(table, set()) - cat_by[table])
        removed_eligible = sorted(previous_eligible.get(table, set()) - eligible_by[table])
        if removed_catalogue:
            problems.append(f"{table}: contracción del catálogo: {removed_catalogue[:8]}")
        if removed_eligible:
            problems.append(f"{table}: contracción del universo elegible: {removed_eligible[:8]}")
    return problems


def _meta_payload(method: dict, all_meta: dict, cone_meta: dict, eligibility: dict | None = None) -> dict:
    """Payload del meta JSON del publicador (testeable sin correr la añada completa).

    Expone los contadores del cono (``cone_violations_pre``/``post`` + desglose) como
    métrica de primera clase: el correo SES y los gates los vigilan por añada.
    """
    payload = {
        "method": method,
        "horizon_months": HORIZON,
        "base_date": config.BASE_EPOCH,
        "n_series": len(all_meta),
        # AL5/F1: proyección al cono incorporada al publicador — el pre es la métrica
        # vigilada (SES/gates); el post se mide tras ambas pasadas, no se asume.
        "cone_policy": "punto proyectado (isotonica min/max); bandas desplazadas con el punto",
        "cone_violations_pre": cone_meta["cone_violations_pre"],
        "cone_violations_post": cone_meta["cone_violations_post"],
        "cone_violations_detail": cone_meta["cone_violations_detail"],
        "series": all_meta,
    }
    if eligibility is not None:
        payload["eligibility"] = eligibility
    return payload


WEB_COLS = ["country", "category", "table", "date", "days", "lo80", "hi80", "lo95", "hi95"]
LOG_COLS = ["origin", "h", *WEB_COLS, "band_method", *ledger.V2_COLS, "deployment_id", "pipeline_run_id"]
LOG_KEYS = ledger.KEYS


def _append_log(rows: list[dict], model_version: str | dict[str, str] | None = "n/d", as_of: str | None = None) -> Path:
    """Anexa la añada al ledger append-only ``reports/prospective/forecast_log.csv`` (idempotente
    por (origin, serie, fecha-objetivo)). Es el registro inmutable de lo que
    predijimos y desde cuándo — base de la evaluación prospectiva (``score_forecasts``).

    C3: ``keep="first"`` — un pronóstico ya congelado NUNCA se sobrescribe. Con
    ``keep="last"`` un re-run (código/semilla distinta) reemplazaba añadas ya
    archivadas e invalidaba la evaluación prospectiva.

    A2: cada fila nueva se sella con la identidad de freeze v2 (``vp_model.ledger``):
    ``frozen_at``/``panel_hash``/``git_sha``/``model_version`` + ``evaluation_mode``
    (``live`` solo si el target era desconocido al vintage del panel; todo ``as_of``
    explícito es ``backfill``)."""
    log_path = REPORTS / "prospective" / "forecast_log.csv"
    stamped = ledger.stamp_rows(rows, model_version, as_of=as_of)
    combined = ledger.append(log_path, stamped, cols=LOG_COLS)
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
    manifest = champion.load_manifest()
    prod: dict[str, tuple[str, ...]] = {t: r.models for t, r in manifest.items()}
    pi_scales = _load_pi_scales()
    aci_gamma = _load_aci_gamma()
    hits = _ledger_hits()
    csv_path = REPORTS / "prospective" / "web_forecasts.csv"
    meta_path = REPORTS / "prospective" / "web_forecasts_meta.json"
    catalogue, expected_keys, structural = _forecast_universe(as_of)
    eligibility_snapshot = _eligibility_payload(set(catalogue), expected_keys, structural)
    if as_of is None:
        previous_catalogue, previous_eligible = _previous_universe(meta_path)
        universe_problems = _universe_problems(
            set(catalogue),
            expected_keys,
            previous_catalogue,
            previous_eligible,
        )
        if universe_problems:
            raise SystemExit("ABORT (contracción del universo de forecasts): " + " | ".join(universe_problems))
    for table in config.TABLES:
        table_catalogue = sum(key.endswith(f"/{table}") for key in catalogue)
        table_eligible = sum(key.endswith(f"/{table}") for key in expected_keys)
        table_structural = Counter(item.reason for item in structural.values() if item.table == table)
        log.info(
            "universo %s: catálogo=%d elegibles=%d estructurales=%d (%s)",
            table,
            table_catalogue,
            table_eligible,
            table_catalogue - table_eligible,
            dict(sorted(table_structural.items())),
        )
    all_rows: list[dict] = []
    all_meta: dict = {}
    for key, (country, category, table) in catalogue.items():
        if key not in expected_keys:
            continue
        out = _series_forecast(country, category, table, as_of, prod, pi_scales, aci_gamma, hits)
        if out is None:
            continue
        rows, meta = out
        all_rows += rows
        all_meta.update(meta)
        log.info("✓ %s (%d series acumuladas)", key, len(all_meta))

    # C2 + A-05 (auditoria ciega 11-jul): gate de salida por TABLA y SET DE CLAVES contra
    # el catalogo VIGENTE (antes: n_series del meta del run ANTERIOR con 10% de tolerancia
    # global — una tabla completa ausente pasaba si la otra producia filas). Un env roto a
    # medias NO publica ni archiva: el ledger es inmutable (C3).
    got_keys = set(all_meta)
    allowed = ledger.load_completeness_allowlist()
    problems: list[str] = []
    for table in config.TABLES:
        exp_t = {k for k in expected_keys if k.endswith(f"/{table}")}
        got_t = {k for k in got_keys if k.endswith(f"/{table}")}
        problems += ledger.completeness_problems(exp_t, got_t, label=table, allowed=allowed)
        for k in sorted(exp_t - got_t):
            if k in allowed:  # eximida NOMINALMENTE — visible en log Y en el correo SES
                log.warning("[%s] omision permitida por allowlist: %s (%s)", table, k, allowed[k])
                with open("/tmp/completeness.txt", "a") as fh:
                    fh.write(f"[{table}] omitida con excepcion nominal: {k} ({allowed[k]})\n")
    if problems:
        raise SystemExit("ABORT (completitud fail-closed): " + " | ".join(problems))

    # AL5/F1: proyección al cono de coherencia ANTES de serializar — el ledger congela
    # exactamente la añada que se publica (misma proyección para punto y bandas; ver
    # _project_rows). El contador pre-proyección es la métrica que vigilan meta/SES.
    all_rows, cone_meta = _project_rows(all_rows)
    log.info(
        "cono de coherencia: %d violaciones pre-proyección -> %d post (detalle: %s)",
        cone_meta["cone_violations_pre"],
        cone_meta["cone_violations_post"],
        cone_meta["cone_violations_detail"],
    )

    # A2: la añada se archiva con identidad de freeze — receta desplegada por tabla y
    # modo honesto (as_of explícito ⇒ backfill; en vivo, live solo si el target es futuro).
    log_path = _append_log(all_rows, model_version={t: r.name for t, r in manifest.items()}, as_of=as_of)
    # A-05: validar el ledger PERSISTIDO inmediatamente tras el append — una violacion
    # del contrato v2 (sello nulo, hash que no re-deriva, live imposible) impide publicar.
    violations = ledger.validate(pd.read_csv(log_path))
    if violations:
        raise SystemExit("ABORT (ledger campeon viola el contrato v2 tras el append): " + "; ".join(violations))
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
                _no_nan(_meta_payload(method, all_meta, cone_meta, eligibility_snapshot)),
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
