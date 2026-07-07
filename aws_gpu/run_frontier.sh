#!/usr/bin/env bash
# ============================================================================
#  Orquestador de UN comando — barrido frontier + multi-semilla en la GPU.
#  (VisaPredict AI · bundle aws_gpu · correr en una EC2 g5.xlarge — ver GUIA_EC2.md)
#
#  Uso (en la instancia, dentro de ~/run/aws_gpu con el venv activo, en tmux):
#     bash run_frontier.sh                       # panel en ../visa_panel_long.parquet
#     bash run_frontier.sh /ruta/al/panel.parquet
#
#  Corre las 4 fases del plan probado (multi-semilla del ganador + frontier pesado,
#  FAD y DFF), guarda cada resultado en reports/campaign/global_*.csv y — en la
#  ÚLTIMA fase — APAGA la instancia sola (--shutdown-on-done, red de seguridad de
#  costo). Cada train_gpu aísla los fallos por modelo (un modelo que reviente NO
#  aborta el resto). Luego bajas los CSV a la Mac y evalúas (§5–6 de la guía).
#
#  Baseline a batir (hold-out MASE, key_facts al 6-jul-2026): FAD AutoBiTCN 0.109 ·
#  campeón desplegado median(theta+ets+sarima) 0.121 · DFF campeón SARIMA 0.100 ·
#  piso naive1 0.100. La comparación real se hace en la Mac con eval_neuralforecast.
# ============================================================================
set -uo pipefail
PANEL="${1:-../visa_panel_long.parquet}"
PY="${PY:-python}"
say() { echo ""; echo "########## $* — $(date '+%F %T') ##########"; }

[ -f "$PANEL" ] || { echo "ERROR: no encuentro el panel en '$PANEL' (súbelo con scp, ver GUIA_EC2.md §2)"; exit 1; }

say "pre-vuelo: self-check + CUDA"
$PY train_gpu.py --selfcheck || { echo "self-check FALLÓ"; exit 1; }
$PY -c "import torch; assert torch.cuda.is_available(), 'CUDA NO disponible — ¿AMI/instancia GPU?'; print('CUDA OK:', torch.cuda.get_device_name(0))" \
  || { echo "CUDA no disponible — detén y revisa la instancia"; exit 1; }
echo "panel: $PANEL"; $PY -c "import pandas as pd; p=pd.read_parquet('$PANEL'); print(f'  {len(p):,} filas · {p.bulletin_date.nunique()} meses')"

# NOTA: train_gpu.py DIFERENCIA por defecto; el flag es `--no-diff` (para niveles). NO pasar
# `--diff` (no existe -> argparse aborta la fase). La receta ganadora = diferencia => sin flag.
# ---- 1) CONFIRMAR el ganador con HPO AMPLIO + multi-semilla (lo que el CPU no alcanza) ----
say "1/4 · AutoBiTCN FAD — HPO amplio (80 trials) × 10 semillas → IC del ganador"
$PY train_gpu.py --panel "$PANEL" --table FAD --local-scaler \
    --auto --models AutoBiTCN --num-samples 80 --seeds 1 2 3 4 5 6 7 8 9 10

say "2/4 · AutoBiTCN + AutoTiDE DFF — HPO (50 trials) × 5 semillas"
$PY train_gpu.py --panel "$PANEL" --table DFF \
    --auto --models AutoBiTCN AutoTiDE --num-samples 50 --seeds 1 2 3 4 5

# ---- 2) FRONTIER pesado global — ¿la CAPACIDAD bate al AutoBiTCN? ----
say "3/4 · frontier pesado FAD (Informer/Autoformer/FEDformer/PatchTST/TimesNet, max_steps=2000)"
$PY train_gpu.py --panel "$PANEL" --table FAD --local-scaler \
    --models Informer Autoformer FEDformer PatchTST TimesNet --max-steps 2000

say "4/4 · frontier pesado DFF (+ APAGA la instancia al terminar)"
$PY train_gpu.py --panel "$PANEL" --table DFF \
    --models Informer Autoformer FEDformer PatchTST TimesNet --max-steps 2000 \
    --shutdown-on-done

# (Nota: si NO quieres que apague sola, quita --shutdown-on-done de la fase 4/4 y
#  apaga/termina la instancia a mano — ver §7 de GUIA_EC2.md. Chronos zero-shot es
#  opcional: python chronos_lora.py zeroshot --panel "$PANEL" --table FAD)
say "LISTO — baja reports/campaign/global_*.csv a la Mac y evalúa (aggregate_seeds + eval_neuralforecast)"
