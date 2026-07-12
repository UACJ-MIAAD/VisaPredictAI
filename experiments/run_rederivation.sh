#!/bin/bash
# Re-derivación COMPLETA de las cifras de modelado sobre el panel vigente.
# Es el runbook del precedente B1 (2-jul-2026) convertido en orquestador: cuando el
# dataset cambia (p. ej. resurrección I1: 27,289→27,611 filas F 15,758→15,931), TODAS
# las cifras canónicas (MASE/MCS/DM/cobertura) deben re-derivarse y propagarse (regla #0).
#
# Compone los orquestadores/pasos EXISTENTES en orden de dependencias — no duplica lógica:
#   1. run_campaign.sh        F1 pool local 23 modelos + F2 deep global (búsqueda HPO 1x +
#                             re-entrenos multi-semilla del ganador, AK8) + agregación
#   2. proyección de pools    campaign_pool_* -> model_comparison_*21.csv (mismo cómputo;
#                             los consumen ensemble/figuras — un solo entrenamiento; el "21"
#                             del nombre es histórico, el pool ya trae 23 modelos)
#   3. save_finalists.sh      modelos finalistas (deep+locales) + holdout_forecasts_* frescos
#   4. combinadores           ensembles / conformal / stacking / FFORMA (sobre holdouts frescos;
#                             la corrida de ensembles dentro de run_campaign usa holdouts previos
#                             y queda superseded por esta)
#   5. baselines prob./clásicos  Auto-ARIMA (AICc) · deep-PI · CRPS
#   6. tuning GBMs (AK)       run_tuning (Optuna persistente, 150 trials, familia+empleo,
#                             candidatos) + confirm_tuning (aceptación en val-confirm
#                             INDEPENDIENTE; hold-out solo como reporte) + rank-check (AK9)
#   7. significancia          Friedman-Nemenyi + MCS + DM · champion-challenger
#   8. fuente de verdad       key_facts.json/.tex + model card + drift
#   9. figuras de resultados  results_* + hero (las EDA no cambian: el panel es el mismo)
#  10. verificación           guardián de consistencia (si FALLA => hay cifras que propagar a
#                             .tex/paper/web; eso es una decisión editorial, no un error del run)
#
# Uso (desde la raíz; ~8-11 h; caffeinate evita que macOS duerma a mitad de campaña):
#   caffeinate -is bash experiments/run_rederivation.sh > reports/rederivation.log 2>&1
#
# Fail-closed (auditoría 12-jul-2026): las etapas OBLIGATORIAS (run_req: build_database,
# campaña, significancia, champion-challenger, key_facts, model_card) hacen que el runbook
# TERMINE EN ROJO (exit≠0) si fallan; las best-effort (run: modelos que fallan en series
# cortas) solo se cuentan. La consistencia rota al final también es exit≠0 (hay cifras que
# propagar antes de publicar). NADA se publica automáticamente: run_campaign llama a sync_all
# en modo LOCAL (sin push); publicar exige `sync_all.sh --publish` humano tras validar.
set -uo pipefail
cd "$(dirname "$0")/.."
ANTE=ante/bin/python
NF=ante_nf/bin/python
[ -x "$ANTE" ] && [ -x "$NF" ] || { echo "ERROR: faltan venvs ante/ y/o ante_nf/ en la raíz" >&2; exit 1; }

# ── Identidad fija + árbol limpio (auditoría 12-jul-2026) ────────────────────
# La campaña DEBE arrancar sobre un árbol limpio y sella UN solo SHA + campaign_id
# para toda la corrida. Si el árbol está sucio, se aborta: commitear código a mitad
# de campaña marca los outputs con SHAs distintos (el bug de "identidades mezcladas").
# ⚠️ NO commitear NADA en este repo mientras la campaña corre.
if [ -z "${ALLOW_DIRTY:-}" ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "ERROR: el árbol tiene cambios sin commitear. La campaña exige árbol limpio para" >&2
  echo "       sellar una identidad única (SHA). Commitea/revierte, o usa ALLOW_DIRTY=1" >&2
  echo "       explícitamente si sabes lo que haces. Aborta." >&2
  git status --short >&2
  exit 1
fi
CAMPAIGN_SHA="$(git rev-parse HEAD)"
CAMPAIGN_ID="rederiv_$(git rev-parse --short HEAD)_$(date +%Y%m%dT%H%M%S)"
export CAMPAIGN_SHA CAMPAIGN_ID
echo "campaign_id=$CAMPAIGN_ID  ·  sha=$CAMPAIGN_SHA  ·  dirty=${ALLOW_DIRTY:+SI (forzado)}"
mkdir -p reports/campaign
printf '{"campaign_id":"%s","sha":"%s","started_at":"%s"}\n' \
  "$CAMPAIGN_ID" "$CAMPAIGN_SHA" "$(date -u +%FT%TZ)" > reports/campaign/campaign_manifest.json

FAILS=0
REQ_FAILS=0
stage() { echo ""; echo "##### [$1] $2 — $(date '+%F %T')"; }
# run(): best-effort — un fallo se cuenta pero la corrida sigue (para modelos que
# fallan legítimamente en series cortas dentro de un pool).
run()   { "$@" || { echo "##### ETAPA FALLIDA (exit $?): $*"; FAILS=$((FAILS+1)); }; }
# run_req(): OBLIGATORIA — su fallo hace que la campaña termine en rojo (aunque
# el resto siga para diagnóstico). Sin esto, un build_database/significance/key_facts
# roto pasaba desapercibido y el runbook "terminaba en verde".
run_req() { "$@" || { echo "##### ETAPA OBLIGATORIA FALLIDA (exit $?): $*"; FAILS=$((FAILS+1)); REQ_FAILS=$((REQ_FAILS+1)); }; }

echo "=== RE-DERIVACIÓN arranca $(date) ==="
run $ANTE -c "import pandas as pd; p=pd.read_csv('data/processed/visa_panel_long.csv'); \
print(f'panel: {len(p):,} filas · {p.bulletin_date.nunique()} meses · F={int((p.status==\"F\").sum()):,}')"

stage 0 "almacén fresco (el modelado lee DuckDB y aborta si está desfasado)"
run_req $ANTE -m pipeline.build_database

stage 1 "campaña F1+F2 (pools 21 modelos + deep global multi-semilla)"
run_req bash experiments/run_campaign.sh

stage 2 "proyección pools -> model_comparison_*21.csv (consumidores: ensemble/tuning/figuras)"
for t in FAD DFF; do
  run cp "reports/campaign/campaign_pool_${t}_family.csv" "reports/eval/model_comparison_${t}21.csv"
  run cp "reports/campaign/campaign_pool_${t}_employment.csv" "reports/eval/model_comparison_EB_${t}21.csv"
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

stage 6 "tuning GBMs (Optuna persistente + confirmación en val-confirm independiente, AK)"
run $ANTE -m vp_model.run_tuning --n-trials 150 --mlflow
run $ANTE -m vp_model.run_tuning --rank-check --mlflow
run $ANTE -m vp_model.run_tuning --select-by-deploy   # fix #20: re-elige por deploy-score antes de confirmar
run $ANTE -m vp_model.confirm_tuning --holdout-report --mlflow

stage 7 "significancia (Friedman-Nemenyi + MCS + DM) y champion-challenger"
run_req $ANTE experiments/significance_tables.py
run_req $ANTE experiments/run_champion_challenger.py --mlflow

stage 8 "fuente única de verdad: key_facts + model card + drift"
run_req $ANTE experiments/build_key_facts.py
run_req $ANTE experiments/build_model_card.py
run $ANTE experiments/check_drift.py

stage 9 "figuras de resultados (las EDA no cambian: mismo panel)"
run $ANTE experiments/make_result_figures.py
run $ANTE experiments/make_hero_figures.py

stage 10 "guardián de consistencia (FALLA = hay cifras nuevas que propagar, regla #0)"
CONSISTENCY_OK=1
$ANTE tools/check_consistency.py || { CONSISTENCY_OK=0; echo "##### CONSISTENCIA ROTA: las cifras cambiaron — propagar a .tex/paper/web ANTES de publicar"; }

echo ""
echo "=== RE-DERIVACIÓN termina $(date) ==="
echo "campaign_id=$CAMPAIGN_ID  ·  sha=$CAMPAIGN_SHA"
echo "etapas fallidas: $FAILS (obligatorias: $REQ_FAILS) · consistencia: $([ $CONSISTENCY_OK = 1 ] && echo OK || echo ROTA)"

# Fail-closed: rojo si falló cualquier etapa OBLIGATORIA. La consistencia rota NO es
# error del run (es la señal de que hay que propagar), pero se reporta en exit 2 para
# que un publicador automático jamás la confunda con verde.
if [ "$REQ_FAILS" -gt 0 ]; then
  echo "✗ CAMPAÑA FALLIDA: $REQ_FAILS etapa(s) obligatoria(s) rota(s). NO publicar." >&2
  exit 1
fi
if [ "$CONSISTENCY_OK" = 0 ]; then
  echo "⚠ Campaña OK pero cifras cambiaron: propagar (regla #0) y validar antes de publicar." >&2
  exit 2
fi
echo "✓ Campaña completa y consistente. Publicar es un paso humano: sync_all.sh --publish"
exit 0
