#!/bin/bash
# Guarda TODOS los modelos finalistas (globales deep + locales por serie) para comparar,
# graficar y explotar; exporta sus pronósticos hold-out; y sincroniza modelos+forecasts a S3.
# Correr en background:  bash experiments/save_finalists.sh > reports/finalists.log 2>&1
# Los `|| true` por paso son deliberados (un paso caído no aborta la colección), por eso el
# guard de venvs es obligatorio: sin él, un cwd/venv equivocado era un no-op "exitoso" (E1).
set -uo pipefail
cd "$(dirname "$0")/.."
[ -x ante/bin/python ] && [ -x ante_nf/bin/python ] || { echo "ERROR: faltan venvs ante/ y/o ante_nf/ en la raíz" >&2; exit 1; }
rm -f models/manifest.jsonl   # manifiesto fresco
echo "=== GUARDAR FINALISTAS $(date) ==="
echo ">>> [1/4] modelos GLOBALES deep (ante_nf)"
ante_nf/bin/python experiments/save_finalists_deep.py || true
echo ">>> [2/4] modelos LOCALES por serie (ante)"
ante/bin/python experiments/save_finalists.py || true
echo ">>> [3/4] pronosticos finalistas -> CSV tidy (ante)"
ante/bin/python experiments/export_forecasts.py || true
echo ">>> [4/4] commit forecasts + sync (DVC->S3 + git)"
git add reports/eval/finalist_forecasts_*.csv 2>/dev/null
bash experiments/sync_all.sh "finalistas: $(ls models -R 2>/dev/null | grep -c model) modelos + forecasts ($(date +%Y-%m-%d))" || true
echo "=== FINALISTAS LISTOS $(date) ==="
