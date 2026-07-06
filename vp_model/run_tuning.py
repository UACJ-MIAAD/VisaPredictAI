"""CLI del HPO de los GBMs: busca CANDIDATOS y los persiste para confirmación.

Corre ``tune.tune`` por (modelo x tabla x bloque) con el objetivo F-only y
leakage-free (cola val-tuning; ni val-confirm ni el hold-out se tocan) y escribe
``reports/eval/tuned_params.json``. AK6: cada entrada nace con ``"improved":
False`` — es un CANDIDATO. La aceptación (que voltea el flag a True y con ello
enruta los parámetros a los GBMs del catálogo vía ``models._tree_params``) la
decide ``confirm_tuning`` sobre la ventana val-confirm independiente. Re-correr
este módulo NUNCA acepta nada por sí solo.

AK7: argparse — ``--n-trials``, ``--groups`` (incluye ``FAD_employment`` y
``DFF_employment``: los EB dejan de correr con defaults para siempre),
``--models``, ``--storage``, ``--out`` y ``--mlflow`` (callback por trial + run
resumen, AK5). ``--rank-check`` corre la verificación objetivo<->despliegue
(AK9) sobre los estudios persistidos en lugar de tunear.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from vp_model import config, tune
from vp_model.config import get_logger

log = get_logger("run_tuning")
OUT = Path(__file__).resolve().parent.parent / "reports" / "eval" / "tuned_params.json"
GROUPS = ("FAD_family", "DFF_family", "FAD_employment", "DFF_employment")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n-trials", type=int, default=tune.N_TRIALS)
    ap.add_argument("--groups", nargs="+", default=list(GROUPS), choices=list(GROUPS))
    ap.add_argument("--models", nargs="+", default=sorted(config.DIFFERENCED), choices=sorted(config.DIFFERENCED))
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--storage", default=None, help="URL de storage Optuna (default: sqlite reports/eval/optuna.db)")
    ap.add_argument("--mlflow", action="store_true", help="loguear cada trial + resumen al tracking (AK5)")
    ap.add_argument("--rank-check", action="store_true", help="correr AK9 sobre los estudios ya persistidos")
    ap.add_argument(
        "--select-by-deploy",
        action="store_true",
        help="fix #20: re-elige best_params por el deploy-score del rank-check (correr DESPUÉS de --rank-check y ANTES de confirm_tuning)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse(argv)

    if args.rank_check:
        for model_name in args.models:
            for key in args.groups:
                table, block = key.split("_", 1)
                try:
                    tune.rank_check(model_name, table=table, block=block, storage=args.storage)
                except KeyError as e:  # estudio inexistente (aún sin tunear ese grupo)
                    log.warning("rank_check %s · %s: sin estudio (%s)", model_name, key, e)
        return

    if args.select_by_deploy:
        # Fix #20 / AK9: dentro del top-K, study.best_params (argmin del objetivo barato)
        # NO es la config que mejor se despliega. Re-elige best_params con el deploy-score
        # que rank_check ya pagó (MASE de SELECCIÓN del walk-forward; hold-out intacto).
        # Correr DESPUÉS de --rank-check y ANTES de confirm_tuning: así confirm acepta
        # exactamente la config que se desplegará.
        tuned: dict[str, dict] = json.loads(args.out.read_text()) if args.out.exists() else {}
        for model_name in args.models:
            for key in args.groups:
                table, block = key.split("_", 1)
                entry = tuned.get(model_name, {}).get(key)
                if entry is None:
                    log.warning("select-by-deploy %s · %s: sin candidato en %s", model_name, key, args.out)
                    continue
                params = tune.deploy_winner_params(model_name, table=table, block=block, storage=args.storage)
                if not params:
                    log.warning(
                        "select-by-deploy %s · %s: sin rank-check válido; conservo el ganador por objetivo",
                        model_name,
                        key,
                    )
                    continue
                if params != entry.get("best_params"):
                    entry.setdefault("objective_best_params", entry.get("best_params", {}))
                    entry["best_params"] = params
                    entry["best_params_source"] = "deploy_rank"
                    log.info("select-by-deploy %s · %s: best_params <- ganador por deploy-score", model_name, key)
                else:
                    entry["best_params_source"] = "objective_eq_deploy"
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(tuned, indent=2))
        log.info("select-by-deploy listo -> %s (correr confirm_tuning para aceptar)", args.out)
        return

    # Merge sobre el archivo existente: re-tunear un grupo NO borra los candidatos
    # (ni las aceptaciones) de los demás modelos/grupos.
    results: dict[str, dict] = json.loads(args.out.read_text()) if args.out.exists() else {}
    for model_name in args.models:
        results.setdefault(model_name, {})
        for key in args.groups:
            table, block = key.split("_", 1)
            log.info("tuning %s · %s (%d trials)", model_name, key, args.n_trials)
            r = tune.tune(
                model_name, table=table, block=block, n_trials=args.n_trials, storage=args.storage, mlflow=args.mlflow
            )
            delta = (
                100 * (r.default_score - r.best_score) / r.default_score
                if math.isfinite(r.default_score) and r.default_score > 0
                else float("nan")
            )
            results[model_name][key] = {
                "best_params": r.best_params,
                "best_score": round(r.best_score, 4),
                "default_score": round(r.default_score, 4),
                "delta_pct": round(delta, 1) if math.isfinite(delta) else None,
                "study_name": r.study_name,
                "n_trials": r.n_trials,
                "n_pruned": r.n_pruned,
                # AK6: candidato pendiente — confirm_tuning decide la aceptación en
                # val-confirm y voltea "improved" (la llave que lee models._tree_params).
                "improved": False,
                "improved_tuning_val": bool(r.best_score < r.default_score),
            }
            log.info(
                "  %s %s: default=%.4f -> mejor=%.4f (%+.1f%%) [%d trials, %d pruned] CANDIDATO",
                model_name,
                key,
                r.default_score,
                r.best_score,
                delta,
                r.n_trials,
                r.n_pruned,
            )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(results, indent=2))  # persistir incremental
            if args.mlflow:  # run resumen del estudio (AK5)
                from vp_data import tracking

                tracking.log_run(
                    f"hpo_{model_name}_{table}",
                    f"{r.study_name}-summary",
                    params={"model": model_name, "table": table, "block": block}
                    | {f"best_{k}": v for k, v in r.best_params.items()},
                    metrics={
                        "best_score": r.best_score,
                        "default_score": r.default_score,
                        "delta_pct": delta,
                        "n_pruned": r.n_pruned,
                        "n_trials": r.n_trials,
                    },
                    tags={"layer": "hpo", "kind": "summary", "study": r.study_name},
                )
    log.info("listo -> %s (candidatos; correr confirm_tuning para aceptar)", args.out)


if __name__ == "__main__":
    main()
