#!/usr/bin/env bash
# Siembra REPRODUCIBLE del ledger de evaluación prospectiva (reports/prospective/forecast_log.csv).
#
# La añada EN VIVO usa, por serie, su PROPIO último mes F como origen. La mayoría de las
# series termina en el último boletín (p.ej. 2026-07), pero las series mayormente Current
# terminan antes → producen orígenes 2026-06, 2026-03, 2023-09, 2022-04, 2021-11, 2015-08.
# Por eso la corrida en vivo, ELLA SOLA, genera ~7 añadas distintas. Sumando las 3 añadas
# HISTÓRICAS leakage-free de abajo (as_of), el ledger reproducible tiene 10 añadas.
# Todo sale del pipeline (generate_web_forecasts.py, semilla fija) — sin parches manuales.
# El ledger es idempotente (dedup por origen+serie+fecha), así que re-correr no duplica.
#
# PRERREQUISITO: la BD DuckDB debe existir → `make panel && make db` en un clon nuevo.
# Uso:  bash experiments/backfill_vintages.sh        (desde cualquier cwd; se ancla a la raíz)
#       PYBOOT=python3.14 bash experiments/backfill_vintages.sh   (override del bootstrap orquestador)
set -euo pipefail
cd "$(dirname "$0")/.."
# R9.4: el bootstrap SOLO orquesta (tools.python_env es stdlib-only). La LÓGICA DE PRODUCTO corre en el
# entorno `model` content-addressed que abre `run-command`, jamás en el python ambiental.
PYBOOT=${PYBOOT:-python3}
command -v "$PYBOOT" >/dev/null 2>&1 || { echo "ERROR: falta $PYBOOT (bootstrap del orquestador)" >&2; exit 1; }
runc() { "$PYBOOT" -m tools.python_env run-command --id "$1" -- "${@:2}"; }

# Añadas históricas a sembrar (origen del pronóstico). Cada una predice 12 meses ya
# observados → 100 % evaluables. Ampliar/recortar según se quiera más cobertura.
# ⚠️ CAVEAT (C3): estas 3 añadas caen DENTRO del hold-out que seleccionó a la receta
# campeona → su "MASE prospectivo" es parcialmente in-selección (optimista). Las añadas
# genuinamente prospectivas son las que el cron congela a partir del despliegue (jun-2026+).
VINTAGES=(2024-07 2025-01 2025-07)

echo "[backfill] live vintage (also serves the web) ..."
runc generate_web_forecasts

for m in "${VINTAGES[@]}"; do
  echo "[backfill] historical vintage ${m} ..."
  runc generate_web_forecasts "${m}"
done

echo "[backfill] prospective scoring ..."
runc score_forecasts
echo "[backfill] done."
