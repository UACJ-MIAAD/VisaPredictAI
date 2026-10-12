"""Probabilistic evaluation of the DEPLOYED champion recipe: Vincentization + CRPS (AN6).

The deployed recipe (FAD: median of {theta, ets, sarima}; DFF: sarima — read live from the
champion manifest) had NO probabilistic evaluation: CRPS existed for individual models, never
for the served ensemble. This script builds an ensemble predictive distribution per series by
**Vincentization** (quantile averaging — the standard way to combine forecast distributions,
cf. Lichtendahl et al. 2013) over the recipe members on the 24-month hold-out:

  * probabilistic members (ARIMA/SARIMA — ``config.PROBABILISTIC``): native sampling via a
    1-step walk-forward over the hold-out with ``--num-samples`` draws; member quantiles are
    read off the empirical sample distribution per step.
  * deterministic members (ETS/Theta): the walk-forward point forecast plus empirical
    quantiles of the SIGNED 1-step residuals from the selection region (F-only, leakage-free
    — the conformal-style calibration used by the web demo, but per-quantile).

The ensemble quantile function is the per-level mean of member quantiles; CRPS is then
**approximated as 2 x the mean pinball loss over the quantile grid** (quadrature of the
CRPS integral over QUANTS; the same convention as ``eval_deep_pi.py``, so numbers are
comparable). Coverage of the ensemble 95% band ([q0.025, q0.975]) is reported with a
Jeffreys CI and n, flagged below the n>=30 floor (AN7). All scoring is F-only (B1).

Output: ``reports/eval/crps_champion.csv`` — one row per series plus one "ALL" aggregate row
per table (mean CRPS, pooled coverage + CI).

Runs in ``ante`` from the repo root:
  ante/bin/python experiments/run_champion_crps.py [--tables FAD,DFF] [--block family] [--limit N]
  ante/bin/python experiments/run_champion_crps.py --complement

★ M74-F · C5: with ``--complement`` it runs inside a running ``champion_crps`` complement
(`vp_model.complement`): only the full protocol (`champion.CRPS_PROTOCOLO`), preconditions checked before
computing and again before sealing, and the receipt carries the complement's identity plus every skipped
series with its reason. Without the flag, a tree with a complement refuses to run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import config, dataset, intervals, models, walkforward
from vp_model.champion import CRPS_PROTOCOLO, CRPS_QUANTS

REPORTS = Path(__file__).resolve().parent.parent / "reports"
OUT = REPORTS / "eval" / "crps_champion.csv"
QUANTS = CRPS_QUANTS
N_FLOOR = 30
log = config.get_logger("champion_crps")


def _pinball(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    e = y - q
    return float(np.mean(np.maximum(tau * e, (tau - 1) * e)))


def _member_quantiles(name: str, country: str, category: str, table: str, num_samples: int) -> dict[float, pd.Series]:
    """Per-quantile hold-out forecast series of one recipe member (indexed by month).

    Probabilistic members sample natively; deterministic members add empirical signed
    residual quantiles (selection region, F-only) to their walk-forward point forecasts.
    """
    raw = dataset.load_series(country, category, table).astype("float64")
    ts = models.to_timeseries(raw)
    split = ts.time_index[-config.HOLDOUT]
    if name in config.PROBABILISTIC:
        m = models.build_model(name)
        samples = m.historical_forecasts(  # type: ignore[attr-defined]
            ts,
            start=split,
            forecast_horizon=1,
            stride=1,
            retrain=True,
            last_points_only=True,
            verbose=False,
            num_samples=num_samples,
        )
        return {q: samples.quantile(q).to_series() for q in QUANTS}
    # deterministic member: full walk-forward -> selection residuals calibrate the quantiles
    _ts2, forecasts = walkforward.run_forecasts(name, country, category, table)
    sel_fc, hold_fc = forecasts.split_before(split)
    sel = sel_fc.to_series()
    sel_actual = raw.reindex(sel.index).dropna()  # F-only selection actuals (B1)
    resid = (sel_actual - sel.reindex(sel_actual.index)).to_numpy()
    if len(resid) < 8:
        raise ValueError(f"{name}: only {len(resid)} F residuals in selection region")
    hold = hold_fc.to_series()
    return {q: hold + float(np.quantile(resid, q)) for q in QUANTS}


def _score_series(
    recipe: tuple[str, ...], country: str, category: str, table: str, num_samples: int
) -> tuple[dict | None, str | None]:
    """CRPS + 95% coverage of the Vincentized champion on one series: ``(row, None)`` or ``(None, reason)``."""
    try:
        member_qs = [_member_quantiles(m, country, category, table, num_samples) for m in recipe]
        raw = dataset.load_series(country, category, table).astype("float64")
        idx = member_qs[0][QUANTS[0]].index
        for mq in member_qs[1:]:
            idx = idx.intersection(mq[QUANTS[0]].index)
        idx = idx.intersection(raw.index)  # F-only scoring dates (B1)
        if len(idx) == 0:
            raise ValueError("no F observation in the hold-out window")
        y = raw.reindex(idx).to_numpy()
        # Vincentization: ensemble quantile = mean of member quantiles, level by level.
        ens = {q: np.mean(np.vstack([mq[q].reindex(idx).to_numpy() for mq in member_qs]), axis=0) for q in QUANTS}
        crps = 2.0 * float(np.mean([_pinball(y, ens[q], q) for q in QUANTS]))
        hits = (y >= ens[0.025]) & (y <= ens[0.975])
        return {
            "table": table,
            "country": country,
            "category": category,
            "n_holdout_F": int(len(y)),
            "crps": round(crps, 2),
            "cov95": round(float(hits.mean()), 4),
            "k95": int(hits.sum()),
        }, None
    except Exception as e:  # noqa: BLE001 — one failing series must not abort the run
        log.info("skip %s/%s/%s: %s", country, category, table, e)
        return None, f"{type(e).__name__}: {e}"[:300]


def run(
    tables: tuple[str, ...], block: str, limit: int | None, num_samples: int, *, complement: bool = False
) -> pd.DataFrame:
    from vp_model import champion

    manifest = champion.load_manifest()
    config.seed_everything()
    rows: list[dict] = []
    skipped: list[dict] = []
    for table in tables:
        recipe = manifest[table].models
        cat = dataset.list_series(table=table, block=block, countries=config.PILOT_COUNTRIES)
        if limit:
            cat = cat.head(limit)
        per_table: list[dict] = []
        for r in cat.itertuples():
            out, reason = _score_series(recipe, r.country, r.category, table, num_samples)
            if out is None:
                skipped.append({"table": table, "country": r.country, "category": r.category, "reason": reason})
                continue
            per_table.append(out)
            log.info("✓ %s/%s/%s CRPS=%.1f cov95=%.2f", table, r.country, r.category, out["crps"], out["cov95"])
        if not per_table:
            log.warning("no evaluable series for %s", table)
            continue
        rows += per_table
        k = sum(s["k95"] for s in per_table)
        n = sum(s["n_holdout_F"] for s in per_table)
        lo, hi = intervals.jeffreys_ci(k, n)
        agg = {
            "table": table,
            "country": "ALL",
            "category": "ALL",
            "n_holdout_F": n,
            "crps": round(float(np.mean([s["crps"] for s in per_table])), 2),
            "cov95": round(k / n, 4),
            "k95": k,
            "cov95_ci_lo": round(lo, 3),
            "cov95_ci_hi": round(hi, 3),
            "n_series": len(per_table),
            "insufficient_n": n < N_FLOOR,
            "recipe": "+".join(recipe),
        }
        rows.append(agg)
        flag = f"  [INSUFFICIENT n<{N_FLOOR}]" if agg["insufficient_n"] else ""
        print(
            f"{table} champion ({agg['recipe']}): CRPS {agg['crps']} d · cov95 {agg['cov95']:.3f} "
            f"CI95 [{lo:.3f}, {hi:.3f}] n={n} ({agg['n_series']} series){flag}"
        )
    df = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"-> {OUT} ({len(df)} rows)")
    _seal(tables, block, limit, num_samples, skipped, complement=complement)
    return df


def _protocol(tables: tuple[str, ...], block: str, num_samples: int) -> dict:
    return {"tables": list(tables), "block": block, "num_samples": num_samples, "quants": list(QUANTS)}


def _seal(
    tables: tuple[str, ...],
    block: str,
    limit: int | None,
    num_samples: int,
    skipped: list[dict],
    *,
    complement: bool = False,
) -> None:
    """Recibo del CSV (M74-F · deuda 1): sin él, `champion.crps_provenance` lo trata como heredado.

    Sólo se sella una corrida COMPLETA con identidad que prestar: una campaña ``running`` o, con
    ``complement``, la complementaria `champion_crps` en curso (C5). Un ``--limit`` es un smoke y una
    corrida suelta no tiene identidad: el CSV queda escrito pero sin acreditar, y así lo dice el veredicto
    en vez de publicarlo como vigente. El recibo declara también las series omitidas con su motivo, para
    que el lector pueda exigir puntuadas + omitidas = catálogo.
    """
    from vp_model import artifact_receipt as ar
    from vp_model import champion

    if limit:
        log.warning("--limit %s: smoke, el CSV queda SIN recibo (heredado para el veredicto)", limit)
        return
    if complement:
        from vp_model import complement as cx

        try:  # el código o la DuckDB pudieron cambiar mientras se calculaba: sin precondiciones no hay firma
            ident = cx.identidad_crps(REPORTS.parent, duckdb_consumida=Path(dataset.DB_PATH))
        except cx.ComplementoError as exc:
            raise SystemExit(f"✗ el CRPS campeón no se firma: {exc}") from exc
        cid, sha, panel = ident["campaign_id"], ident["source_git_sha"], ident["panel_sha256"]
    else:
        try:
            cid, sha, panel = ar.campaign_identity(REPORTS, code_root=REPORTS.parent)
        except ar.ReceiptError as exc:
            log.warning("fuera de campaña, el CSV queda SIN recibo (heredado para el veredicto): %s", exc)
            return
    recibo = ar.seal(
        OUT,
        schema=champion.CRPS_RECEIPT_SCHEMA,
        campaign_id=cid,
        code_sha=sha,
        panel_sha256=panel,
        protocol=_protocol(tables, block, num_samples),
        coverage={"tables": sorted(set(tables)), "skipped": sorted(skipped, key=lambda o: tuple(o.values()))},
    )
    print(f"   recibo {recibo.name} · {cid}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default=",".join(CRPS_PROTOCOLO["tables"]))
    ap.add_argument("--block", default=CRPS_PROTOCOLO["block"])
    ap.add_argument("--limit", type=int, default=None, help="cap series per table (smoke test)")
    ap.add_argument(
        "--num-samples", type=int, default=CRPS_PROTOCOLO["num_samples"], help="native samples for ARIMA/SARIMA members"
    )
    ap.add_argument("--complement", action="store_true", help="C5 inside the running champion_crps complement")
    args = ap.parse_args(argv)
    tables = tuple(t.strip() for t in args.tables.split(",") if t.strip())
    if args.complement:
        from vp_model import complement as cx

        if args.limit or _protocol(tables, args.block, args.num_samples) != CRPS_PROTOCOLO:
            raise SystemExit(f"✗ C5 sólo acredita la corrida completa {CRPS_PROTOCOLO}: sin --limit ni otro protocolo")
        try:
            cx.precondiciones_calculo(REPORTS.parent, proposito="champion_crps", duckdb_consumida=Path(dataset.DB_PATH))
        except cx.ComplementoError as exc:
            raise SystemExit(f"✗ el CRPS campeón no puede calcularse aquí: {exc}") from exc
    elif (REPORTS / "campaign" / "complement.json").exists():
        raise SystemExit("✗ este árbol tiene una transacción complementaria: corre con --complement")
    run(tables, args.block, args.limit, args.num_samples, complement=args.complement)


if __name__ == "__main__":
    main()
