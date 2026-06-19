#!/bin/bash
# "TODO MACHIN": deja todo sincronizado tras una corrida de experimentos —
#   1. MLflow   : staging JSONL -> mlflow.db (todas las métricas/params/tags).
#   2. DVC -> S3: re-hashea y sube datos + modelos + mlflow.db (el tracking entero).
#   3. git      : commitea+pushea los pointers .dvc (para que cualquiera haga `dvc pull`).
# Idempotente: si nada cambió, no commitea. Requiere creds AWS (dvc push) y remoto git.
# Uso:  bash sync_all.sh ["mensaje de commit"]
set -u
cd "$(dirname "$0")"
MSG="${1:-experiments: sync MLflow + DVC->S3 ($(date +%Y-%m-%d' '%H:%M))}"

echo ">>> [1/3] MLflow: staging -> mlflow.db"
ante_nf/bin/python sync_mlflow.py

echo ">>> [2/3] DVC: re-hash + push a S3 (datos + modelos + tracking)"
dvc add models data/processed/visa_panel_long.parquet data/processed/visapredict.duckdb mlflow.db >/dev/null 2>&1
dvc push

echo ">>> [3/3] git: pointers .dvc"
git add models.dvc mlflow.db.dvc data/processed/visa_panel_long.parquet.dvc \
    data/processed/visapredict.duckdb.dvc .gitignore 2>/dev/null
if git diff --cached --quiet; then
    echo "    sin cambios que commitear"
else
    git commit -q -m "$MSG"
    git push
    echo "    commiteado + pusheado"
fi
echo "✓ sync_all OK ($(date +%H:%M))"
