"""Corre el HPO de los GBMs sobre los grupos piloto y guarda los mejores hiperparámetros.

Tunea xgboost/lightgbm/catboost para (tabla × bloque) con el objetivo barato y
leakage-free de ``tune`` (cola de validación interna; el hold-out de 24m NUNCA se
toca). Escribe ``reports/tuned_params.json`` con {modelo: {tabla_bloque: params}} y un
resumen default-vs-mejor. La regla de ACEPTACIÓN final (tuned mejora en hold-out) se
confirma aparte (US-O3); aquí solo se buscan los candidatos.
"""

from __future__ import annotations

import json
from pathlib import Path

from vp_model import config, tune
from vp_model.config import get_logger

log = get_logger("run_tuning")
OUT = Path(__file__).resolve().parent.parent / "reports" / "tuned_params.json"


def main(n_trials: int = 40, groups: tuple[tuple[str, str], ...] = (("FAD", "family"), ("DFF", "family"))) -> None:
    results: dict[str, dict] = {}
    for model_name in sorted(config.DIFFERENCED):
        results[model_name] = {}
        for table, block in groups:
            key = f"{table}_{block}"
            log.info("tuning %s · %s (%d trials)", model_name, key, n_trials)
            r = tune.tune(model_name, table=table, block=block, n_trials=n_trials)
            improved = r.best_score < r.default_score
            results[model_name][key] = {
                "best_params": r.best_params,
                "best_score": round(r.best_score, 4),
                "default_score": round(r.default_score, 4),
                "improved": improved,
                "delta_pct": round(100 * (r.default_score - r.best_score) / r.default_score, 1),
            }
            log.info(
                "  %s %s: default=%.4f -> mejor=%.4f (%+.1f%%) %s",
                model_name, key, r.default_score, r.best_score,
                100 * (r.default_score - r.best_score) / r.default_score,
                "MEJORA" if improved else "sin mejora",
            )
            OUT.write_text(json.dumps(results, indent=2))  # persistir incremental
    log.info("listo -> %s", OUT)


if __name__ == "__main__":
    main()
