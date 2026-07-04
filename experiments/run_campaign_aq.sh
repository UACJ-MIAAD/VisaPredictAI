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
ANTE=ante/bin/python
NF=ante_nf/bin/python
[ -x "$ANTE" ] && [ -x "$NF" ] || { echo "ERROR: missing venvs ante/ and/or ante_nf/" >&2; exit 1; }
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
run $ANTE -c "import pandas as pd; p=pd.read_csv('data/processed/visa_panel_long.csv'); \
print(f'panel: {len(p):,} rows / {p.bulletin_date.nunique()} months / F={int((p.status==\"F\").sum()):,}')"

stage 0 "fresh warehouse (modeling reads DuckDB and aborts if stale)"
run $ANTE -m pipeline.build_database

stage A "parallel lanes: pool(non-GBM) | HPO | deep | new families"

(   # ---- lane P: local pool, non-GBM models, both tables x blocks ----
    for table in FAD DFF; do
      for block in family employment; do
        echo ">>> lane P: pool $table/$block $(date)"
        $ANTE -m vp_model.run_comparison --country all --table "$table" --block "$block" --mlflow \
          --models $NON_GBM --out "reports/campaign/aq_pool_nongbm_${table}_${block}.csv" || echo "LANE-P FAILED $table/$block"
      done
    done
    echo "lane P done $(date)"
) > "$LOGDIR/lane_pool.log" 2>&1 &
PID_P=$!

(   # ---- lane T: Optuna HPO -> independent confirmation -> rank check ----
    echo ">>> lane T: run_tuning $(date)"
    $ANTE -m vp_model.run_tuning --n-trials 150 --mlflow || echo "LANE-T FAILED run_tuning"
    $ANTE -m vp_model.confirm_tuning --holdout-report --mlflow || echo "LANE-T FAILED confirm"
    $ANTE -m vp_model.run_tuning --rank-check --mlflow || echo "LANE-T FAILED rank-check"
    echo "lane T done $(date)"
) > "$LOGDIR/lane_tuning.log" 2>&1 &
PID_T=$!

(   # ---- lane D: deep global (det variants x seeds, HPO search 1x, winner refits) ----
    for table in FAD DFF; do
      for seed in $SEEDS; do
        $NF experiments/run_global_deep.py --table "$table" --block family --max-steps 800 --models $DET_MODELS \
          --seed "$seed" --suffix "camp_levels_s${seed}" || echo "LANE-D FAILED levels $table s$seed"
        $NF experiments/run_global_deep.py --table "$table" --block family --diff --max-steps 800 --models $DET_MODELS \
          --seed "$seed" --suffix "camp_diff_s${seed}" || echo "LANE-D FAILED diff $table s$seed"
        $NF experiments/run_global_deep.py --table "$table" --block family --diff --local-scaler --max-steps 800 \
          --models $DET_MODELS --seed "$seed" --suffix "camp_diffls_s${seed}" || echo "LANE-D FAILED diffls $table s$seed"
      done
      $NF experiments/run_global_deep.py --table "$table" --block family --diff --auto --num-samples 40 \
        --models AutoBiTCN AutoTiDE AutoNHITS --seed 1 --suffix "camp_hposearch" || echo "LANE-D FAILED hposearch $table"
      for seed in $SEEDS; do
        $NF experiments/run_global_deep.py --table "$table" --block family --diff \
          --models BiTCN TiDE NHITS --config "reports/campaign/hpo_deep_best_${table}_Auto{model}.json" \
          --seed "$seed" --suffix "camp_auto_s${seed}" || echo "LANE-D FAILED auto $table s$seed"
      done
    done
    echo "lane D done $(date)"
) > "$LOGDIR/lane_deep.log" 2>&1 &
PID_D=$!

(   # ---- lane X: new families + classic tuned baseline ----
    echo ">>> lane X: statsforecast $(date)"
    $NF experiments/run_statsforecast.py --table both --refit 1 || echo "LANE-X FAILED statsforecast"
    echo ">>> lane X: auto-ARIMA $(date)"
    $ANTE experiments/auto_arima_baseline.py || echo "LANE-X FAILED auto_arima"
    echo ">>> lane X: global GBM $(date)"
    $ANTE experiments/run_global_gbm.py --table both --models lightgbm xgboost || echo "LANE-X FAILED global_gbm"
    echo ">>> lane X: hurdle $(date)"
    $ANTE experiments/run_hurdle.py --table both || echo "LANE-X FAILED hurdle"
    $ANTE experiments/run_hurdle.py --table both --threshold || echo "LANE-X FAILED hurdle-thr"
    echo "lane X done $(date)"
) > "$LOGDIR/lane_newfam.log" 2>&1 &
PID_X=$!

echo "lanes launched: P=$PID_P T=$PID_T D=$PID_D X=$PID_X"
wait $PID_T; echo "lane T (tuning) finished $(date)"

stage B "GBM catalog rows with same-campaign accepted winners (waits: lane T)"
for table in FAD DFF; do
  for block in family employment; do
    run $ANTE -m vp_model.run_comparison --country all --table "$table" --block "$block" --mlflow \
      --models $GBM --out "reports/campaign/aq_pool_gbm_${table}_${block}.csv"
  done
done

wait $PID_P; echo "lane P (pool) finished $(date)"
wait $PID_X; echo "lane X (new families) finished $(date)"

stage C1 "merge pool halves -> campaign_pool + model_comparison projections"
run $ANTE - <<'PY'
import pandas as pd, pathlib
camp = pathlib.Path("reports/campaign"); ev = pathlib.Path("reports/eval")
for table in ("FAD", "DFF"):
    for block in ("family", "employment"):
        parts = []
        for kind in ("nongbm", "gbm"):
            f = camp / f"aq_pool_{kind}_{table}_{block}.csv"
            if f.exists():
                parts.append(pd.read_csv(f))
            else:
                print(f"MISSING pool half: {f}")
        if not parts:
            continue
        full = pd.concat(parts, ignore_index=True)
        full.to_csv(camp / f"campaign_pool_{table}_{block}.csv", index=False)
        tgt = f"model_comparison_{table}21.csv" if block == "family" else f"model_comparison_EB_{table}21.csv"
        full.to_csv(ev / tgt, index=False)
        print(f"{table}/{block}: {len(full)} rows -> {tgt}")
PY

wait $PID_D; echo "lane D (deep) finished $(date)"

stage C2 "deep multi-seed aggregation -> MLflow"
for table in FAD DFF; do
  for m in $DET_MODELS; do
    for v in camp_levels_s camp_diff_s camp_diffls_s; do
      run $ANTE experiments/aggregate_seeds.py --table "$table" --prefix "$v" --model "$m" --mlflow
    done
  done
  for m in AutoBiTCN AutoTiDE AutoNHITS; do
    run $ANTE experiments/aggregate_seeds.py --table "$table" --prefix camp_auto_s --model "$m" --mlflow
  done
done

stage C3 "finalists + fresh holdout/selection forecasts"
run bash experiments/save_finalists.sh

stage C4 "combiners on fresh holdouts (best-K / stacking simplex / FFORMA / conformal)"
run $ANTE experiments/run_ensembles.py --mlflow
run $ANTE experiments/improve_stacking.py --mlflow
run $ANTE experiments/improve_fforma.py --mlflow

stage C5 "probabilistic: deep-PI (36 windows x 3 seeds + CQR) + CRPS"
for t in FAD DFF; do
  run $NF experiments/run_deep_pi.py --table "$t" --model BiTCN --max-steps 800
  run $NF experiments/run_deep_pi.py --table "$t" --model BiTCN --max-steps 800 --cqr
  run $ANTE experiments/eval_deep_pi.py --table "$t"
done
run $ANTE experiments/improve_conformal.py --mlflow
run $ANTE experiments/run_crps_baseline.py
run $ANTE experiments/run_champion_crps.py

stage C6 "significance (Friedman-Nemenyi + MCS + DM) + champion-challenger"
run $ANTE experiments/significance_tables.py
run $ANTE experiments/run_champion_challenger.py --mlflow

stage D1 "deploy tail: per-horizon PI scales + web vintage + cone + shadow"
run $ANTE experiments/derive_band80_ratio.py
run $ANTE experiments/generate_web_forecasts.py
run $ANTE experiments/apply_cone_constraints.py
run $ANTE experiments/score_forecasts.py
run $ANTE experiments/freeze_shadow.py

stage D2 "single source of truth: key_facts + fe_facts + model card + drift"
run $ANTE experiments/build_key_facts.py
run $ANTE experiments/build_fe_facts.py
run $ANTE experiments/build_model_card.py
run $ANTE experiments/check_drift.py

stage D3 "result figures + tex tables"
run $ANTE experiments/make_result_figures.py
run $ANTE experiments/make_hero_figures.py
run $ANTE experiments/make_tex_tables.py

stage D4 "MLflow sync (historical archive) "
run $NF experiments/sync_mlflow.py

stage D5 "consistency guard (FAIL here = new figures to propagate, regla #0)"
$ANTE tools/check_consistency.py || echo "##### CONSISTENCY BROKEN (expected): propagate new figures to .tex/paper/web"

echo ""
echo "=== AQ CAMPAIGN ends $(date) ==="
echo "failed stages: $FAILS"
