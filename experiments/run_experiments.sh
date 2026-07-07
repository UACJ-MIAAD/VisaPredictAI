#!/bin/zsh
# Barrido de variantes deep (DeepAR-fix / pooling / HPO). Correr desde cualquier cwd.
# pipefail + `|| true` SOLO en el grep: un python caído propaga su exit code (el grep
# sin matches ya no lo enmascara — E1), y un grep sin líneas no tumba la corrida.
set -e -o pipefail
cd "$(dirname "$0")/.."
PY=./ante_nf/bin/python
[ -x "$PY" ] || { echo "ERROR: falta el venv ante_nf/ en la raíz" >&2; exit 1; }
export PYTHONWARNINGS=ignore
echo "=== A) DeepAR-fix: FAD family diff + local_scaler (8 modelos) ==="
$PY experiments/run_global_deep.py --table FAD --block family --diff --local-scaler --max-steps 800 --suffix diff_ls 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }
echo "=== C) Pooling familiar+EB: FAD both diff + local_scaler ==="
$PY experiments/run_global_deep.py --table FAD --block both --diff --local-scaler --max-steps 800 --suffix both_ls 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }
echo "=== B) AutoModels HPO: FAD family diff + auto + local_scaler (4 modelos x 15 trials) ==="
$PY experiments/run_global_deep.py --table FAD --block family --diff --auto --local-scaler --num-samples 15 --suffix auto 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }
echo "=== EXPERIMENTOS DONE ==="
