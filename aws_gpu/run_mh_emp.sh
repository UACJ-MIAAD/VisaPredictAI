#!/usr/bin/env bash
# ============================================================================
#  Showdown MULTI-HORIZONTE — bloque EMPLEO (62% Current = ramp de fecha-boletín,
#  donde el random walk fracasa y los modelos con tendencia deberían aplastarlo).
#  Espejo de run_mh.sh pero --block employment. Salidas en reports/campaign/mh/
#  como global_{table}_employment_h36_*.csv (el naming lleva el bloque).
# ============================================================================
set -uo pipefail
PANEL="${1:-../visa_panel_long.parquet}"
PY="${PY:-python}"
OUT="reports/campaign/mh"
BLK="employment"
say() { echo ""; echo "########## $* — $(date '+%F %T') ##########"; }

[ -f "$PANEL" ] || { echo "ERROR: panel no encontrado en '$PANEL'"; exit 1; }
mkdir -p "$OUT"

say "MHEMP 1/4 · frontier directo FAD/employment h=36"
$PY train_gpu.py --panel "$PANEL" --table FAD --block "$BLK" --horizon 36 --local-scaler \
    --models Informer Autoformer FEDformer PatchTST TimesNet TiDE NHITS BiTCN --max-steps 2000 --out-dir "$OUT"

say "MHEMP 2/4 · frontier directo DFF/employment h=36"
$PY train_gpu.py --panel "$PANEL" --table DFF --block "$BLK" --horizon 36 \
    --models Informer Autoformer FEDformer PatchTST TimesNet TiDE NHITS BiTCN --max-steps 2000 --out-dir "$OUT"

say "MHEMP 3/4 · Auto FAD/employment h=36 (HPO 40 × 3 semillas)"
$PY train_gpu.py --panel "$PANEL" --table FAD --block "$BLK" --horizon 36 --local-scaler --auto \
    --models AutoBiTCN AutoTiDE AutoPatchTST --num-samples 40 --seeds 1 2 3 --out-dir "$OUT"

say "MHEMP 4/4 · Auto DFF/employment h=36 (HPO 40 × 3 semillas)"
$PY train_gpu.py --panel "$PANEL" --table DFF --block "$BLK" --horizon 36 --auto \
    --models AutoBiTCN AutoTiDE AutoPatchTST --num-samples 40 --seeds 1 2 3 --out-dir "$OUT"

say "MHEMP LISTO — global_*_employment_h36_*.csv en reports/campaign/mh/"
