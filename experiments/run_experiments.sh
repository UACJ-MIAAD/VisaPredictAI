#!/bin/zsh
set -e
cd /Users/haowei/Documents/Anteproyecto/VisaPredictAI
PY=./ante_nf/bin/python
export PYTHONWARNINGS=ignore
echo "=== A) DeepAR-fix: FAD family diff + local_scaler (8 modelos) ==="
$PY experiments/run_global_deep.py --table FAD --block family --diff --local-scaler --max-steps 800 --suffix diff_ls 2>&1 | grep -E "panel:|✓|✗|guardado"
echo "=== C) Pooling familiar+EB: FAD both diff + local_scaler ==="
$PY experiments/run_global_deep.py --table FAD --block both --diff --local-scaler --max-steps 800 --suffix both_ls 2>&1 | grep -E "panel:|✓|✗|guardado"
echo "=== B) AutoModels HPO: FAD family diff + auto + local_scaler (4 modelos x 15 trials) ==="
$PY experiments/run_global_deep.py --table FAD --block family --diff --auto --local-scaler --num-samples 15 --suffix auto 2>&1 | grep -E "panel:|✓|✗|guardado"
echo "=== EXPERIMENTOS DONE ==="
