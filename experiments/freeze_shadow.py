"""Shadow deployment of the best challenger (AO6): freeze its vintage next to the champion's.

The promotion gate (``run_champion_challenger.py``) demands PROSPECTIVE confirmation, but the
prospective ledger only ever graded the deployed champion — the gate had no data to confirm
against. This script closes that loop cheaply: it reads the latest verdict
(``reports/governance/champion_challenger.json``), picks the best challenger per table (the
``promote`` entry if any, else the lowest-MASE challenger whose recipe differs from the
champion's), re-fits its recipe (statistical models — minutes) and freezes a full 12-month
vintage with 80/95 % conformal bands into an append-only shadow ledger:

    reports/prospective/forecast_log_shadow.csv   (shadow=true + serialized recipe per row)

Why a SEPARATE file instead of shadow-tagged rows inside ``forecast_log.csv``: the champion
ledger's idempotency key is (origin, series, target date) with ``keep="first"``
(``generate_web_forecasts._append_log``) — shadow rows for the same origin/series/date would
collide with the champion's and one of the two would be silently dropped on the next append.
A separate ledger is immutable under the same ``keep="first"`` rule and keeps the champion
scorecard uncontaminated BY CONSTRUCTION (``score_forecasts.py`` reads ``forecast_log.csv``
only — verified; scoring the shadow vintage is follow-up work in that script).

Runs in ``ante`` from the repo root:  ante/bin/python experiments/freeze_shadow.py
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments import generate_web_forecasts as gwf  # noqa: E402 — band method single-source
from vp_data import tracking  # noqa: E402
from vp_model import champion, config, dataset, intervals, metrics, models  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
SHADOW_LOG = REPORTS / "prospective" / "forecast_log_shadow.csv"
VERDICT = REPORTS / "governance" / "champion_challenger.json"
WEB_META = REPORTS / "prospective" / "web_forecasts_meta.json"
HORIZON = 12
SHADOW_KEYS = ["origin", "country", "category", "table", "date"]

log = config.get_logger("freeze_shadow")


def best_challenger(entry: dict) -> dict | None:
    """Best challenger recipe (serialized) for one table's verdict entry — pure, testable.

    Preference: the gated ``promote`` winner; otherwise the lowest-MASE challenger whose
    display name differs from the champion's (shadowing the champion itself is pointless).
    Returns the serialized recipe dict, or None when there is nothing worth shadowing.
    """
    promote = entry.get("promote")
    if promote and promote.get("recipe"):
        return promote["recipe"]
    for row in sorted(entry.get("challengers", []), key=lambda r: r.get("mean", float("inf"))):
        if row.get("challenger") != entry.get("champion") and row.get("recipe"):
            return row["recipe"]
    return None


def _point(values: list[np.ndarray], agg: str) -> np.ndarray:
    """Elementwise recipe aggregation (unlike the web generator, honors agg="mean")."""
    stack = np.vstack(values)
    return np.mean(stack, axis=0) if agg == "mean" else np.median(stack, axis=0)


def _series_shadow(recipe: champion.Recipe, country: str, category: str, table: str) -> list[dict]:
    """12-month shadow vintage for one series: recipe point + split-conformal 80/95 bands.

    Mirrors the deployed champion's method (1-step hold-out walk-forward calibrates the
    conformal half-width on F-only dates; per-horizon empirical quantile bands from the
    prospective ledger, sqrt(h) only as documented fallback) so shadow and champion
    vintages are directly comparable once both are scored against realized cutoffs.
    """
    fseries = dataset.load_series(country, category, table)
    ts = models.to_timeseries(fseries)
    if len(ts) < config.MIN_TRAIN[table] + config.HOLDOUT + config.MIN_BACKTEST_BUFFER:
        raise ValueError(f"serie demasiado corta ({len(ts)})")
    origin = ts.end_time().strftime("%Y-%m")
    split = ts.time_index[-config.HOLDOUT]
    preds = {}
    for name in recipe.models:
        m = models.build_model(name)
        preds[name] = m.historical_forecasts(  # type: ignore[attr-defined]
            ts, start=split, forecast_horizon=1, stride=1, retrain=True, last_points_only=True, verbose=False
        )
    common = preds[recipe.models[0]].time_index
    for p in preds.values():
        common = common.intersection(p.time_index)
    actual = ts.slice_intersect(preds[recipe.models[0]]).to_series().reindex(common)
    hold_point = _point([p.to_series().reindex(common).to_numpy() for p in preds.values()], recipe.agg)
    from darts import TimeSeries

    hold_ts = TimeSeries.from_series(pd.Series(hold_point, index=common))
    actual_ts = TimeSeries.from_series(actual)
    # F-only calibration (B1): interpolated months have artificially small residuals.
    half95 = (
        (intervals.conformal(hold_ts, actual_ts, hold_ts, alpha=0.05, calib_dates=fseries.index).upper - hold_ts)
        .values()
        .flatten()[0]
    )
    insample = ts.split_before(split)[0]
    # B1: the frozen hold_mase must use the same F-mask + raw-scale convention as
    # every other published number (audit: it was scored on interpolated months).
    mase = float(
        metrics.compute(
            actual_ts,
            hold_ts,
            insample,
            dates=fseries.index,
            scale=metrics.naive_scale_before(fseries, split),
        ).get("mase", float("nan"))
    )

    fut = []
    for name in recipe.models:
        m = models.build_model(name)
        m.fit(ts)  # statistical recipes need no covariates (same as the deployed champion)
        fut.append(m.predict(HORIZON).to_series().to_numpy())
    point = _point(fut, recipe.agg)
    future_idx = pd.date_range(ts.end_time() + ts.freq, periods=HORIZON, freq=ts.freq)
    rows = []
    scales = gwf._load_pi_scales()
    for h, (d, pv) in enumerate(zip(future_idx, point, strict=True), start=1):
        # Method-identical to the deployed champion (audit: the shadow froze the
        # dead sqrt-h heuristic while the champion moved to per-horizon quantiles,
        # confounding any shadow-vs-champion coverage comparison).
        half80, half95_h, method = gwf._band_halfwidths(h, half95, table, scales)
        rows.append(
            {
                "origin": origin,
                "h": h,
                "country": country,
                "category": category,
                "table": table,
                "date": d.strftime("%Y-%m-%d"),
                "days": int(round(pv)),
                "lo80": int(round(pv - half80)),
                "hi80": int(round(pv + half80)),
                "lo95": int(round(pv - half95_h)),
                "hi95": int(round(pv + half95_h)),
                "shadow": True,
                "recipe": recipe.name,
                "hold_mase": round(mase, 4),
                "band_method": method,
            }
        )
    return rows


def append_shadow(rows: list[dict]) -> Path:
    """Append to the immutable shadow ledger — keep="first" on (origin, series, date).

    Same C3 contract as the champion ledger: a frozen shadow forecast is NEVER
    overwritten; re-runs within the same vintage are no-ops.
    """
    SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows)
    combined = pd.concat([pd.read_csv(SHADOW_LOG), new], ignore_index=True) if SHADOW_LOG.exists() else new
    combined = combined.drop_duplicates(subset=SHADOW_KEYS, keep="first").sort_values(SHADOW_KEYS)
    combined.to_csv(SHADOW_LOG, index=False)
    return SHADOW_LOG


def main() -> int:
    warnings.filterwarnings("ignore")
    config.seed_everything()
    if not VERDICT.exists():
        log.warning("no hay %s — corre run_champion_challenger primero; nada que congelar", VERDICT)
        return 0
    verdict = json.loads(VERDICT.read_text())
    champions = champion.load_manifest()
    all_rows: list[dict] = []
    n_series = 0
    for table in config.TABLES:
        entry = verdict.get(table)
        if not entry:
            continue
        rec_dict = best_challenger(entry)
        if rec_dict is None:
            log.info("[%s] verdict sin recetas serializadas de retador — nada que sombrear", table)
            continue
        recipe = champion.recipe_from_dict(rec_dict)
        if any(m in config.DIFFERENCED for m in recipe.models):
            # ponytail: covariate plumbing for GBM recipes isn't wired here yet —
            # skip LOUDLY instead of dying per-series and aborting the vintage.
            # Upgrade path: FeatureBuilder covariates over an extended calendar.
            log.warning("[%s] retador %s incluye GBM — sombra no soportada aún, se omite", table, recipe.name)
            continue
        if recipe.name == champions[table].name:
            log.info("[%s] el mejor retador ES el campeón desplegado — no se sombrea", table)
            continue
        log.info("[%s] sombra = %s", table, recipe.name)
        for block in ("family", "employment"):
            cat = dataset.list_series(table=table, block=block, countries=config.PILOT_COUNTRIES)
            for r in cat.itertuples():
                try:
                    all_rows += _series_shadow(recipe, r.country, r.category, table)
                    n_series += 1
                except Exception as e:  # noqa: BLE001 — one failing series must not kill the vintage
                    log.info("skip %s/%s/%s: %s", table, r.country, r.category, e)
        tracking.log_run(
            "shadow_forecasts",
            f"{table}-{recipe.name}",
            params={"table": table, "recipe": recipe.name, "models": "+".join(recipe.models), "agg": recipe.agg},
            metrics={"n_rows": float(sum(1 for x in all_rows if x["table"] == table))},
            tags={"shadow": "true"},
        )
    if not all_rows:
        log.info("sin filas sombra este run (sin retador distinto o todas las series fallaron)")
        return 0
    # C2-style exit gate PER TABLE (audit: the global gate compared one table's
    # series count against BOTH tables' expectation and aborted valid freezes).
    if WEB_META.exists():
        meta_series = json.loads(WEB_META.read_text()).get("series", {})
        for table in config.TABLES:
            expected = sum(1 for k in meta_series if k.endswith(f"/{table}"))
            got = len({(r["country"], r["category"]) for r in all_rows if r["table"] == table})
            if got and expected and got < 0.9 * expected:
                log.warning("[%s] solo %d/%d series sombra (<90%%) — se descarta la tabla", table, got, expected)
                all_rows = [r for r in all_rows if r["table"] != table]
    if not all_rows:
        log.info("todas las tablas descartadas por el gate — nada que archivar")
        return 0
    path = append_shadow(all_rows)
    log.info("shadow ledger -> %s (+%d filas de %d series)", path, len(all_rows), n_series)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
