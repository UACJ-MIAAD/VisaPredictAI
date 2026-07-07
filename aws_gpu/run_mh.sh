#!/usr/bin/env bash
# ============================================================================
#  Showdown MULTI-HORIZONTE (h=36) — deep en su cancha real vs los clásicos.
#  train_gpu.py --horizon 36 emite (unique_id, cutoff, ds, <modelos>, y, h);
#  la eval por horizonte (MASE a h=1/3/6/12/24/36) se hace en la Mac.
#  Salidas en reports/campaign/mh/ (separadas de los CSV h=1). NO auto-apaga
#  (el orquestador controla el shutdown para bajar resultados antes).
# ============================================================================
set -uo pipefail
PANEL="${1:-../visa_panel_long.parquet}"
PY="${PY:-python}"
OUT="reports/campaign/mh"
say() { echo ""; echo "########## $* — $(date '+%F %T') ##########"; }

[ -f "$PANEL" ] || { echo "ERROR: panel no encontrado en '$PANEL'"; exit 1; }
mkdir -p "$OUT"

# ---- Directos frontier (especialistas de horizonte largo), un modelo aislado por vez ----
say "MH 1/4 · frontier directo FAD h=36 (max_steps=2000)"
$PY train_gpu.py --panel "$PANEL" --table FAD --horizon 36 --local-scaler \
    --models Informer Autoformer FEDformer PatchTST TimesNet TiDE NHITS BiTCN --max-steps 2000 --out-dir "$OUT"

say "MH 2/4 · frontier directo DFF h=36 (max_steps=2000)"
$PY train_gpu.py --panel "$PANEL" --table DFF --horizon 36 \
    --models Informer Autoformer FEDformer PatchTST TimesNet TiDE NHITS BiTCN --max-steps 2000 --out-dir "$OUT"

# ---- Auto (HPO) de los mejores candidatos, 3 semillas para IC ----
say "MH 3/4 · Auto FAD h=36 (HPO 40 trials × 3 semillas)"
$PY train_gpu.py --panel "$PANEL" --table FAD --horizon 36 --local-scaler --auto \
    --models AutoBiTCN AutoTiDE AutoPatchTST --num-samples 40 --seeds 1 2 3 --out-dir "$OUT"

say "MH 4/4 · Auto DFF h=36 (HPO 40 trials × 3 semillas)"
$PY train_gpu.py --panel "$PANEL" --table DFF --horizon 36 --auto \
    --models AutoBiTCN AutoTiDE AutoPatchTST --num-samples 40 --seeds 1 2 3 --out-dir "$OUT"

say "MH LISTO — baja reports/campaign/mh/global_*_h36_*.csv a la Mac y evalúa por horizonte"
