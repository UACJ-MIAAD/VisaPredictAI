#!/bin/zsh
# Barrido de variantes deep (DeepAR-fix / pooling / HPO). Correr desde cualquier cwd.
# pipefail + `|| true` SOLO en el grep: un python caído propaga su exit code (el grep
# sin matches ya no lo enmascara — E1), y un grep sin líneas no tumba la corrida.
set -e -o pipefail
cd "$(dirname "$0")/.."
# R9.4: bootstrap orquestador; el deep corre en el entorno `deep/cpu` content-addressed (run-command).
PYBOOT=${PYBOOT:-python3}
command -v "$PYBOOT" >/dev/null 2>&1 || { echo "ERROR: falta $PYBOOT (bootstrap del orquestador)" >&2; exit 1; }
runc() { "$PYBOOT" -m tools.python_env run-command --id "$1" -- "${@:2}"; }
export PYTHONWARNINGS=ignore
echo "=== A) DeepAR-fix: FAD family diff + local_scaler (8 modelos) ==="
runc run_global_deep --table FAD --block family --diff --local-scaler --max-steps 800 --suffix diff_ls 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }
echo "=== C) Pooling familiar+EB: FAD both diff + local_scaler ==="
runc run_global_deep --table FAD --block both --diff --local-scaler --max-steps 800 --suffix both_ls 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }
echo "=== B) AutoModels HPO: FAD family diff + auto + local_scaler (4 modelos x 15 trials) ==="
runc run_global_deep --table FAD --block family --diff --auto --local-scaler --num-samples 15 --suffix auto 2>&1 | { grep -E "panel:|✓|✗|guardado" || true; }
echo "=== EXPERIMENTOS DONE ==="
