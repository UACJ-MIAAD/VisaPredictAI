"""Confirmación INDEPENDIENTE del tuning (AK6, regla anti-overtuning).

El flujo viejo aceptaba/rechazaba candidatos mirando el MASE de HOLD-OUT
publicado — selección adaptativa sobre el conjunto de prueba. Protocolo nuevo:
la región de selección se parte en train / val-tuning (24) / val-confirm (12).
Optuna optimizó en val-tuning (``run_tuning``); ESTE módulo decide la
aceptación — y el fallback por-serie a los defaults — en val-confirm, una
ventana que ni el tuner ni el incumbente vieron jamás. El hold-out de 24 meses
queda SOLO para el número publicado (rol de reporte, separado de la decisión).

El resumen reporta media + mediana + % de series que mejoran: una media sola
esconde que el tuning empeore la mayoría de las series (pasó en DFF: 7/14
series peores con media "ganadora"). Las pseudo-réplicas del corte mundial ya
vienen colapsadas por ``tune._group_series`` (AK2), así que las medias no
sobreponderan el corte mundial. La aceptación se escribe de vuelta a
``reports/eval/tuned_params.json`` (``"improved": True``) — la llave que
``models._tree_params`` usa para enrutar los ganadores al catálogo. ⚠️
``_tree_params`` hoy solo lee ``{table}_family``; las aceptaciones de employment
quedan registradas pero no se enrutan hasta que models.py aprenda el bloque.

``--holdout-report`` re-corre el walk-forward COMPLETO para tuned y default EN
LA MISMA SESIÓN (mismo panel, mismo código — el viejo ``_default_holdout``
mezclaba añadas al leer ``run_id.max()`` del CSV de una campaña previa) y
escribe ``reports/eval/tuning_holdout_report.csv``. Ese archivo es SOLO
reporte: jamás alimenta la decisión de aceptación.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import models, tune, walkforward
from vp_model.config import get_logger

log = get_logger("confirm_tuning")
REPORTS = Path(__file__).resolve().parent.parent / "reports"
TUNED = REPORTS / "eval" / "tuned_params.json"


def decide(tuned_path: Path = TUNED) -> pd.DataFrame:
    """MASE por serie en val-confirm: candidato tuneado vs default del catálogo.

    Usa el mismo objetivo barato del tuner (``tune._val_mase``) pero sobre la
    ventana ``confirm`` que el tuner nunca vio. ``use_tuned`` es el fallback
    por-serie: si el default gana en val-confirm en ESA serie, el despliegue
    por-serie debe conservar el default aunque el grupo acepte. NaN en ambos
    lados (serie corta) se omite; NaN solo en tuned cuenta como no-mejora.
    """
    tuned = json.loads(tuned_path.read_text())
    rows = []
    for model, groups in tuned.items():
        for key, info in groups.items():
            table, block = key.split("_", 1)
            params = info.get("best_params", {})
            for country, category, tb in tune._group_series(table, block):
                try:
                    d = tune._val_mase(model, country, category, tb, None, window="confirm")
                    t = tune._val_mase(model, country, category, tb, dict(params), window="confirm")
                except (ValueError, KeyError) as e:  # una serie que falle no aborta el resto
                    log.warning("skip %s %s/%s: %s", model, country, category, e)
                    continue
                if np.isnan(d) and np.isnan(t):
                    continue
                rows.append(
                    {
                        "model": model,
                        "table": table,
                        "block": block,
                        "country": country,
                        "category": category,
                        "confirm_default": d,
                        "confirm_tuned": t,
                        "use_tuned": bool(t < d),  # NaN nunca gana (conservador)
                    }
                )
            log.info("confirmado %s · %s (val-confirm)", model, key)
    df = pd.DataFrame(rows)
    out = REPORTS / "eval" / "tuning_confirmation.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    return df


def summary(df: pd.DataFrame) -> pd.DataFrame:
    """Veredicto por modelo x tabla x bloque: media + mediana + % de series que mejoran.

    ``acepta`` (media tuneada < media default en val-confirm) es la regla de
    aceptación; ``median_agrees`` y ``pct_improve`` acompañan SIEMPRE al
    reporte para que la prosa no pueda decir "mejora" cuando la mediana o la
    mayoría de las series digan lo contrario.
    """
    g = (
        df.groupby(["model", "table", "block"])
        .agg(
            tuned_mean=("confirm_tuned", "mean"),
            default_mean=("confirm_default", "mean"),
            tuned_median=("confirm_tuned", "median"),
            default_median=("confirm_default", "median"),
            pct_improve=("use_tuned", "mean"),
            n=("country", "count"),
        )
        .reset_index()
    )
    g["delta_pct"] = (100 * (g.default_mean - g.tuned_mean) / g.default_mean).round(1)
    g["acepta"] = g.tuned_mean < g.default_mean
    g["median_agrees"] = g.tuned_median < g.default_median
    return g.round(4)


def apply_acceptance(s: pd.DataFrame, tuned_path: Path = TUNED) -> None:
    """Escribe la aceptación en ``tuned_params.json`` (enruta ``models._tree_params``).

    Solo las entradas con ``"improved": True`` llegan a los GBMs del catálogo;
    el resto conserva los defaults puente. También persiste las cifras de
    val-confirm dentro de la entrada (procedencia de la decisión).
    """
    tuned = json.loads(tuned_path.read_text())
    for r in s.itertuples():
        entry = tuned.get(r.model, {}).get(f"{r.table}_{r.block}")
        if entry is None:
            continue
        entry["improved"] = bool(r.acepta)
        entry["confirm"] = {
            "tuned_mean": float(r.tuned_mean),
            "default_mean": float(r.default_mean),
            "tuned_median": float(r.tuned_median),
            "default_median": float(r.default_median),
            "pct_improve": float(r.pct_improve),
            "median_agrees": bool(r.median_agrees),
            "n_series": int(r.n),
        }
    tuned_path.write_text(json.dumps(tuned, indent=2))
    log.info("aceptación escrita -> %s", tuned_path)


def holdout_report(tuned_path: Path = TUNED) -> pd.DataFrame:
    """Hold-out MISMO-VINTAGE de tuned vs default puente — SOLO reporte (AK6).

    Re-corre el walk-forward completo para ambas variantes en esta sesión: el
    default se inyecta con ``models.build_model(model)`` (``table=None`` = puente
    de config, NO el ganador ya enrutado) para que la comparación sea contra lo
    que se desplegaría sin tuning. Caro (~2 backtests por serie); se corre en
    campaña, después de ``apply_acceptance``. Nunca alimenta la decisión.
    """
    tuned = json.loads(tuned_path.read_text())
    rows = []
    for model, groups in tuned.items():
        for key, info in groups.items():
            table, block = key.split("_", 1)
            params = info.get("best_params", {})
            for country, category, tb in tune._group_series(table, block):
                try:
                    t_res = walkforward.backtest(
                        model, country, category, tb, model=tune._build_tuned(model, dict(params))
                    )
                    d_res = walkforward.backtest(model, country, category, tb, model=models.build_model(model))
                except Exception as e:  # noqa: BLE001 — report loop: a frozen series
                    # (e.g. CatBoostError "All train targets are equal" on all-zero
                    # deltas) must not kill the whole holdout report.
                    log.warning("holdout skip %s %s/%s: %s", model, country, category, e)
                    continue
                rows.append(
                    {
                        "model": model,
                        "table": table,
                        "block": block,
                        "country": country,
                        "category": category,
                        "tuned_hold_mase": t_res.holdout["mase"],
                        "default_hold_mase": d_res.holdout["mase"],
                        "accepted": bool(info.get("improved", False)),
                    }
                )
            log.info("holdout report %s · %s", model, key)
    df = pd.DataFrame(rows)
    out = REPORTS / "eval" / "tuning_holdout_report.csv"
    df.to_csv(out, index=False)
    return df


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tuned", type=Path, default=TUNED)
    ap.add_argument("--holdout-report", action="store_true", help="además, hold-out mismo-vintage (solo reporte)")
    ap.add_argument("--mlflow", action="store_true", help="loguear el veredicto por grupo al tracking")
    args = ap.parse_args(argv)

    s = summary(decide(args.tuned))
    apply_acceptance(s, args.tuned)
    print(s.to_string(index=False))
    print("\nACEPTAR donde acepta=True (decidido en val-confirm, regla AK6); default en el resto.")
    print("Reportar SIEMPRE media + mediana + pct_improve juntas (la media sola engaña).")

    if args.mlflow:
        from vp_data import tracking

        for r in s.itertuples():
            tracking.log_run(
                f"hpo_{r.model}_{r.table}",
                f"confirm-{r.model}-{r.table}-{r.block}",
                params={"model": r.model, "table": r.table, "block": r.block, "acepta": str(r.acepta)},
                metrics={
                    "confirm_tuned_mean": r.tuned_mean,
                    "confirm_default_mean": r.default_mean,
                    "confirm_tuned_median": r.tuned_median,
                    "confirm_default_median": r.default_median,
                    "pct_improve": r.pct_improve,
                },
                tags={"layer": "hpo", "kind": "confirm"},
            )

    if args.holdout_report:
        hr = holdout_report(args.tuned)
        if not hr.empty:
            g = hr.groupby(["model", "table", "block"])[["tuned_hold_mase", "default_hold_mase"]].agg(
                ["mean", "median"]
            )
            print("\nHold-out mismo-vintage (SOLO reporte, no decisión):")
            print(g.round(4).to_string())


if __name__ == "__main__":
    main()
