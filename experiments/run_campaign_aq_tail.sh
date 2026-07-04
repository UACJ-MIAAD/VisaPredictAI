#!/bin/bash
# AQ campaign REPAIR TAIL. Two live-caught failures forced partial re-derivation:
#   1. lane T crashed on zero-F structural series in the employment groups (fixed
#      in tune._group_series); tuning re-ran separately AFTER the orchestrator's
#      stage B had already produced GBM catalog rows with stale/partial winners.
#   2. the FAD Auto* HPO search ran with the kernel_size bug: AutoBiTCN trials all
#      failed; the winner JSON was repaired by a parallel search (camp_hposearchfix)
#      and the FAD camp_auto refits may have run without a BiTCN config.
# This tail re-derives everything downstream of those two inputs. Idempotent.
# Run AFTER run_campaign_aq.sh AND the tuning re-run both finish:
#   caffeinate -is bash experiments/run_campaign_aq_tail.sh > reports/campaign_aq_tail.log 2>&1
set -uo pipefail
cd "$(dirname "$0")/.."
ANTE=ante/bin/python
NF=ante_nf/bin/python
[ -x "$ANTE" ] && [ -x "$NF" ] || { echo "ERROR: missing venvs" >&2; exit 1; }
FAILS=0
stage() { echo ""; echo "##### [$1] $2 — $(date '+%F %T')"; }
run()   { "$@" || { echo "##### STAGE FAILED (exit $?): $*"; FAILS=$((FAILS+1)); }; }
GBM="xgboost lightgbm catboost"

echo "=== AQ TAIL starts $(date) ==="

stage T1 "FAD deep auto refits if the BiTCN winner config was late"
if [ -f reports/campaign/hpo_deep_best_FAD_AutoBiTCN.json ]; then
  if ! $ANTE - <<'PY'
import pandas as pd, pathlib, sys
f = pathlib.Path("reports/campaign/global_FAD_camp_auto_s1.csv")
ok = f.exists() and "BiTCN" in set(pd.read_csv(f)["model"].unique())
sys.exit(0 if ok else 1)
PY
  then
    echo "FAD camp_auto is missing BiTCN rows -> re-running the 5 refit seeds"
    for seed in 1 2 3 4 5; do
      run $NF experiments/run_global_deep.py --table FAD --block family --diff \
        --models BiTCN --config "reports/campaign/hpo_deep_best_FAD_Auto{model}.json" \
        --seed "$seed" --suffix "camp_auto_s${seed}"
    done
    run $ANTE experiments/aggregate_seeds.py --table FAD --prefix camp_auto_s --model AutoBiTCN --mlflow
  else
    echo "FAD camp_auto already has BiTCN rows — no repair needed"
  fi
else
  echo "##### STAGE FAILED: hpo_deep_best_FAD_AutoBiTCN.json still missing"; FAILS=$((FAILS+1))
fi

stage T2 "GBM catalog rows with CONFIRMED winners (tuning re-ran after stage B)"
for table in FAD DFF; do
  for block in family employment; do
    run $ANTE -m vp_model.run_comparison --country all --table "$table" --block "$block" --mlflow \
      --models $GBM --out "reports/campaign/aq_pool_gbm_${table}_${block}.csv"
  done
done

stage T3 "re-merge pool halves -> campaign_pool + model_comparison projections"
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

stage T4 "finalists + fresh holdout/selection forecasts"
run bash experiments/save_finalists.sh

stage T5 "combiners on fresh holdouts"
run $ANTE experiments/run_ensembles.py --mlflow
run $ANTE experiments/improve_stacking.py --mlflow
run $ANTE experiments/improve_fforma.py --mlflow
run $ANTE experiments/improve_conformal.py --mlflow

stage T6 "champion CRPS + significance + champion-challenger"
run $ANTE experiments/run_champion_crps.py
run $ANTE experiments/significance_tables.py
run $ANTE experiments/run_champion_challenger.py --mlflow

stage T7 "deploy tail: per-horizon PI + web vintage + cone + shadow + scoring"
run $ANTE experiments/derive_band80_ratio.py
run $ANTE experiments/generate_web_forecasts.py
run $ANTE experiments/apply_cone_constraints.py
run $ANTE experiments/score_forecasts.py
run $ANTE experiments/freeze_shadow.py

stage T8 "single source of truth: key_facts + fe_facts + model card + drift"
run $ANTE experiments/build_key_facts.py
run $ANTE experiments/build_fe_facts.py
run $ANTE experiments/build_model_card.py
run $ANTE experiments/check_drift.py

stage T9 "result figures + tex tables"
run $ANTE experiments/make_result_figures.py
run $ANTE experiments/make_hero_figures.py
run $ANTE experiments/make_tex_tables.py

stage T10 "MLflow sync"
run $NF experiments/sync_mlflow.py

stage T11 "consistency guard (FAIL = new figures to propagate, regla #0)"
$ANTE tools/check_consistency.py || echo "##### CONSISTENCY BROKEN (expected): propagate to .tex/paper/web"

echo ""
echo "=== AQ TAIL ends $(date) ==="
echo "failed stages: $FAILS"
