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
# Fail-closed (auditoría 12-jul-2026): las etapas OBLIGATORIAS (run_req) hacen que el runbook
# TERMINE EN ROJO (exit≠0) si fallan: build_database · campaña · proyección de pools ·
# finalistas · ensembles · conformal · Auto-ARIMA · CRPS · confirm_tuning · significancia ·
# champion-challenger · key_facts · model_card · figuras de resultados. Quedan best-effort
# (run) solo las genuinamente tolerables: stacking/FFORMA exploratorios, deep-PI diagnóstico,
# búsqueda de tuning (confirm sí es obligatoria), drift y hero. La consistencia rota al final
# es exit 2 (hay cifras que propagar). NADA se publica: run_campaign y save_finalists llaman
# a sync_all con SYNC_PUBLISH=0; publicar exige `sync_all.sh --publish` humano tras validar.
set -uo pipefail
cd "$(dirname "$0")/.."
# R9.4: bootstrap orquestador; la LÓGICA DE PRODUCTO corre en los entornos content-addressed
# (runtime/model/deep-cpu) que abre `run-command`, jamás en el python ambiental.
PYBOOT=${PYBOOT:-python3.14}
command -v "$PYBOOT" >/dev/null 2>&1 || { echo "ERROR: falta $PYBOOT (bootstrap del orquestador)" >&2; exit 1; }
runc() { "$PYBOOT" -m tools.python_env run-command --id "$1" -- "${@:2}"; }

# ── Identidad fija + árbol limpio (auditoría 12-jul-2026) ────────────────────
# La campaña DEBE arrancar sobre un árbol limpio y sella UN solo SHA + campaign_id
# para toda la corrida. Si el árbol está sucio, se aborta: commitear código a mitad
# de campaña marca los outputs con SHAs distintos (el bug de "identidades mezcladas").
# ⚠️ NO commitear NADA en este repo mientras la campaña corre.
# Sucio = tracked modificado O código untracked (.py/.sh/.sql/.yaml/.yml/.toml). Los
# outputs generados (reports/, data/, models/, *.log, staging) son untracked legítimos y
# NO cuentan; un .py suelto SÍ cambia el comportamiento y debe abortar.
tree_dirty() {
  git status --porcelain --untracked-files=no | grep -q . && { echo "tracked-modificado"; return; }
  git ls-files --others --exclude-standard | grep -qE '\.(py|sh|sql|ya?ml|toml)$' && { echo "codigo-untracked"; return; }
}
if [ -z "${ALLOW_DIRTY:-}" ] && [ -n "$(tree_dirty)" ]; then
  echo "ERROR: el árbol tiene cambios de código sin commitear ($(tree_dirty)). La campaña" >&2
  echo "       exige árbol limpio para sellar una identidad única (SHA). Commitea/revierte," >&2
  echo "       o usa ALLOW_DIRTY=1 explícitamente. Aborta." >&2
  git status --short >&2
  git ls-files --others --exclude-standard | grep -E '\.(py|sh|sql|ya?ml|toml)$' >&2
  exit 1
fi
CAMPAIGN_SHA="$(git rev-parse HEAD)"
CAMPAIGN_ID="rederiv_$(git rev-parse --short HEAD)_$(date +%Y%m%dT%H%M%S)"
CAMPAIGN_DIRTY="${ALLOW_DIRTY:+true}"; CAMPAIGN_DIRTY="${CAMPAIGN_DIRTY:-false}"
# ⚠️ CAMPAIGN_DIRTY debe EXPORTARSE (auditoría 13-jul ronda 8): sin esto, save_finalists_deep
# y los ledgers (tracking.py/config.py) leían "false" por defecto y estampaban git_dirty=false
# aunque la campaña fuese diagnóstica — la identidad mentía. Ahora todos los productores ven
# el mismo dirty sellado.
export CAMPAIGN_SHA CAMPAIGN_ID CAMPAIGN_DIRTY
export CAMPAIGN_GIT_SHA="$CAMPAIGN_SHA"
echo "campaign_id=$CAMPAIGN_ID  ·  sha=$CAMPAIGN_SHA  ·  dirty=$CAMPAIGN_DIRTY"
# ⚠️ Una campaña OFICIAL (para publicar) NO debe correr con ALLOW_DIRTY: los ledgers
# sellan dirty=False y quedaria una mentira. ALLOW_DIRTY solo para diagnostico/depuracion.
if [ -n "${ALLOW_DIRTY:-}" ] && [ -z "${CAMPAIGN_DIAGNOSTIC:-}" ]; then
  echo "ERROR: ALLOW_DIRTY=1 sin CAMPAIGN_DIAGNOSTIC=1. Una campaña oficial exige árbol" >&2
  echo "       limpio (los ledgers sellan dirty=False). Usa CAMPAIGN_DIAGNOSTIC=1 solo" >&2
  echo "       para una corrida de depuracion que NO se publicara. Aborta." >&2
  exit 6
fi
mkdir -p reports/campaign
printf '{"campaign_id":"%s","sha":"%s","git_sha":"%s","dirty":%s,"started_at":"%s"}\n' \
  "$CAMPAIGN_ID" "$CAMPAIGN_SHA" "$CAMPAIGN_SHA" "$CAMPAIGN_DIRTY" "$(date -u +%FT%TZ)" \
  > reports/campaign/campaign_manifest.json

FAILS=0
REQ_FAILS=0
# stage(): además de rotular, verifica que HEAD NO cambió desde el sellado — si alguien
# commitea a mitad de campaña, aborta (los outputs quedarían con SHAs mezclados).
stage() {
  local now; now="$(git rev-parse HEAD)"
  if [ "$now" != "$CAMPAIGN_SHA" ]; then
    echo "ERROR: HEAD cambió a mitad de campaña ($CAMPAIGN_SHA -> $now). Aborta para no" >&2
    echo "       mezclar identidades. Re-lanza desde árbol limpio." >&2
    exit 3
  fi
  echo ""; echo "##### [$1] $2 — $(date '+%F %T')"
}
# run(): best-effort — un fallo se cuenta pero la corrida sigue (para modelos que
# fallan legítimamente en series cortas dentro de un pool).
run()   { "$@" || { echo "##### ETAPA FALLIDA (exit $?): $*"; FAILS=$((FAILS+1)); }; }
# run_req(): OBLIGATORIA — su fallo hace que la campaña termine en rojo (aunque
# el resto siga para diagnóstico). Sin esto, un build_database/significance/key_facts
# roto pasaba desapercibido y el runbook "terminaba en verde".
run_req() { "$@" || { echo "##### ETAPA OBLIGATORIA FALLIDA (exit $?): $*"; FAILS=$((FAILS+1)); REQ_FAILS=$((REQ_FAILS+1)); }; }

echo "=== RE-DERIVACIÓN arranca $(date) ==="
run runc print_panel_summary   # R9.4/B66: extraído del `-c` (tools/print_panel_summary.py)

stage 0 "almacén fresco (el modelado lee DuckDB y aborta si está desfasado)"
run_req runc build_database

stage 1 "campaña F1+F2 (pools 21 modelos + deep global multi-semilla)"
run_req bash experiments/run_campaign.sh

stage 2 "proyección pools -> model_comparison_*21.csv (consumidores: ensemble/tuning/figuras)"
for t in FAD DFF; do
  run_req cp "reports/campaign/campaign_pool_${t}_family.csv" "reports/eval/model_comparison_${t}21.csv"
  run_req cp "reports/campaign/campaign_pool_${t}_employment.csv" "reports/eval/model_comparison_EB_${t}21.csv"
done

stage 3 "finalistas (modelos deep+locales) + holdout_forecasts frescos"
run_req bash experiments/save_finalists.sh

stage 4 "combinadores sobre holdouts frescos (supersede el ensembles de la campaña)"
run_req runc run_ensembles --mlflow
run_req runc improve_conformal --mlflow
run runc improve_stacking --mlflow
run runc improve_fforma --mlflow

stage 5 "baselines: Auto-ARIMA (AICc) · deep-PI · CRPS"
run_req runc auto_arima_baseline
for t in FAD DFF; do
  run runc run_deep_pi --table "$t" --model BiTCN --max-steps 800
  run runc eval_deep_pi --table "$t"
done
run_req runc run_crps_baseline

stage 6 "tuning GBMs (Optuna persistente + confirmación en val-confirm independiente, AK)"
run runc run_tuning --n-trials 150 --mlflow
run runc run_tuning --rank-check --mlflow
run runc run_tuning --select-by-deploy   # fix #20: re-elige por deploy-score antes de confirmar
run_req runc confirm_tuning --holdout-report --mlflow

stage 6.5 "GATE de INPUTS (pools/semillas/HPO/finalists frescos y con métricas finitas)"
# Candado 1: si ya fallo CUALQUIER etapa obligatoria (0-6), NO correr los consumidores
# (significancia/champion) sobre outputs parciales — aborta antes.
if [ "$REQ_FAILS" -gt 0 ]; then
  echo "✗ $REQ_FAILS etapa(s) obligatoria(s) fallaron antes de significancia. Aborta." >&2
  exit 5
fi
# Candado 2: gate de inputs (ABORTA, no run_req): significancia/champion/key_facts NO deben
# correr sobre inputs incompletos, stale, con NaN o con el conjunto de semillas equivocado.
if ! runc check_campaign_completeness --phase inputs; then
  echo "✗ GATE DE INPUTS FALLIDO: inputs incompletos/stale/invalidos. Aborta antes de significancia." >&2
  exit 4
fi

stage 7 "significancia (Friedman-Nemenyi + MCS + DM) y champion-challenger"
run_req runc significance_tables
run_req runc run_champion_challenger --mlflow

stage 8 "fuente única de verdad: key_facts + model card + drift"
run_req runc build_key_facts
run_req runc build_model_card
run runc check_drift

stage 8.5 "GATE de OUTPUTS (significancia/champion/key_facts frescos + identidad)"
run_req runc check_campaign_completeness --phase outputs

stage 9 "figuras de resultados (las EDA no cambian: mismo panel)"
run_req runc make_result_figures
run runc make_hero_figures

stage 10 "guardián de consistencia (FALLA = hay cifras nuevas que propagar, regla #0)"
CONSISTENCY_OK=1
runc check_consistency || { CONSISTENCY_OK=0; echo "##### CONSISTENCIA ROTA: las cifras cambiaron — propagar a .tex/paper/web ANTES de publicar"; }

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
