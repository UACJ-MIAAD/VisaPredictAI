"""Conformal avanzado para subir la cobertura del PI del ganador profundo (objetivo 0.95).

La investigación señaló EnbPI / Adaptive Conformal Inference (ACI) / CQR. Aquí se comparan,
sobre los pronósticos puntuales del deep global (BiTCN, ``reports/campaign/global_{table}_camp_diff_s1.csv``):
  * split-conformal con el cuantil FINITE-SAMPLE correcto ceil((n+1)(1-α))/n de |residuales|,
  * ACI (Gibbs & Candès 2021): ajusta α_t online tras cada punto para clavar la cobertura,
contra el baseline neuralforecast-conformal, que se DERIVA de ``reports/eval/deep_pi_{table}.csv``
(AN4c: antes era un literal stale hardcodeado que contradecía el CSV medible).

Calibración leakage-free: por serie, los primeros 12 meses del hold-out calibran; los últimos
12 se evalúan. El gamma de ACI se elige de un grid {0.01, 0.03, 0.05, 0.1} usando SOLO el tramo
de calibración (sub-split interno 2/3 pool / 1/3 online) — el test nunca participa en la
selección (AN4a). El gamma elegido por tabla se publica en ``reports/eval/aci_gamma.json``
para que el flujo desplegado (``generate_web_forecasts``) lo consuma.

Hygiene (AN7): pooled coverages carry a Jeffreys CI and n; means of per-series coverages are
reported as before for continuity but the binomial claim is the pooled one.

★ M74-F · C3: con ``--complement --parent-root MADRE`` corre dentro de una transacción
complementaria `running` de ``deep_pi_conformal``. El deep-PI (baseline) y los pronósticos globales
NO se producen aquí: se consumen sólo como importaciones acreditadas de la madre
(`complement.importacion_acreditada`), se rotulan ``importado:<madre>`` y quedan en un sidecar de
procedencia (`complement.PROCEDENCIA_REL`) sin cambiar el esquema del CSV. Si cualquiera no se
acredita, no se escribe nada. Sin la bandera, un árbol con complementaria se niega a correr.

Corre en ``ante``.  Uso:  ante/bin/python experiments/improve_conformal.py [--mlflow]
      ante/bin/python experiments/improve_conformal.py --complement --parent-root RAIZ_MADRE
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from vp_data import tracking
from vp_model import complement as cx
from vp_model import dataset
from vp_model.intervals import jeffreys_ci
from vp_model.metrics import mase_by_series, naive_scale_before

REPORTS = Path(__file__).resolve().parent.parent / "reports"
ALPHA = 0.05  # PI al 95%
GAMMA_GRID = (0.01, 0.03, 0.05, 0.1)
N_FLOOR = 30  # AN7: below this a pooled coverage is flagged insufficient_n


def _split_conformal(res_cal: np.ndarray, alpha: float) -> float:
    """Semiancho conformal: cuantil finite-sample de |residuales| de calibración."""
    n = len(res_cal)
    q_level = min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)
    return float(np.quantile(np.abs(res_cal), q_level, method="higher"))


def _aci(res_cal: np.ndarray, res_test: np.ndarray, alpha: float, gamma: float) -> tuple[list[bool], list[float]]:
    """Adaptive Conformal Inference: ajusta alpha_t online; devuelve (hits, anchos)."""
    a = alpha
    pool = list(np.abs(res_cal))
    covered: list[bool] = []
    widths: list[float] = []
    for r in res_test:
        a_eff = min(max(a, 1e-3), 0.999)
        q = float(np.quantile(pool, 1 - a_eff, method="higher"))
        covered.append(bool(abs(r) <= q))
        widths.append(2 * q)
        a = a + gamma * (alpha - (0 if abs(r) <= q else 1))  # err_t=1 si fuera del PI
        pool.append(abs(r))
    return covered, widths


def select_gamma(res_cal_by_series: list[np.ndarray], alpha: float, grid: tuple[float, ...] = GAMMA_GRID) -> float:
    """Pick gamma on CALIBRATION data only (AN4a): sub-split each series' calibration
    residuals into a 2/3 pool + 1/3 online stretch, replay ACI per gamma, and choose the
    gamma whose pooled coverage is closest to (1 - alpha), breaking ties by mean width."""
    best: tuple[float, float, float] | None = None  # (|cov - target|, mean_width, gamma)
    for gamma in grid:
        hits: list[bool] = []
        widths: list[float] = []
        for res in res_cal_by_series:
            cut = max(1, (2 * len(res)) // 3)
            if len(res) - cut < 1:
                continue
            h, w = _aci(res[:cut], res[cut:], alpha, gamma)
            hits += h
            widths += w
        if not hits:
            continue
        key = (abs(float(np.mean(hits)) - (1 - alpha)), float(np.mean(widths)), gamma)
        if best is None or key[:2] < best[:2]:
            best = key
    return best[2] if best is not None else 0.05


def deep_pi_no_acreditado(path: Path, table: str, *, reports: Path | None = None) -> str | None:
    """``None`` si ``deep_pi_{table}.csv`` es de la campaña en curso; si no, el motivo. ★ M74-F · deuda 4.

    En la cuarta campaña real conformal (etapa 4) leyó el deep-PI de la corrida anterior porque
    deep-PI no corría hasta la etapa 5. El orden ya está corregido en el runbook; esto es lo que lo
    hace comprobable: un CSV sin recibo, de otra campaña o tocado después del sellado no es baseline.
    """
    from vp_model import artifact_receipt as ar

    reports = reports if reports is not None else REPORTS
    if not path.is_file():
        return f"no existe {path.name}"
    try:
        cid, sha, panel = ar.campaign_identity(reports, code_root=reports.parent)
        ar.verify(
            path,
            schema="deep-pi-receipt/1",
            campaign_id=cid,
            code_sha=sha,
            panel_sha256=panel,
            protocol={"table": table, "cqr": False},
        )
    except ar.ReceiptError as exc:
        return str(exc)
    return None


def _baseline_cov(table: str) -> float | None:
    """Derive the neuralforecast-conformal baseline coverage from ``deep_pi_{table}.csv``
    (F-only mask, mean of per-series coverages — same recipe as eval_deep_pi). AN4c: the
    previous hardcoded {FAD: 0.89, DFF: 0.77} contradicted the measurable CSVs."""
    path = REPORTS / "eval" / f"deep_pi_{table}.csv"
    motivo = deep_pi_no_acreditado(path, table)
    if motivo is not None:
        print(f"  [{table}] baseline deep-PI descartado: {motivo}")
        return None
    return _cobertura_deep_pi(path, table)


def _cobertura_deep_pi(path: Path, table: str) -> float | None:
    """Cobertura media por serie (máscara F-only) de un CSV deep-PI ya acreditado por quien llama."""
    df = pd.read_csv(path, parse_dates=["ds"])
    model = next(c for c in df.columns if "-" not in c and c not in ("unique_id", "ds", "y"))
    covs = []
    for uid, g in df.groupby("unique_id"):
        country, _block, category = uid.split("/")
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        g = g.sort_values("ds")
        y = full.reindex(g["ds"]).to_numpy()
        fin = np.isfinite(y)
        if not fin.any():
            continue
        g, y = g[fin], y[fin]
        covs.append(float(np.mean((y >= g[f"{model}-lo-95"].to_numpy()) & (y <= g[f"{model}-hi-95"].to_numpy()))))
    return float(np.mean(covs)) if covs else None


def _point_mase(deep: pd.DataFrame, table: str) -> float:
    """Point MASE of the deep forecasts via the canonical scorer (AM4d).

    ``metrics.mase_by_series`` owns the F-only mask + shared naive scale; the residual /
    coverage machinery below keeps its own loop because it needs per-series residual
    SPLITS (calibration/test), which is outside the point scorer's contract.
    """
    frame = deep[deep["BiTCN"].notna()][["unique_id", "ds", "BiTCN"]].rename(
        columns={"ds": "date", "BiTCN": "forecast"}
    )
    parts = frame.unique_id.str.split("/")
    frame = frame.assign(country=parts.str[0], category=parts.str[2])
    return float(mase_by_series(frame, table).mean())


def _evaluate(table: str) -> dict:
    deep = pd.read_csv(REPORTS.parent / cx.artefacto_c3("forecasts", table)[0], parse_dates=["ds"])
    series: list[dict] = []
    for uid, g in deep.groupby("unique_id"):
        country, _b, category = uid.split("/")
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        g = g.sort_values("ds")
        g = g[g["ds"].isin(full.index)]  # F-only
        if len(g) < 16 or "BiTCN" not in g.columns:
            continue
        y = full.reindex(g["ds"]).to_numpy()
        res = y - g["BiTCN"].to_numpy()  # residuales del pronóstico
        cut = len(res) // 2
        scale = naive_scale_before(full, g["ds"].iloc[cut])
        series.append({"res_cal": res[:cut], "res_test": res[cut:], "scale": scale})

    point_mase = _point_mase(deep, table)

    # AN4a: gamma selected on calibration residuals only, then frozen for the test pass.
    gamma = select_gamma([s["res_cal"] for s in series], ALPHA)

    cov_split, w_split = [], []
    aci_hits_all: list[bool] = []
    cov_aci, w_aci = [], []
    split_hits_all: list[bool] = []
    for s in series:
        q = _split_conformal(s["res_cal"], ALPHA)
        hits_split = list(np.abs(s["res_test"]) <= q)
        split_hits_all += hits_split
        cov_split.append(float(np.mean(hits_split)))
        w_split.append(2 * q / s["scale"])
        hits, widths = _aci(s["res_cal"], s["res_test"], ALPHA, gamma)
        aci_hits_all += hits
        cov_aci.append(float(np.mean(hits)))
        w_aci.append(float(np.mean(widths)) / s["scale"])

    def _pooled(hits: list[bool]) -> dict:
        k, n = int(sum(hits)), len(hits)
        lo, hi = jeffreys_ci(k, n)
        return {"cov": k / n, "ci": (round(lo, 3), round(hi, 3)), "n": n, "insufficient_n": n < N_FLOOR}

    return {
        "gamma": gamma,
        "point_mase": point_mase,
        "split_cov": float(np.mean(cov_split)),
        "split_width": float(np.mean(w_split)),
        "split_pooled": _pooled(split_hits_all),
        "aci_cov": float(np.mean(cov_aci)),
        "aci_width": float(np.mean(w_aci)),
        "aci_pooled": _pooled(aci_hits_all),
        "n": len(series),
    }


def importaciones_c3(parent_root: Path, tablas: tuple[str, ...] = ("FAD", "DFF")) -> dict[str, dict[str, dict]]:
    """Las importaciones que C3 consume, acreditadas ANTES de calcular nada. Fail-closed (SystemExit).

    Antes, las precondiciones del contrato: código permitido y cargado de este árbol, panel y DuckDB
    sellados, y la DuckDB que `dataset` abrirá de verdad (``DB_PATH`` resuelta contra el cwd) es la suya.
    """
    root = REPORTS.parent
    try:
        cx.precondiciones_calculo(root, proposito="deep_pi_conformal", duckdb_consumida=Path(dataset.DB_PATH))
        return {
            t: {
                rol: cx.importacion_acreditada(root, *cx.artefacto_c3(rol, t), parent_root=parent_root)
                for rol in cx.ROLES_C3
            }
            for t in tablas
        }
    except cx.ComplementoError as exc:
        raise SystemExit(f"✗ C3 no puede consumir sus importaciones: {exc}") from exc


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    ap.add_argument("--complement", action="store_true", help="C3 dentro de la complementaria en curso")
    ap.add_argument("--parent-root", type=Path, help="raíz de la campaña madre (obligatoria con --complement)")
    args = ap.parse_args(argv)
    if args.complement != (args.parent_root is not None):
        ap.error("--complement y --parent-root van juntos")
    consumidas = None
    if args.complement:
        consumidas = importaciones_c3(args.parent_root)
    elif (REPORTS / "campaign" / "complement.json").exists():
        raise SystemExit("✗ este árbol tiene una transacción complementaria: corre con --complement")
    rows = []
    gammas: dict[str, float] = {}
    for table in ("FAD", "DFF"):
        r = _evaluate(table)
        gammas[table] = r["gamma"]
        if consumidas is not None:
            base = _cobertura_deep_pi(REPORTS.parent / cx.artefacto_c3("baseline", table)[0], table)
            if base is None:
                raise SystemExit(f"✗ C3: el deep-PI importado de {table} no deja calcular el baseline")
        else:
            base = _baseline_cov(table)
        origen = consumidas[table]["baseline"]["source"] if consumidas is not None else f"deep_pi_{table}.csv"
        base_txt = f"{base:.3f} (derived from {origen})" if base is not None else "n/a (deep-PI no acreditado)"
        sp, ap_ = r["split_pooled"], r["aci_pooled"]
        print(f"\n=== CONFORMAL AVANZADO {table} ({r['n']} series, target cobertura 0.95) ===")
        print(f"  MASE puntual BiTCN (scorer canónico): {r['point_mase']:.3f}  [informativo]")
        print(f"  baseline (nf conformal)      : cobertura {base_txt}")
        print(
            f"  split-conformal finite-sample: cobertura {r['split_cov']:.3f} "
            f"[pooled {sp['cov']:.3f} CI95 {sp['ci']} n={sp['n']}]  ancho {r['split_width']:.2f}"
        )
        print(
            f"  ACI (gamma={r['gamma']}, grid-cal)  : cobertura {r['aci_cov']:.3f} "
            f"[pooled {ap_['cov']:.3f} CI95 {ap_['ci']} n={ap_['n']}]  ancho {r['aci_width']:.2f}"
        )
        if sp["insufficient_n"] or ap_["insufficient_n"]:
            print(f"  WARNING: n < {N_FLOOR} — coverage claims flagged insufficient_n")
        rows.append(
            {
                "table": table,
                "n_series": r["n"],
                "baseline_coverage": round(base, 4) if base is not None else None,
                "aci_gamma": r["gamma"],
                "split_coverage": round(r["split_cov"], 4),
                "split_coverage_pooled": round(sp["cov"], 4),
                "split_cov_ci_lo": sp["ci"][0],
                "split_cov_ci_hi": sp["ci"][1],
                "split_width_scaled": round(r["split_width"], 4),
                "aci_coverage": round(r["aci_cov"], 4),
                "aci_coverage_pooled": round(ap_["cov"], 4),
                "aci_cov_ci_lo": ap_["ci"][0],
                "aci_cov_ci_hi": ap_["ci"][1],
                "aci_width_scaled": round(r["aci_width"], 4),
                "n_pooled": sp["n"],
                "insufficient_n": sp["insufficient_n"],
            }
        )
        if args.mlflow:
            for m, cov, w in [
                ("split_conformal", r["split_cov"], r["split_width"]),
                ("aci", r["aci_cov"], r["aci_width"]),
            ]:
                tracking.log_run(
                    f"improve_{table}",
                    f"conformal_{m}/{table}",
                    params={"method": f"conformal_{m}", "table": table, "layer": "improve"},
                    metrics={"coverage95": cov, "width_scaled": w},
                    tags={"layer": "improve", "technique": "conformal"},
                )
    pd.DataFrame(rows).to_csv(
        REPORTS / "eval" / "conformal_coverage.csv", index=False
    )  # artefacto reproducible del claim ACI
    # AN4: the deployed flow (generate_web_forecasts) reads the selected gamma from here.
    (REPORTS / "eval" / "aci_gamma.json").write_text(
        json.dumps({**gammas, "grid": list(GAMMA_GRID), "selected_on": "calibration split only"}, indent=2) + "\n"
    )
    print(f"\naci_gamma.json -> {gammas}")
    if consumidas is not None:
        try:
            cx.sellar_procedencia_c3(REPORTS.parent, consumidas)
        except cx.ComplementoError as exc:  # el código cambió mientras calculaba: sin firma, C3 no cierra
            raise SystemExit(f"✗ C3 no firma su procedencia: {exc}") from exc
        print(f"procedencia del baseline -> {cx.PROCEDENCIA_REL} (importado de la madre)")


if __name__ == "__main__":
    main()
