#!/bin/zsh
# Corrida nocturna: entrenamiento GLOBAL profundo a escala, secuencial (sin contención CPU).
# Lanzar SOLO cuando el barrido EB haya liberado la CPU. Salidas en reports/campaign/global_*.csv.
# Variante DIFERENCIADA primero (la apuesta fuerte), luego niveles; FAD y DFF; familiar+EB apilados.
# pipefail + `|| true` SOLO en el grep: un python caído propaga su exit code (E1).
set -e -o pipefail
cd "$(dirname "$0")/.."
# R9.4: bootstrap orquestador; el deep corre en el entorno `deep/cpu` content-addressed (run-command).
PYBOOT=${PYBOOT:-python3.14}
command -v "$PYBOOT" >/dev/null 2>&1 || { echo "ERROR: falta $PYBOOT (bootstrap del orquestador)" >&2; exit 1; }
runc() { "$PYBOOT" -m tools.python_env run-command --id "$1" -- "${@:2}"; }
export PYTHONWARNINGS=ignore
MS=1000

echo "=== [1/4] FAD diff (familiar+EB, max_steps=$MS) ==="
runc run_global_deep --table FAD --block both --diff --max-steps $MS 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }

echo "=== [2/4] FAD levels ==="
runc run_global_deep --table FAD --block both --max-steps $MS 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }

echo "=== [3/4] DFF diff ==="
runc run_global_deep --table DFF --block both --diff --max-steps $MS 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }

echo "=== [4/4] DFF levels ==="
runc run_global_deep --table DFF --block both --max-steps $MS 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }

echo "=== OVERNIGHT GLOBAL DONE ==="
