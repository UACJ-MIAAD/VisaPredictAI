#!/bin/bash
# Campaña de experimentos F1+F2 (sólido sin GPU, todo tracked a MLflow vía tracking JSONL).
# F1: pool local de 24 modelos (incluye el híbrido ARIMA-LSTM y los pisos naive1/drift)
#     × FAD/DFF × familia/empleo. (Los CSV conservan el sufijo histórico *_pool_*.)
# F2: deep global — matriz de variantes (espacio-target × normalización × HPO) × multi-semilla.
#     HPO deep (AK8c): UNA búsqueda de 40 trials por modelo y el ganador re-entrenado
#     con 5 semillas (antes: 5 búsquedas independientes de 15 = 75 trials desperdiciados).
# FAIL-CLOSED (auditoría 12-jul-2026): cada PASO acumula su fallo y la campaña TERMINA EN
# ROJO (exit≠0) si algún paso falló. Antes cada paso llevaba `|| true` y el script salía 0
# aunque fallaran F1/F2/agregación/ensembles/sync — un falso éxito que el run_req de
# run_rederivation.sh no podía detectar. Los fallos POR-MODELO en series cortas ocurren
# DENTRO de run_comparison (que sale 0 con su pool CSV), así que capturar el exit de cada
# PASO es lo correcto. Correr en background:
#   bash experiments/run_campaign.sh > reports/campaign.log 2>&1
set -uo pipefail
cd "$(dirname "$0")/.."   # los intérpretes y las rutas de salida viven en la RAÍZ del repo
# R9.4: bootstrap orquestador (tools.python_env es stdlib-only). La LÓGICA DE PRODUCTO corre en los
# entornos `model`/`deep-cpu` content-addressed que abre `run-command`, jamás en el python ambiental.
PYBOOT=${PYBOOT:-python3}
command -v "$PYBOOT" >/dev/null 2>&1 || { echo "ERROR: falta $PYBOOT (bootstrap del orquestador)" >&2; exit 1; }
runc() { "$PYBOOT" -m tools.python_env run-command --id "$1" -- "${@:2}"; }
SEEDS="1 2 3 4 5"
CAMP_FAILS=0
step() { local label="$1"; shift; echo ">>> $label $(date)"; "$@" || { echo "##### PASO FALLIDO (exit $?): $label :: $*"; CAMP_FAILS=$((CAMP_FAILS+1)); }; }
echo "=== CAMPAÑA arranca $(date) ==="
echo "campaign_id=${CAMPAIGN_ID:-standalone}  ·  sha=${CAMPAIGN_SHA:-$(git rev-parse --short HEAD)}"

# ---------- F1: pool local 24 modelos (tracked) ----------
for table in FAD DFF; do
  for block in family employment; do
    step "F1 pool $table/$block" runc run_comparison --country all --table "$table" --block "$block" --mlflow \
      --out "reports/campaign/campaign_pool_${table}_${block}.csv"
  done
done

# ---------- F2: deep global — matriz de variantes × multi-semilla ----------
# Variantes deterministas (4 modelos por corrida): nivel, diferencia, diff+norma-por-serie.
DET_MODELS="BiTCN PatchTST TiDE NHITS"
for table in FAD DFF; do
  for seed in $SEEDS; do
    step "deep levels $table s$seed" runc run_global_deep --table "$table" --block family --max-steps 800 --models $DET_MODELS \
      --seed "$seed" --suffix "camp_levels_s${seed}"
    step "deep diff $table s$seed" runc run_global_deep --table "$table" --block family --diff --max-steps 800 --models $DET_MODELS \
      --seed "$seed" --suffix "camp_diff_s${seed}"
    step "deep diffls $table s$seed" runc run_global_deep --table "$table" --block family --diff --local-scaler --max-steps 800 \
      --models $DET_MODELS --seed "$seed" --suffix "camp_diffls_s${seed}"
  done
  # Variante con HPO (Auto*), AK8c: UNA búsqueda (40 trials, arquitectura + early stop)
  # que persiste la config ganadora por modelo, y el ganador re-entrenado con las 5
  # semillas. El sufijo de búsqueda NO empieza con camp_auto_s para no contaminar la
  # agregación multi-semilla por prefijo.
  step "deep hposearch $table" runc run_global_deep --table "$table" --block family --diff --auto --num-samples 40 \
    --models AutoBiTCN AutoTiDE AutoNHITS --seed 1 --suffix "camp_hposearch"
  for seed in $SEEDS; do
    step "deep auto $table s$seed" runc run_global_deep --table "$table" --block family --diff \
      --models BiTCN TiDE NHITS --config "reports/campaign/hpo_deep_best_${table}_Auto{model}.json" \
      --seed "$seed" --suffix "camp_auto_s${seed}"
  done
done

# ---------- F2: agregación multi-semilla -> MLflow (media ± IC por modelo×variante) ----------
echo ">>> F2 agregación tracked $(date)"
for table in FAD DFF; do
  for m in $DET_MODELS; do
    for v in camp_levels_s camp_diff_s camp_diffls_s; do
      step "agg $table $m $v" runc aggregate_seeds --table "$table" --prefix "$v" --model "$m" --mlflow
    done
  done
  for m in AutoBiTCN AutoTiDE AutoNHITS; do
    step "agg $table $m auto" runc aggregate_seeds --table "$table" --prefix camp_auto_s --model "$m" --mlflow
  done
done

# ---------- F2: ensembles (combinaciones) -> MLflow ----------
step "F2 ensembles" runc run_ensembles --mlflow

# ---------- MLflow + DVC re-hash LOCAL (NO publica; auditoría 12-jul-2026) ----------
# sync_all sin --publish: sincroniza MLflow y re-hashea con dvc add, pero NO hace
# git push / dvc push. Publicar es un paso humano posterior a la validación.
# SYNC_PUBLISH=0 explícito: aunque el entorno lo herede en 1, la campaña NUNCA publica.
step "sync_all LOCAL (sin push)" env SYNC_PUBLISH=0 bash experiments/sync_all.sh "campaña: MLflow + DVC local ($(date +%Y-%m-%d))"

echo "=== CAMPAÑA termina $(date) · pasos fallidos: $CAMP_FAILS ==="
if [ "$CAMP_FAILS" -gt 0 ]; then
  echo "✗ CAMPAÑA FALLIDA: $CAMP_FAILS paso(s) rotos. NO es un éxito." >&2
  exit 1
fi
exit 0
