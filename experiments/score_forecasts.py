"""Evaluación PROSPECTIVA (en tiempo real) de los pronósticos congelados.

El MASE del entregable es *retrospectivo* (hold-out: el modelo "predice" meses ya
conocidos). Esto es lo contrario: toma cada pronóstico **congelado** en
``reports/prospective/forecast_log.csv`` (lo que predijimos y desde qué mes — la "añada") y lo
compara con el **corte realmente publicado** después, conforme llegan los boletines.
Es la única medida honesta de qué tan bueno es el pronóstico a 12 meses en el mundo real.

Por cada fila del ledger cuyo mes-objetivo ya tiene un corte real (estado F en el panel):
  • error = predicho − real (días);   |error|;   error escalado (MASE) por la escala
    naïve estacional in-sample hasta el origen (leakage-free, misma def. que el .tex);
  • cobertura: ¿el real cayó dentro de la banda 80 % / 95 %?

Agrega global, por horizonte h=1..12 y por tabla. **A3 (plan auditoría 2026-07-11):** el
ledger SOMBRA se puntúa con la MISMA maquinaria (máscara F, escala, universo) a archivos
propios, los agregados JAMÁS combinan ``evaluation_mode`` backfill y live (``overall``/
``by_horizon``/``by_table`` quedan anclados al modo backfill; ``by_mode`` reporta cada
modo por separado) y se emite la comparación campeón-vs-sombra por pares del mismo
universo, consumible por el gate de promoción (A4). Salidas:
  • reports/prospective/forecast_scorecard.csv         — una fila por predicción campeón evaluable
  • reports/prospective/forecast_scorecard_meta.json   — agregados (MAE/MASE/cobertura, n, by_mode)
  • reports/prospective/forecast_scorecard_shadow.csv  — ídem para el ledger sombra
  • reports/prospective/forecast_scorecard_shadow_meta.json
  • reports/prospective/prospective_head_to_head.json  — pares campeón/sombra (mismo modo)
Tracking MLflow (experimento "web_forecast_scoring") es para desarrollo local; el registro
DURABLE es el scorecard commiteado en git (en CI el staging MLflow es efímero).

Al inicio de una añada nada está realizado aún → n=0 (correcto): la medición se
acumula mes a mes. Corre en ``ante``:  ante/bin/python experiments/score_forecasts.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from vp_data import tracking
from vp_model import config, dataset, intervals, metrics

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
N_FLOOR = 30  # AN7: coverage blocks with n below this carry insufficient_n=true
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
                # A3: el modo y la receta viajan del ledger v2 al scorecard para que los
                # agregados puedan separarse; frames pre-v2 (demo/tests) degradan a n/d.
                "evaluation_mode": getattr(r, "evaluation_mode", "n/d"),
                "model_version": getattr(r, "model_version", "n/d"),
            }
        )
    return scored, pending


def _agg(d: pd.DataFrame) -> dict:
    # AN7: every reported coverage carries a Jeffreys CI and its n; below the n floor
    # the block is flagged insufficient_n (a coverage on a handful of points is noise).
    n = int(len(d))
    out: dict[str, object] = {
        "n": n,
        "mae_days": round(float(d["abs_err"].mean()), 1),
        "mase": round(float(d["scaled_err"].mean()), 4),
        "cov80": round(float(d["in80"].mean()), 3),
        "cov95": round(float(d["in95"].mean()), 3),
    }
    for col in ("in80", "in95"):
        lo, hi = intervals.jeffreys_ci(int(d[col].sum()), n)
        out[f"cov{col[2:]}_ci95"] = [round(lo, 3), round(hi, 3)]
    if n < N_FLOOR:
        out["insufficient_n"] = True
    return out


def _mode_blocks(sdf: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """(filas backfill, bloques por modo) — A3: los agregados JAMÁS combinan modos.

    ``overall``/``by_horizon``/``by_table`` del meta se anclan al modo ``backfill``
    (hoy el único con filas puntuadas); cada modo reporta su propio bloque en
    ``by_mode`` y las añadas ``live`` se acumulan ahí sin diluirse ni diluir."""
    if not len(sdf) or "evaluation_mode" not in sdf.columns:
        return sdf, {}
    by_mode = {
        str(mode): {"overall": _agg(g), "by_horizon": {int(h): _agg(gh) for h, gh in g.groupby("h")}}
        for mode, g in sdf.groupby("evaluation_mode")
    }
    return sdf[sdf["evaluation_mode"] == "backfill"], by_mode


def _head_to_head(champ: pd.DataFrame, shadow: pd.DataFrame) -> dict:
    """Comparación campeón-vs-sombra por pares del MISMO universo (A3 → gate A4).

    Un par = misma (añada, serie, fecha objetivo, h) puntuada en ambos ledgers. Solo se
    agregan pares cuyo ``evaluation_mode`` coincide en ambos lados (backfill con backfill,
    live con live); los pares de modo mixto se cuentan y se excluyen."""
    if not len(champ) or not len(shadow):
        return {"n_pairs": 0, "n_mixed_mode_excluded": 0, "by_table": {}}
    keys = ["origin", "country", "category", "table", "target", "h"]
    pair = champ.merge(shadow, on=keys, suffixes=("_champ", "_shadow"))
    out: dict[str, object] = {"n_pairs": int(len(pair)), "n_mixed_mode_excluded": 0, "by_table": {}}
    if not len(pair):
        return out
    same = pair[pair["evaluation_mode_champ"] == pair["evaluation_mode_shadow"]]
    out["n_mixed_mode_excluded"] = int(len(pair) - len(same))

    def _side(g: pd.DataFrame, suffix: str) -> dict:
        return {
            "mase": round(float(g[f"scaled_err{suffix}"].mean()), 4),
            "mae_days": round(float(g[f"abs_err{suffix}"].mean()), 1),
            "model_version": sorted(g[f"model_version{suffix}"].astype(str).unique()),
        }

    tables: dict[str, dict] = {}
    for (table, mode), g in same.groupby(["table", "evaluation_mode_champ"]):
        blk: dict[str, object] = {
            "n": int(len(g)),
            "champion": _side(g, "_champ"),
            "shadow": _side(g, "_shadow"),
            "by_horizon": {
                int(h): {
                    "n": int(len(gh)),
                    "champion_mase": round(float(gh["scaled_err_champ"].mean()), 4),
                    "shadow_mase": round(float(gh["scaled_err_shadow"].mean()), 4),
                }
                for h, gh in g.groupby("h")
            },
        }
        if len(g) < N_FLOOR:
            blk["insufficient_n"] = True
        tables.setdefault(str(table), {})[str(mode)] = blk
    out["by_table"] = tables
    return out


def run() -> Path | None:
    log_path = REPORTS / "prospective" / "forecast_log.csv"
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
    sdf.to_csv(REPORTS / "prospective" / "forecast_scorecard.csv", index=False)
    n_no_scale = int(sdf["scaled_err"].isna().sum()) if len(sdf) else 0
    if n_no_scale:
        log.warning("%d fila(s) evaluable(s) sin escala naïve válida (excluidas del MASE)", n_no_scale)

    # A3: overall/by_horizon/by_table se ANCLAN al modo backfill — cuando las añadas live
    # empiecen a puntuar viven en by_mode; ningún agregado combina modos jamás.
    back, by_mode = _mode_blocks(sdf)
    overall = _agg(back) if len(back) else {"n": 0}
    by_h = {int(h): _agg(g) for h, g in back.groupby("h")} if len(back) else {}
    by_table = {t: _agg(g) for t, g in back.groupby("table")} if len(back) else {}
    # cov80 HELD-OUT: cobertura de la banda 80 % sobre las añadas NO usadas para calibrar
    # BAND80_RATIO → out-of-sample, no circular (overall.cov80 sí incluye calibración).
    heldout = back[~back["origin"].isin(config.BAND80_CAL_VINTAGES)] if len(back) else back
    # n efectivo por añada: muchas añadas (orígenes con último-F antiguo) NO aportan filas
    # evaluables (sus meses-objetivo caen en régimen C/U) → honestidad: el grueso del n
    # viene de pocas añadas recientes. Se reporta el desglose para no inflar la amplitud.
    scored_by_vintage = {o: int((sdf["origin"] == o).sum()) for o in sorted(fc["origin"].unique())} if len(sdf) else {}
    meta = {
        "what": "evaluación prospectiva (pronóstico congelado vs corte realmente publicado)",
        "caveat": "backfill leakage-free; NO equivale a haber servido los pronósticos en tiempo real",
        "aggregation_scope": (
            "overall/by_horizon/by_table = SOLO filas evaluation_mode=backfill (A3); "
            "las añadas live se reportan aparte en by_mode y JAMÁS se agregan junto al backfill"
        ),
        "by_mode": by_mode,
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
            "cov80_heldout_ci95": (
                [round(c, 3) for c in intervals.jeffreys_ci(int(heldout["in80"].sum()), len(heldout))]
                if len(heldout)
                else None
            ),
            "insufficient_n": len(heldout) < N_FLOOR,
            "note": "BAND80_RATIO se calibra en cal_vintages; cov80_heldout es la cobertura 80 % OUT-OF-SAMPLE (overall.cov80 incluye la calibración y es optimista).",
        },
    }
    (REPORTS / "prospective" / "forecast_scorecard_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    )

    # A3: el ledger sombra se puntúa con la MISMA maquinaria y el mismo universo de
    # actuals, a archivos propios (jamás se mezcla con el scorecard del campeón), y se
    # emite la comparación campeón-vs-sombra por pares para el gate de promoción (A4).
    shadow_path = REPORTS / "prospective" / "forecast_log_shadow.csv"
    if shadow_path.exists():
        sfc = pd.read_csv(shadow_path)
        s_scored, s_pending = _score_rows(sfc, actuals, scale_for)
        s_sdf = pd.DataFrame(s_scored)
        s_sdf.to_csv(REPORTS / "prospective" / "forecast_scorecard_shadow.csv", index=False)
        _s_back, s_by_mode = _mode_blocks(s_sdf)
        shadow_meta = {
            "what": "scoring del ledger SOMBRA (retador) con la misma maquinaria y universo que el campeón (A3)",
            "caveat": "backfill leakage-free; NO equivale a haber servido los pronósticos en tiempo real",
            "n_scored": int(len(s_sdf)),
            "n_pending": int(s_pending),
            "by_mode": s_by_mode,
            "recipes": sorted(sfc["recipe"].astype(str).unique().tolist()) if "recipe" in sfc.columns else [],
        }
        (REPORTS / "prospective" / "forecast_scorecard_shadow_meta.json").write_text(
            json.dumps(shadow_meta, ensure_ascii=False, indent=2) + "\n"
        )
        h2h = {
            "what": (
                "campeón vs sombra por pares (misma añada/serie/target/h y MISMO "
                "evaluation_mode) — insumo del gate de promoción (A4)"
            ),
            **_head_to_head(sdf, s_sdf),
        }
        (REPORTS / "prospective" / "prospective_head_to_head.json").write_text(
            json.dumps(h2h, ensure_ascii=False, indent=2) + "\n"
        )
        log.info(
            "SOMBRA: n=%d puntuadas (%d pendientes) · head-to-head: %d pares (%d de modo mixto excluidos)",
            len(s_sdf),
            s_pending,
            h2h["n_pairs"],
            h2h["n_mixed_mode_excluded"],
        )

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
    return REPORTS / "prospective" / "forecast_scorecard.csv"


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
