#!/bin/bash
# "TODO MACHIN": deja todo sincronizado tras una corrida de experimentos —
#   1. MLflow   : staging JSONL -> mlflow.db (todas las métricas/params/tags).
#   2. DVC -> S3: re-hashea y sube modelos + mlflow.db (el tracking entero).
#   3. git      : commitea+pushea los pointers .dvc (para que cualquiera haga `dvc pull`).
# Idempotente: si nada cambió, no commitea. Requiere creds AWS (dvc push) y remoto git.
# NO toca los artefactos del pipeline de datos (panel/parquet/duckdb): esos los gobierna
# dvc.yaml (`dvc repro`); `dvc add` sobre salidas de stage lo rechaza DVC (E1/M3) y el
# .duckdb está des-trackeado a propósito (no determinista).
# Uso:  bash experiments/sync_all.sh ["mensaje de commit"]
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ante_nf/bin/python ] || { echo "ERROR: falta el venv ante_nf/ en la raíz del repo" >&2; exit 1; }
MSG="${1:-experiments: sync MLflow + DVC->S3 ($(date +%Y-%m-%d' '%H:%M))}"

echo ">>> [1/3] MLflow: staging -> mlflow.db"
ante_nf/bin/python experiments/sync_mlflow.py

echo ">>> [2/3] DVC: re-hash + push a S3 (modelos + tracking)"
dvc add models mlflow.db
dvc push

echo ">>> [3/3] git: pointers .dvc"
git add models.dvc mlflow.db.dvc .gitignore
if git diff --cached --quiet; then
    echo "    sin cambios que commitear"
else
    git commit -q -m "$MSG"
    git push
    echo "    commiteado + pusheado"
fi
echo "✓ sync_all OK ($(date +%H:%M))"
