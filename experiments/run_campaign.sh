#!/bin/bash
# Campaña de experimentos F1+F2 (sólido sin GPU, todo tracked a MLflow vía tracking JSONL).
# F1: pool local de 23 modelos (incluye el híbrido ARIMA-LSTM y los pisos naive1/drift)
#     × FAD/DFF × familia/empleo. (Los CSV conservan el sufijo histórico *_pool_*.)
# F2: deep global — matriz de variantes (espacio-target × normalización × HPO) × multi-semilla.
#     HPO deep (AK8c): UNA búsqueda de 40 trials por modelo y el ganador re-entrenado
#     con 5 semillas (antes: 5 búsquedas independientes de 15 = 75 trials desperdiciados).
# Cada paso es independiente (|| true): un fallo no aborta la campaña. Correr en background:
#   bash experiments/run_campaign.sh > reports/campaign.log 2>&1
# Al terminar: ante_nf/bin/python experiments/sync_mlflow.py  &&  mlflow ui --backend-store-uri sqlite:///mlflow.db
set -uo pipefail
cd "$(dirname "$0")/.."   # los intérpretes y las rutas de salida viven en la RAÍZ del repo
ANTE=ante/bin/python
NF=ante_nf/bin/python
# Guard fail-loud: sin esto, con el cwd/venv equivocado los `|| true` de abajo convertían
# TODA la campaña en un no-op silencioso que imprimía éxito (E1).
[ -x "$ANTE" ] && [ -x "$NF" ] || { echo "ERROR: faltan venvs ante/ y/o ante_nf/ en la raíz" >&2; exit 1; }
SEEDS="1 2 3 4 5"
echo "=== CAMPAÑA arranca $(date) ==="

# ---------- F1: pool local 23 modelos (tracked) ----------
for table in FAD DFF; do
  for block in family employment; do
    echo ">>> F1 pool $table/$block $(date)"
    $ANTE -m vp_model.run_comparison --country all --table "$table" --block "$block" --mlflow \
      --out "reports/campaign/campaign_pool_${table}_${block}.csv" || true
  done
done

# ---------- F2: deep global — matriz de variantes × multi-semilla ----------
# Variantes deterministas (4 modelos por corrida): nivel, diferencia, diff+norma-por-serie.
DET_MODELS="BiTCN PatchTST TiDE NHITS"
for table in FAD DFF; do
  for seed in $SEEDS; do
    $NF experiments/run_global_deep.py --table "$table" --block family --max-steps 800 --models $DET_MODELS \
      --seed "$seed" --suffix "camp_levels_s${seed}" || true
    $NF experiments/run_global_deep.py --table "$table" --block family --diff --max-steps 800 --models $DET_MODELS \
      --seed "$seed" --suffix "camp_diff_s${seed}" || true
    $NF experiments/run_global_deep.py --table "$table" --block family --diff --local-scaler --max-steps 800 \
      --models $DET_MODELS --seed "$seed" --suffix "camp_diffls_s${seed}" || true
  done
  # Variante con HPO (Auto*), AK8c: UNA búsqueda (40 trials, arquitectura + early stop)
  # que persiste la config ganadora por modelo, y el ganador re-entrenado con las 5
  # semillas. El sufijo de búsqueda NO empieza con camp_auto_s para no contaminar la
  # agregación multi-semilla por prefijo.
  $NF experiments/run_global_deep.py --table "$table" --block family --diff --auto --num-samples 40 \
    --models AutoBiTCN AutoTiDE AutoNHITS --seed 1 --suffix "camp_hposearch" || true
  for seed in $SEEDS; do
    $NF experiments/run_global_deep.py --table "$table" --block family --diff \
      --models BiTCN TiDE NHITS --config "reports/campaign/hpo_deep_best_${table}_Auto{model}.json" \
      --seed "$seed" --suffix "camp_auto_s${seed}" || true
  done
done

# ---------- F2: agregación multi-semilla -> MLflow (media ± IC por modelo×variante) ----------
echo ">>> F2 agregación tracked $(date)"
for table in FAD DFF; do
  for m in $DET_MODELS; do
    for v in camp_levels_s camp_diff_s camp_diffls_s; do
      $ANTE experiments/aggregate_seeds.py --table "$table" --prefix "$v" --model "$m" --mlflow || true
    done
  done
  for m in AutoBiTCN AutoTiDE AutoNHITS; do
    $ANTE experiments/aggregate_seeds.py --table "$table" --prefix camp_auto_s --model "$m" --mlflow || true
  done
done

# ---------- F2: ensembles (combinaciones) -> MLflow ----------
echo ">>> F2 ensembles tracked $(date)"
$ANTE experiments/run_ensembles.py --mlflow || true

# ---------- TODO MACHIN: MLflow + DVC->S3 + git ----------
echo ">>> sync_all (MLflow + DVC->S3 + git) $(date)"
bash experiments/sync_all.sh "campaña: MLflow + DVC->S3 ($(date +%Y-%m-%d))" || true
echo "=== CAMPAÑA termina $(date) ==="
