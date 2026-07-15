#!/bin/bash
# Guarda TODOS los modelos finalistas (globales deep + locales por serie) para comparar,
# graficar y explotar; exporta sus pronósticos hold-out; y re-hashea localmente.
# Correr en background:  bash experiments/save_finalists.sh > reports/finalists.log 2>&1
#
# FAIL-CLOSED (auditoría 12-jul-2026): cada paso acumula su fallo; el script SALE EN ROJO
# (exit≠0) si algún paso canónico falló. sync_all se invoca con SYNC_PUBLISH=0 explícito
# (NO publica aunque el entorno herede SYNC_PUBLISH=1). El guard de venvs es obligatorio:
# sin él, un cwd/venv equivocado era un no-op "exitoso" (E1).
set -uo pipefail
cd "$(dirname "$0")/.."
# R9.4: bootstrap orquestador; la lógica corre en los entornos `deep-cpu`/`model` content-addressed.
PYBOOT=${PYBOOT:-python3}
command -v "$PYBOOT" >/dev/null 2>&1 || { echo "ERROR: falta $PYBOOT (bootstrap del orquestador)" >&2; exit 1; }
runc() { "$PYBOOT" -m tools.python_env run-command --id "$1" -- "${@:2}"; }
FIN_FAILS=0
step() { local label="$1"; shift; echo ">>> $label"; "$@" || { echo "##### PASO FALLIDO (exit $?): $label :: $*"; FIN_FAILS=$((FIN_FAILS+1)); }; }
rm -f models/manifest.jsonl   # manifiesto fresco
echo "=== GUARDAR FINALISTAS $(date) ==="
step "[1/4] modelos GLOBALES deep (deep-cpu)" runc save_finalists_deep
step "[2/4] modelos LOCALES por serie (model)" runc save_finalists
step "[3/4] pronosticos finalistas -> CSV tidy (model)" runc export_forecasts
echo ">>> [4/4] stage forecasts + sync LOCAL (sin push)"
git add reports/eval/finalist_forecasts_*.csv 2>/dev/null
step "sync_all LOCAL (sin push)" env SYNC_PUBLISH=0 bash experiments/sync_all.sh "finalistas: $(ls models -R 2>/dev/null | grep -c model) modelos + forecasts ($(date +%Y-%m-%d))"
echo "=== FINALISTAS $(date) · pasos fallidos: $FIN_FAILS ==="
if [ "$FIN_FAILS" -gt 0 ]; then
  echo "✗ FINALISTAS FALLIDOS: $FIN_FAILS paso(s) rotos. NO es un éxito." >&2
  exit 1
fi
exit 0
