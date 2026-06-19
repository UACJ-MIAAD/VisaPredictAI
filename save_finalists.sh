#!/bin/bash
# Guarda TODOS los modelos finalistas (globales deep + locales por serie) para comparar,
# graficar y explotar; exporta sus pronósticos hold-out; y sincroniza modelos+forecasts a S3.
# Correr en background:  bash save_finalists.sh > reports/finalists.log 2>&1
set -u
cd "$(dirname "$0")"
rm -f models/manifest.jsonl   # manifiesto fresco
echo "=== GUARDAR FINALISTAS $(date) ==="
echo ">>> [1/4] modelos GLOBALES deep (ante_nf)"
ante_nf/bin/python save_finalists_deep.py || true
echo ">>> [2/4] modelos LOCALES por serie (ante)"
ante/bin/python save_finalists.py || true
echo ">>> [3/4] pronosticos finalistas -> CSV tidy (ante)"
ante/bin/python export_forecasts.py || true
echo ">>> [4/4] commit forecasts + sync (DVC->S3 + git)"
git add reports/finalist_forecasts_*.csv 2>/dev/null
bash sync_all.sh "finalistas: $(ls models -R 2>/dev/null | grep -c model) modelos + forecasts ($(date +%Y-%m-%d))" || true
echo "=== FINALISTAS LISTOS $(date) ==="
