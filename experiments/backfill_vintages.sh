#!/usr/bin/env bash
# Siembra REPRODUCIBLE del ledger de evaluación prospectiva (reports/forecast_log.csv).
#
# La añada en vivo (origen = último mes F de cada serie) la produce el pipeline normal;
# aquí añadimos añadas HISTÓRICAS leakage-free (`as_of`) para que el scorecard tenga
# objetivos ya realizados y arroje métricas desde hoy (en vez de esperar 12 meses).
# Todo sale del pipeline (generate_web_forecasts.py) — sin parches manuales. El ledger
# es idempotente (dedup por origen+serie+fecha), así que re-correr no duplica.
#
# Uso:  bash experiments/backfill_vintages.sh        (corre desde la raíz del repo)
#       PY=python bash experiments/backfill_vintages.sh   (override del intérprete)
set -euo pipefail
PY=${PY:-ante/bin/python}

# Añadas históricas a sembrar (origen del pronóstico). Cada una predice 12 meses ya
# observados → 100 % evaluables. Ampliar/recortar según se quiera más cobertura.
VINTAGES=(2024-07 2025-01 2025-07)

echo "[backfill] live vintage (also serves the web) ..."
"$PY" experiments/generate_web_forecasts.py

for m in "${VINTAGES[@]}"; do
  echo "[backfill] historical vintage ${m} ..."
  "$PY" experiments/generate_web_forecasts.py "${m}"
done

echo "[backfill] prospective scoring ..."
"$PY" experiments/score_forecasts.py
echo "[backfill] done."
