#!/bin/zsh
# Corrida nocturna: entrenamiento GLOBAL profundo a escala, secuencial (sin contención CPU).
# Lanzar SOLO cuando el barrido EB haya liberado la CPU. Salidas en reports/campaign/global_*.csv.
# Variante DIFERENCIADA primero (la apuesta fuerte), luego niveles; FAD y DFF; familiar+EB apilados.
# pipefail + `|| true` SOLO en el grep: un python caído propaga su exit code (E1).
set -e -o pipefail
cd "$(dirname "$0")/.."
PY=./ante_nf/bin/python
[ -x "$PY" ] || { echo "ERROR: falta el venv ante_nf/ en la raíz" >&2; exit 1; }
export PYTHONWARNINGS=ignore
MS=1000

echo "=== [1/4] FAD diff (familiar+EB, max_steps=$MS) ==="
$PY experiments/run_global_deep.py --table FAD --block both --diff --max-steps $MS 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }

echo "=== [2/4] FAD levels ==="
$PY experiments/run_global_deep.py --table FAD --block both --max-steps $MS 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }

echo "=== [3/4] DFF diff ==="
$PY experiments/run_global_deep.py --table DFF --block both --diff --max-steps $MS 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }

echo "=== [4/4] DFF levels ==="
$PY experiments/run_global_deep.py --table DFF --block both --max-steps $MS 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }

echo "=== OVERNIGHT GLOBAL DONE ==="
