#!/bin/bash
# AQ re-campaign (PLAN MODELOS BRUTAL, 4-jul-2026): full re-derivation of every
# canonical modeling figure AFTER the wave-1/wave-2 overhaul (honest baselines,
# fixed NN specs, F-masked HPO objective, real ensembles, per-horizon PI, 24-model
# catalog, 'year' covariate retired).
#
# Differences vs run_rederivation.sh (the B1-precedent runbook it supersedes for
# this campaign):
#   * PARALLEL lanes on independent stages (12-core Apple Silicon):
#       lane P = local pool, 21 non-GBM models (the GBM rows wait for tuning)
#       lane T = Optuna HPO (150 trials, family+employment) -> confirm -> rank-check
#       lane D = deep global (det variants x 5 seeds + HPO search 1x + winner refits)
#       lane X = statsforecast / auto-ARIMA / global GBM / hurdle (new families)
#   * GBM catalog rows run AFTER confirm_tuning: the published pool uses the
#     same-campaign ACCEPTED winners (the old runbook tuned after the pool).
#   * New deploy tail: per-horizon PI scales, ACI gamma, cone report, shadow vintage.
#   * No sync_all/git push here — the integrator commits/pushes after the guard run.
#
# Usage (repo root, clean tree):
#   caffeinate -is bash experiments/run_campaign_aq.sh > reports/campaign_aq.log 2>&1
set -uo pipefail
cd "$(dirname "$0")/.."
# R9.4: bootstrap orquestador; la lógica corre en los entornos content-addressed (runtime/model/deep-cpu).
PYBOOT=${PYBOOT:-python3}
command -v "$PYBOOT" >/dev/null 2>&1 || { echo "ERROR: falta $PYBOOT (bootstrap del orquestador)" >&2; exit 1; }
runc() { "$PYBOOT" -m tools.python_env run-command --id "$1" -- "${@:2}"; }
LOGDIR=reports/campaign_aq_logs
mkdir -p "$LOGDIR"

FAILS=0
stage() { echo ""; echo "##### [$1] $2 — $(date '+%F %T')"; }
run()   { "$@" || { echo "##### STAGE FAILED (exit $?): $*"; FAILS=$((FAILS+1)); }; }

NON_GBM="naive naive1 drift arima sarima prophet ets theta kalman llt lstm deepar arima_lstm dlinear nlinear nbeats nhits tide rlinear tft chronos"
GBM="xgboost lightgbm catboost"
DET_MODELS="BiTCN PatchTST TiDE NHITS"
SEEDS="1 2 3 4 5"

echo "=== AQ CAMPAIGN starts $(date) ==="
run runc print_panel_summary   # R9.4/B66: extraído del `-c` (tools/print_panel_summary.py)

stage 0 "fresh warehouse (modeling reads DuckDB and aborts if stale)"
run runc build_database

stage A "parallel lanes: pool(non-GBM) | HPO | deep | new families"

(   # ---- lane P: local pool, non-GBM models, both tables x blocks ----
    for table in FAD DFF; do
      for block in family employment; do
        echo ">>> lane P: pool $table/$block $(date)"
        runc run_comparison --country all --table "$table" --block "$block" --mlflow \
          --models $NON_GBM --out "reports/campaign/aq_pool_nongbm_${table}_${block}.csv" || echo "LANE-P FAILED $table/$block"
      done
    done
    echo "lane P done $(date)"
) > "$LOGDIR/lane_pool.log" 2>&1 &
PID_P=$!

(   # ---- lane T: Optuna HPO -> rank-check -> deploy-score select (#20) -> confirmation ----
    # FIX #20 (AK9): rank-check re-scores the top-K with the real deploy protocol; then
    # select-by-deploy overwrites best_params with that leakage-free winner BEFORE confirm,
    # so confirm accepts (and the GBM catalog deploys) exactly the config that ships.
    echo ">>> lane T: run_tuning $(date)"
    runc run_tuning --n-trials 150 --mlflow || echo "LANE-T FAILED run_tuning"
    runc run_tuning --rank-check --mlflow || echo "LANE-T FAILED rank-check"
    runc run_tuning --select-by-deploy || echo "LANE-T FAILED select-by-deploy"
    runc confirm_tuning --holdout-report --mlflow || echo "LANE-T FAILED confirm"
    echo "lane T done $(date)"
) > "$LOGDIR/lane_tuning.log" 2>&1 &
PID_T=$!

(   # ---- lane D: deep global (det variants x seeds, HPO search 1x, winner refits) ----
    for table in FAD DFF; do
      for seed in $SEEDS; do
        runc run_global_deep --table "$table" --block family --max-steps 800 --models $DET_MODELS \
          --seed "$seed" --suffix "camp_levels_s${seed}" || echo "LANE-D FAILED levels $table s$seed"
        runc run_global_deep --table "$table" --block family --diff --max-steps 800 --models $DET_MODELS \
          --seed "$seed" --suffix "camp_diff_s${seed}" || echo "LANE-D FAILED diff $table s$seed"
        runc run_global_deep --table "$table" --block family --diff --local-scaler --max-steps 800 \
          --models $DET_MODELS --seed "$seed" --suffix "camp_diffls_s${seed}" || echo "LANE-D FAILED diffls $table s$seed"
      done
      runc run_global_deep --table "$table" --block family --diff --auto --num-samples 40 \
        --models AutoBiTCN AutoTiDE AutoNHITS --seed 1 --suffix "camp_hposearch" || echo "LANE-D FAILED hposearch $table"
      for seed in $SEEDS; do
        runc run_global_deep --table "$table" --block family --diff \
          --models BiTCN TiDE NHITS --config "reports/campaign/hpo_deep_best_${table}_Auto{model}.json" \
          --seed "$seed" --suffix "camp_auto_s${seed}" || echo "LANE-D FAILED auto $table s$seed"
      done
    done
    echo "lane D done $(date)"
) > "$LOGDIR/lane_deep.log" 2>&1 &
PID_D=$!

(   # ---- lane X: new families + classic tuned baseline ----
    echo ">>> lane X: statsforecast $(date)"
    runc run_statsforecast --table both --refit 1 || echo "LANE-X FAILED statsforecast"
    echo ">>> lane X: auto-ARIMA $(date)"
    runc auto_arima_baseline || echo "LANE-X FAILED auto_arima"
    echo ">>> lane X: global GBM $(date)"
    runc run_global_gbm --table both --models lightgbm xgboost || echo "LANE-X FAILED global_gbm"
    echo ">>> lane X: hurdle $(date)"
    runc run_hurdle --table both || echo "LANE-X FAILED hurdle"
    runc run_hurdle --table both --threshold || echo "LANE-X FAILED hurdle-thr"
    echo "lane X done $(date)"
) > "$LOGDIR/lane_newfam.log" 2>&1 &
PID_X=$!

echo "lanes launched: P=$PID_P T=$PID_T D=$PID_D X=$PID_X"
wait $PID_T; echo "lane T (tuning) finished $(date)"

stage B "GBM catalog rows with same-campaign accepted winners (waits: lane T)"
for table in FAD DFF; do
  for block in family employment; do
    run runc run_comparison --country all --table "$table" --block "$block" --mlflow \
      --models $GBM --out "reports/campaign/aq_pool_gbm_${table}_${block}.csv"
  done
done

wait $PID_P; echo "lane P (pool) finished $(date)"
wait $PID_X; echo "lane X (new families) finished $(date)"

stage C1 "merge pool halves -> campaign_pool + model_comparison projections"
run runc merge_campaign_pools   # R9.4/B66: extraído del heredoc (tools/merge_campaign_pools.py)

wait $PID_D; echo "lane D (deep) finished $(date)"

stage C2 "deep multi-seed aggregation -> MLflow"
for table in FAD DFF; do
  for m in $DET_MODELS; do
    for v in camp_levels_s camp_diff_s camp_diffls_s; do
      run runc aggregate_seeds --table "$table" --prefix "$v" --model "$m" --mlflow
    done
  done
  for m in AutoBiTCN AutoTiDE AutoNHITS; do
    run runc aggregate_seeds --table "$table" --prefix camp_auto_s --model "$m" --mlflow
  done
done

stage C3 "finalists + fresh holdout/selection forecasts"
run bash experiments/save_finalists.sh
# save_finalists.sh does NOT write holdout_forecasts_{table}.csv (its sole writer is
# persist_forecasts) — yet champion-challenger / significance-DM / ensembles / stacking /
# FFORMA / build_key_facts (\factFadChampionMean etc.) all consume it. The AQ campaign ran
# persist_forecasts out of band; running it here (AFTER lane T, so catboost/lightgbm carry
# the accepted deploy-score winners from #20, and with the aligned Differenced backtest from
# #21a) keeps the champion figures consistent with the fresh pool/comparison tables (regla #0).
run runc persist_forecasts

stage C4 "combiners on fresh holdouts (best-K / stacking simplex / FFORMA / conformal)"
run runc run_ensembles --mlflow
run runc improve_stacking --mlflow
run runc improve_fforma --mlflow

stage C5 "probabilistic: deep-PI (36 windows x 3 seeds + CQR) + CRPS"
for t in FAD DFF; do
  run runc run_deep_pi --table "$t" --model BiTCN --max-steps 800
  run runc run_deep_pi --table "$t" --model BiTCN --max-steps 800 --cqr
  run runc eval_deep_pi --table "$t"
done
run runc improve_conformal --mlflow
run runc run_crps_baseline
run runc run_champion_crps

stage C6 "significance (Friedman-Nemenyi + MCS + DM) + champion-challenger"
run runc significance_tables
run runc run_champion_challenger --mlflow

stage D1 "deploy tail: per-horizon PI scales + web vintage + cone + shadow"
run runc derive_band80_ratio
run runc generate_web_forecasts
run runc apply_cone_constraints
run runc score_forecasts
run runc freeze_shadow

stage D2 "single source of truth: key_facts + fe_facts + model card + drift"
run runc build_key_facts
run runc build_fe_facts
run runc build_model_card
run runc check_drift

stage D3 "result figures + tex tables"
run runc make_result_figures
run runc make_hero_figures
run runc make_tex_tables

stage D4 "MLflow sync (historical archive) "
run runc sync_mlflow

stage D5 "consistency guard (FAIL here = new figures to propagate, regla #0)"
runc check_consistency || echo "##### CONSISTENCY BROKEN (expected): propagate new figures to .tex/paper/web"

echo ""
echo "=== AQ CAMPAIGN ends $(date) ==="
echo "failed stages: $FAILS"
