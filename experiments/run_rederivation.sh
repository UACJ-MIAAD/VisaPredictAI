#!/bin/bash
# Re-derivación COMPLETA de las cifras de modelado sobre el panel vigente.
# Es el runbook del precedente B1 (2-jul-2026) convertido en orquestador: cuando el
# dataset cambia (p. ej. resurrección I1: 27,289→27,611 filas F 15,758→15,931), TODAS
# las cifras canónicas (MASE/MCS/DM/cobertura) deben re-derivarse y propagarse (regla #0).
#
# Compone los orquestadores/pasos EXISTENTES en orden de dependencias — no duplica lógica:
#   1. run_campaign.sh        F1 pool local 21 modelos + F2 deep global multi-semilla + agregación
#   2. proyección de pools    campaign_pool_* -> model_comparison_*21.csv (mismo cómputo;
#                             los consumen ensemble/confirm_tuning/figuras — un solo entrenamiento)
#   3. save_finalists.sh      modelos finalistas (deep+locales) + holdout_forecasts_* frescos
#   4. combinadores           ensembles / conformal / stacking / FFORMA (sobre holdouts frescos;
#                             la corrida de ensembles dentro de run_campaign usa holdouts previos
#                             y queda superseded por esta)
#   5. baselines prob./clásicos  Auto-ARIMA (AICc) · deep-PI · CRPS
#   6. tuning GBMs            run_tuning (candidatos) + confirm_tuning (aceptación en hold-out)
#   7. significancia          Friedman-Nemenyi + MCS + DM · champion-challenger
#   8. fuente de verdad       key_facts.json/.tex + model card + drift
#   9. figuras de resultados  results_* + hero (las EDA no cambian: el panel es el mismo)
#  10. verificación           guardián de consistencia (si FALLA => hay cifras que propagar a
#                             .tex/paper/web; eso es una decisión editorial, no un error del run)
#
# Uso (desde la raíz; ~8-11 h; caffeinate evita que macOS duerma a mitad de campaña):
#   caffeinate -is bash experiments/run_rederivation.sh > reports/rederivation.log 2>&1
#
# Cada etapa aisla su fallo (|| true) y lo deja marcado con "ETAPA FALLIDA" — el guard
# de venvs es fail-loud porque sin él un cwd/venv equivocado convierte todo en un no-op
# silencioso (lección E1).
set -uo pipefail
cd "$(dirname "$0")/.."
ANTE=ante/bin/python
NF=ante_nf/bin/python
[ -x "$ANTE" ] && [ -x "$NF" ] || { echo "ERROR: faltan venvs ante/ y/o ante_nf/ en la raíz" >&2; exit 1; }

stage() { echo ""; echo "##### [$1] $2 — $(date '+%F %T')"; }
run()   { "$@" || echo "##### ETAPA FALLIDA (exit $?): $*"; }

echo "=== RE-DERIVACIÓN arranca $(date) ==="
$ANTE -c "import pandas as pd; p=pd.read_csv('data/processed/visa_panel_long.csv'); \
print(f'panel: {len(p):,} filas · {p.bulletin_date.nunique()} meses · F={int((p.status==\"F\").sum()):,}')"

stage 0 "almacén fresco (el modelado lee DuckDB y aborta si está desfasado)"
run $ANTE -m pipeline.build_database

stage 1 "campaña F1+F2 (pools 21 modelos + deep global multi-semilla)"
run bash experiments/run_campaign.sh

stage 2 "proyección pools -> model_comparison_*21.csv (consumidores: ensemble/tuning/figuras)"
for t in FAD DFF; do
  run cp "reports/campaign/campaign_pool_${t}_family.csv" "reports/model_comparison_${t}21.csv"
  run cp "reports/campaign/campaign_pool_${t}_employment.csv" "reports/model_comparison_EB_${t}21.csv"
done

stage 3 "finalistas (modelos deep+locales) + holdout_forecasts frescos"
run bash experiments/save_finalists.sh

stage 4 "combinadores sobre holdouts frescos (supersede el ensembles de la campaña)"
run $ANTE experiments/run_ensembles.py --mlflow
run $ANTE experiments/improve_conformal.py --mlflow
run $ANTE experiments/improve_stacking.py --mlflow
run $ANTE experiments/improve_fforma.py --mlflow

stage 5 "baselines: Auto-ARIMA (AICc) · deep-PI · CRPS"
run $ANTE experiments/auto_arima_baseline.py
for t in FAD DFF; do
  run $NF experiments/run_deep_pi.py --table "$t" --model BiTCN --max-steps 800
  run $ANTE experiments/eval_deep_pi.py --table "$t"
done
run $ANTE experiments/run_crps_baseline.py

stage 6 "tuning GBMs (candidatos + aceptación anti-overtuning en hold-out)"
run $ANTE -m vp_model.run_tuning
run $ANTE -m vp_model.confirm_tuning

stage 7 "significancia (Friedman-Nemenyi + MCS + DM) y champion-challenger"
run $ANTE experiments/significance_tables.py
run $ANTE experiments/run_champion_challenger.py --mlflow

stage 8 "fuente única de verdad: key_facts + model card + drift"
run $ANTE experiments/build_key_facts.py
run $ANTE experiments/build_model_card.py
run $ANTE experiments/check_drift.py

stage 9 "figuras de resultados (las EDA no cambian: mismo panel)"
run $ANTE experiments/make_result_figures.py
run $ANTE experiments/make_hero_figures.py

stage 10 "guardián de consistencia (FALLA = hay cifras nuevas que propagar, regla #0)"
$ANTE tools/check_consistency.py || echo "##### CONSISTENCIA ROTA: las cifras cambiaron — propagar a .tex/paper/web antes de pushear"

echo ""
echo "=== RE-DERIVACIÓN termina $(date) ==="
grep -c "ETAPA FALLIDA" reports/rederivation.log 2>/dev/null | xargs -I{} echo "etapas fallidas: {}"
