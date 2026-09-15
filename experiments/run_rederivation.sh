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
#   3. save_finalists.sh      modelos finalistas (deep+locales) + transporte de ETS/Theta
#   3.5 persist_forecasts     ★ holdout_forecasts_{FAD,DFF} RECONSTRUIDOS y acreditados
#   4. combinadores           ensembles / conformal / stacking / FFORMA, sobre los holdouts
#                             de 3.5 — que ahora se re-acreditan al leerse
#   5. baselines prob./clásicos  Auto-ARIMA (AICc) · deep-PI · CRPS
#   6. tuning GBMs (AK)       OBLIGATORIO desde M74-E-R4/R5: run_tuning (Optuna persistente,
#                             150 trials, familia+empleo,
#                             candidatos) + confirm_tuning (aceptación en val-confirm
#                             INDEPENDIENTE; hold-out solo como reporte) + rank-check (AK9)
#   7. significancia          Friedman-Nemenyi + MCS + DM · champion-challenger
#   8. fuente de verdad       key_facts.json/.tex + model card + drift
#   9. figuras de resultados  results_* + hero (las EDA no cambian: el panel es el mismo)
#   8b. horizonte             build_horizon_facts (estaba PRE-F1 y fuera de la transacción)
#   8c. cohortes E1–E5        build_cohorts · scan_cohorts · los 42 lanes de E3 · score_e3 ·
#                             run_e4_router · build_e5_facts (decisión #58 opción A: la campaña
#                             reescribe los scorecards que E2 sella, así que la cadena se re-deriva
#                             DENTRO de la misma transacción, no en un corte aparte)
#  10. verificación           guardián de consistencia (si FALLA => hay cifras que propagar a
#                             .tex/paper/web; NO es terminal: la transacción queda en `computed`
#                             con la consistencia PENDIENTE y `txn validate` la vuelve a exigir)
#
# ⚠️ Duración: ≥16 h, no las «8-11 h» que este encabezado anunciaba desde julio. La estimación
# vieja no incluía la reconstrucción completa de `holdout_forecasts_{FAD,DFF}` (etapa 3.5: 2 tablas
# × 25 series × 9 modelos de walk-forward con reajuste en cada paso) ni el transporte de ETS/Theta,
# y se tomó cuando la etapa 1 aún calculaba ensembles que ahora se retiraron. Es una cota inferior.
#
# Uso (desde la raíz; ≥16 h; caffeinate evita que macOS duerma a mitad de campaña):
#   caffeinate -is bash experiments/run_rederivation.sh > reports/rederivation_$(date +%Y%m%dT%H%M%S).log 2>&1
# ★ R14 · en un worktree cuyo `ante/` no trae `dvc` (perfil `model-cpu`), exportar antes
#   `VP_DVC=/ruta/absoluta/al/dvc/gobernado` (3.67.1): el runbook lo exige, lo sella en el preflight
#   y `sync_all.sh` lo usa para re-hashear `models.dvc`/`mlflow.db.dvc`.
#
# ⚠️ M74-E-R5 (B7): el log lleva marca de tiempo. Redirigir a `reports/rederivation.log` a secas
# hacía que un relanzamiento SOBRESCRIBIERA la bitácora de la corrida anterior — y la bitácora es
# la única evidencia de una campaña que no terminó. El propio runbook deja además un enlace por
# `campaign_id` (ver más abajo), que es el nombre por el que se busca después.
#
# Fail-closed (auditoría 12-jul-2026): las etapas OBLIGATORIAS (run_req) hacen que el runbook
# TERMINE EN ROJO (exit≠0) si fallan: build_database · LINAJE de procedencia (M74-E-R5) · campaña ·
# proyección de pools · finalistas · holdout_forecasts · ensembles · conformal · Auto-ARIMA · CRPS ·
# BÚSQUEDA DE TUNING y confirm_tuning · significancia · champion-challenger · key_facts ·
# model_card · figuras de resultados. Quedan best-effort (run) solo las genuinamente tolerables:
# stacking/FFORMA exploratorios, deep-PI diagnóstico, drift y hero.
# ⚠️ M74-E-R5: esta enumeración decía «búsqueda de tuning» entre las best-effort cuando R4 ya la
# había vuelto obligatoria. Una documentación que contradice la conducta es peor que ninguna:
# quien lee el encabezado deja de mirar el código. La consistencia rota al final
# es exit 2 (hay cifras que propagar). NADA se publica: run_campaign y save_finalists llaman
# a sync_all con SYNC_PUBLISH=0; publicar exige `sync_all.sh --publish` humano tras validar.
set -uo pipefail
cd "$(dirname "$0")/.."
ANTE=ante/bin/python
NF=ante_nf/bin/python
[ -x "$ANTE" ] && [ -x "$NF" ] || { echo "ERROR: faltan venvs ante/ y/o ante_nf/ en la raíz" >&2; exit 1; }

# ★ M74-E · que los venvs EXISTAN no es que sean los CORRECTOS: el entorno tiene que reproducir su lock, medido y no supuesto.
# El preflight sellaba `locks/model-cpu.txt` y `locks/deep-macos-arm64.txt` y comprobaba que los
# directorios `ante/` y `ante_nf/` EXISTIERAN: sellaba la declaración del entorno, nunca el
# entorno. Medido el 13-sep-2026, los dos intérpretes corrían `torch` 2.12.0 contra un lock que
# sella 2.13.0, y con ellos `numba`/`llvmlite` —que compilan al vuelo el núcleo de statsforecast—
# y `coreforecast`. Once horas de cálculo habrían producido cifras que `locks/` no reconstruye,
# presentadas por el recibo como selladas.
# ⚠️ Sin bypass, y a propósito: un `SKIP_ENV_CHECK=1` aquí sería el mismo agujero que M74-B-R1
# tuvo que arrancar de raíz. La salida es reconstruir el entorno desde su lock.
if ! "$ANTE" -m tools.check_env_matches_lock; then
  echo "ERROR: el entorno no reproduce locks/. La campaña se detiene ANTES de calcular nada." >&2
  exit 8
fi
# ★ M74-E-R6 · el smoke completo es PUERTA, no recomendación (decisión del autor tras la auditoría
# `8bc41ff6…`). Tarda ~2 min y detecta en ese tiempo la clase de defecto que costó once horas.
if ! "$ANTE" tools/check_entrypoint_smoke.py; then
  echo "ERROR: el smoke de entrypoints falló. La campaña se detiene ANTES del primer artefacto." >&2
  exit 9
fi
# ★ M74-E-R8 · la PROCEDENCIA de la matriz y el SELLO de entradas son puertas, no pasos manuales
# (auditoría `e6c76896…`). Un preflight doble verde convivía con un checkout cuyo contrato de locks
# fallaba, y el sello que la campaña debía consumir era un archivo que nadie le pasaba. ★ M74-E-R9: el
# sello se emite DESPUÉS de fijar la identidad (árbol limpio y CAMPAIGN_SHA) y se acredita al registrarlo.
if ! "$ANTE" -m tools.lock_contracts; then
  echo "ERROR: el contrato de procedencia de locks falla. La campaña se detiene ANTES del primer artefacto." >&2
  exit 10
fi
# ★ R14 · el DVC gobernado es PUERTA y se sella: `sync_all.sh` re-hashea los punteros con él al final
# de las etapas 1 y 3, y hasta aquí era un `dvc` a secas del PATH del operador que ninguna puerta
# miraba (en el worktree de ejecución resolvía a un Homebrew 3.66.1; con PATH mínimo, a nada, tras
# diez horas de etapa deep). Misma regla de resolución que `tools/check_dvc_lock_fresh.py`.
export VP_DVC="${VP_DVC:-$PWD/ante/bin/dvc}"
if ! "$ANTE" -m tools.check_dvc_lock_fresh; then
  echo "ERROR: sin DVC gobernado ejecutable (\$VP_DVC=$VP_DVC) o dvc.lock desfasado. Aborta." >&2
  exit 15
fi


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
# ★ Enmienda §8 (2026-09-12): F2 corre ÍNTEGRAMENTE en CPU. `_accelerator()` prefiere MPS si la
# máquina lo tiene, y en la corrida del 11/12-sep eso repartió la etapa [1] entre 130 226 líneas en
# CPU y 846 en MPS. El protocolo no declaraba acelerador; ahora sí, y se declara aquí.
export VP_DEEP_ACCEL=cpu
# ★ M74-E-R5 (B7) · resolución de imports IDÉNTICA en runbook y smoke. Hoy funciona porque el
# editable apunta a este worktree y los guiones deep insertan ROOT, pero eso son dos mecanismos
# distintos para la misma garantía; `PYTHONPATH` la hace explícita y la comparte con el smoke.
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
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
PREFLIGHT_TMP="$(mktemp "${TMPDIR:-/tmp}/vp_preflight.XXXXXX")" || exit 12
if ! "$ANTE" -m tools.campaign_preflight --out "$PREFLIGHT_TMP"; then
  rm -f "$PREFLIGHT_TMP"
  echo "ERROR: el preflight de la campaña falló. La campaña se detiene ANTES del primer artefacto." >&2
  exit 11
fi
mkdir -p reports/campaign reports/logs
# ★ M74-E-R9 · promover y hashear son pasos que pueden fallar, y sin `set -e` nada los detenía.
PREFLIGHT="reports/logs/preflight_${CAMPAIGN_ID}.json"
if ! mv "$PREFLIGHT_TMP" "$PREFLIGHT" || ! PREFLIGHT_SHA256="$(shasum -a 256 "$PREFLIGHT" | cut -d' ' -f1)" \
   || ! [[ "$PREFLIGHT_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "ERROR: no se pudo promover o hashear el sello de entradas ($PREFLIGHT). Aborta." >&2
  exit 12
fi
# ★ M74-E-R5 (B7) · una bitácora POR CAMPAÑA, y la campaña es su dueña.
# Redirigir a `reports/rederivation.log` hacía que un relanzamiento SOBRESCRIBIERA la bitácora de
# la corrida anterior, que es la única evidencia de una campaña que no terminó. Un enlace a un
# nombre adivinado habría quedado colgando: el runbook no sabe adónde redirige el operador. Así
# que escribe la suya con `tee`, sin quitarle al operador la que él eligió.
exec > >(tee -a "reports/logs/${CAMPAIGN_ID}.log") 2>&1
echo "bitácora de esta campaña: reports/logs/${CAMPAIGN_ID}.log"
echo "sello de entradas de esta campaña: $PREFLIGHT (sha256 $PREFLIGHT_SHA256)"
printf '{"campaign_id":"%s","sha":"%s","git_sha":"%s","dirty":%s,"started_at":"%s","preflight":"%s","preflight_sha256":"%s"}\n' \
  "$CAMPAIGN_ID" "$CAMPAIGN_SHA" "$CAMPAIGN_SHA" "$CAMPAIGN_DIRTY" "$(date -u +%FT%TZ)" "$PREFLIGHT" "$PREFLIGHT_SHA256" \
  > reports/campaign/campaign_manifest.json
# ★ M74-E-R9 · el hash anotado no es una puerta hasta que alguien lo comprueba: se acredita AQUÍ, con la
# misma función que usan el gate de completitud y la publicación (sello vacío, `{}`, otro HEAD u otro dirty).
if ! "$ANTE" -m tools.campaign_manifest --assert-sealed reports/campaign/campaign_manifest.json; then
  echo "ERROR: el sello de entradas no se acredita contra el manifiesto de esta campaña. Aborta." >&2
  exit 13
fi
# ★ M74-E-R11 · la forma no es el contenido: rutas inexistentes con SHA válido o una semilla cambiada pasan
# cualquier esquema. El sello tiene que ser EXACTAMENTE lo que el preflight deriva HOY de este árbol —
# entrypoints del runbook, rutas y hashes recalculados en disco, reconcile_protocol() y auditar()—, antes
# de abrir la transacción. `reports/logs/` está ignorado para que la bitácora no cambie el `dirty` medido.
PREFLIGHT_VIVO="$(mktemp "${TMPDIR:-/tmp}/vp_preflight_vivo.XXXXXX")" || exit 14
if ! "$ANTE" -m tools.campaign_preflight --out "$PREFLIGHT_VIVO" || ! cmp -s "$PREFLIGHT_VIVO" "$PREFLIGHT"; then
  rm -f "$PREFLIGHT_VIVO"
  echo "ERROR: el sello registrado no es el que este árbol deriva hoy (entrypoints, entradas, protocolo o entorno). Aborta." >&2
  exit 14
fi
rm -f "$PREFLIGHT_VIVO"

# ── Transacción de campaña (ADR 0003, pendiente #56) ─────────────────────────
# Hasta M73 la máquina de estados existía y NADIE la conducía. Ahora este runbook la
# conduce por `tools/campaign_txn.py`: sella al arrancar, marca `failed` ante CUALQUIER
# salida anormal (incluido un Ctrl-C o un SIGTERM) y sólo llega a `computed` si las tres
# puertas pasaron. Publicar exige después una validación humana explícita.
CAMPAIGN_TXN="${CAMPAIGN_TXN:-reports/campaign/campaign.json}"
# H29: el archivo de transacciones y el panel son rutas REALES del repo. El ensayo hermético de
# la suite ejecuta este mismo bloque, así que necesita poder apuntarlas a un temporal; por
# defecto no cambian nada. Sin esto, una prueba que un día escriba tocaría `reports/`.
CAMPAIGN_TXN_ARCHIVE="${CAMPAIGN_TXN_ARCHIVE:-reports/campaign/transactions}"
CAMPAIGN_TXN_PANEL="${CAMPAIGN_TXN_PANEL:-data/processed/visa_panel_long.csv}"
txn() { $ANTE -m tools.campaign_txn --path "$CAMPAIGN_TXN" "$@"; }
# Una campaña anterior YA TERMINADA se archiva con su id; una abierta ABORTA aquí, que es
# justo lo que la máquina existe para impedir.
txn archive --dir "$CAMPAIGN_TXN_ARCHIVE" || exit 7
# ★ M74-E-R12 · el sello comparado en vivo viaja DENTRO de la transacción, inmutable: sin esto, la publicación
# no podía demostrar después qué sello se validó al arrancar (un manifiesto bien formado de la misma campaña pasaba).
txn open --campaign-id "$CAMPAIGN_ID" --sha "$CAMPAIGN_SHA" --dirty "$CAMPAIGN_DIRTY" \
    --panel "$CAMPAIGN_TXN_PANEL" --input-seal "$PREFLIGHT_SHA256" || exit 7
# El trap cubre TODAS las salidas: los `exit 1/3/4/5/6` de más abajo, una excepción del
# intérprete y las señales. `--if-open` lo hace idempotente: si el estado ya es terminal no
# toca nada, para que un fallo registrado no quede tapado por el error de una transición ilegal.
campaign_abort() {
  local rc=$?
  [ "$rc" -eq 0 ] && return 0
  txn fail --if-open --stage "salida anormal del runbook" --exit-code "$rc" \
      --reason "el runbook terminó con exit $rc sin alcanzar un estado terminal" >&2 || true
  # H14: matar a TODOS los descendientes. Sin esto, al colgarse la terminal el padre muere y los
  # hijos (python de ≥16 h) siguen escribiendo artefactos sobre una campaña ya marcada como fallida.
  # ★ R14 · era `kill -- -$$`, que sólo funciona si bash es líder de su grupo (setsid/nohup); bajo
  # `caffeinate -is bash …` desde una terminal con control de trabajos el líder es caffeinate y el
  # kill fallaba en silencio. Se recorre el árbol de procesos, que no depende de cómo se lanzó.
  kill_descendants "$$"
}
kill_descendants() {
  local hijo
  for hijo in $(pgrep -P "$1" 2>/dev/null); do
    kill_descendants "$hijo"
    kill "$hijo" 2>/dev/null || true
  done
}
trap campaign_abort EXIT
# ★ M74-E punto 10: una detención AUTORIZADA se registra como tal, con su motivo. Hasta ahora un
# Ctrl-C del operador y una muerte por presión de memoria quedaban idénticos en el estado:
# «salida anormal del runbook», exit 130/143. Dos corridas se detuvieron así y el estado no sabía
# decir cuál fue decisión de alguien.
campaign_stop() {
  local senal="$1" rc="$2"
  echo "##### DETENCIÓN AUTORIZADA ($senal): la campaña se cierra a petición, no por fallo." >&2
  txn fail --if-open --stage "detención autorizada ($senal)" --exit-code "$rc" \
      --reason "la corrida se detuvo deliberadamente con $senal; no es un fallo de cómputo" >&2 || true
  exit "$rc"
}
trap 'campaign_stop SIGINT 130' INT
trap 'campaign_stop SIGTERM 143' TERM
# H14: cerrar la terminal de `caffeinate -is bash …` mandaba SIGHUP, que no estaba atrapado:
# la transacción quedaba en `running` para siempre y el hijo, huérfano. Lánzalo con
# `nohup`/`setsid` si vas a desconectarte.
trap 'exit 129' HUP

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
# H16: además de contarlas, se RECUERDAN. Diez etapas tolerables podían fallar sin dejar rastro
# en la transacción, y la revisión humana tenía que descubrirlo leyendo ≥16 h de bitácora.
BEST_EFFORT_FAILED=()
run()   { "$@" || { echo "##### ETAPA FALLIDA (exit $?): $*"; FAILS=$((FAILS+1)); BEST_EFFORT_FAILED+=("$*"); }; }
# run_req(): OBLIGATORIA — su fallo DETIENE la campaña AQUÍ MISMO (M74-E, punto 6).
#
# ⚠️ Hasta M74-E acumulaba y seguía «para diagnóstico», con un corte en la etapa 7. Medido en la
# corrida `rederiv_1022c9d_20260911T212150`: la etapa [3] (finalistas) falló y las etapas [4] y [5]
# corrieron IGUAL, consumiendo `holdout_forecasts_*.csv` del 26-ago que [3] debía haber refrescado.
# Produjeron MASE de aspecto normal (0.1413, 0.1003…) sobre entradas de otra añada, anunciándose en
# el log como «holdouts frescos». Eso no es diagnóstico: es fabricar cifras contaminadas que en el
# log no se distinguen de las buenas.
#
# El diagnóstico se hace ANTES, en segundos, con `tools/check_entrypoint_smoke.py`.
run_req() {
  "$@" && return 0
  local rc=$?
  echo "##### ETAPA OBLIGATORIA FALLIDA (exit $rc): $*" >&2
  echo "##### FAIL-FAST: se detiene aquí para que NINGÚN consumidor lea artefactos a medias." >&2
  FAILS=$((FAILS+1)); REQ_FAILS=$((REQ_FAILS+1))
  txn fail --if-open --stage "$*" --exit-code "$rc" \
      --reason "etapa obligatoria fallida; detenida antes de ejecutar sus consumidores" >&2 || true
  exit "$rc"
}

echo "=== RE-DERIVACIÓN arranca $(date) ==="
run $ANTE -c "import pandas as pd; p=pd.read_csv('data/processed/visa_panel_long.csv'); \
print(f'panel: {len(p):,} filas · {p.bulletin_date.nunique()} meses · F={int((p.status==\"F\").sum()):,}')"

stage 0 "almacén fresco (el modelado lee DuckDB y aborta si está desfasado)"
run_req $ANTE -m pipeline.build_database
# ★ M74-E-R5 (B4) · el almacen puede construirse DEGRADADO y salir 0. Sin esta puerta, una campana
# podia arrancar con `source_artifact` vacia —medido en el worktree de ejecucion: 0 filas— y
# terminar en verde sin una sola fila de linaje. Un directorio de snapshots vacio se sella igual
# de bien que uno lleno, asi que el sello no basta: hay que comprobar la COBERTURA.
run_req $ANTE tools/check_source_lineage.py

stage 1 "campaña F1+F2 (pools 21 modelos + deep global multi-semilla)"
run_req bash experiments/run_campaign.sh

stage 2 "proyección pools -> model_comparison_*21.csv (consumidores: ensemble/tuning/figuras)"
for t in FAD DFF; do
  run_req cp "reports/campaign/campaign_pool_${t}_family.csv" "reports/eval/model_comparison_${t}21.csv"
  run_req cp "reports/campaign/campaign_pool_${t}_employment.csv" "reports/eval/model_comparison_EB_${t}21.csv"
done

stage 3 "finalistas (modelos deep+locales) + transporte de ETS/Theta"
run_req bash experiments/save_finalists.sh

# ★ M74-E-R1 · el productor que faltaba, y la etiqueta que mentía.
# La etapa 3 ANUNCIABA «holdout_forecasts frescos» y la 4 «combinadores sobre holdouts frescos».
# Nadie los escribía: el único escritor es `vp_model.persist_forecasts` y este runbook no lo
# invocaba nunca. Medido contra el panel de hoy, el artefacto vivo (26-ago) tenía 472 claves
# ausentes y 448 no esperadas en FAD, y 754/346 en DFF — no sólo era de otra añada, es que su
# ventana de hold-out ya no existe. Sobre eso se calculaban ensembles, conformal, stacking,
# FFORMA, el campeón y las tablas de significancia, y el runbook terminaba en verde.
stage 3.5 "holdout_forecasts RECONSTRUIDOS y acreditados (insumo de TODOS los combinadores)"
run_req $ANTE -m vp_model.persist_forecasts

stage 4 "combinadores sobre los holdouts de 3.5 (re-acreditados al leerse)"
run_req $ANTE experiments/run_ensembles.py --mlflow
run_req $ANTE experiments/improve_conformal.py --mlflow
run $ANTE experiments/improve_stacking.py --mlflow
run $ANTE experiments/improve_fforma.py --mlflow

stage 5 "baselines: Auto-ARIMA (AICc) · deep-PI · CRPS"
run_req $ANTE experiments/auto_arima_baseline.py
for t in FAD DFF; do
  run $NF experiments/run_deep_pi.py --table "$t" --model BiTCN --max-steps 800
  run $ANTE experiments/eval_deep_pi.py --table "$t"
done
run_req $ANTE experiments/run_crps_baseline.py

stage 6 "tuning GBMs (Optuna persistente + confirmación en val-confirm independiente, AK)"
# ★ M74-E-R4 · las tres pasan de `run` (best-effort) a `run_req` (OBLIGATORIAS).
# Eran best-effort y `optuna` no estaba en el perfil `model`: en un entorno exacto al lock morían
# con ModuleNotFoundError y el runbook SEGUÍA, terminando en verde con el HPO de los GBM
# silenciosamente omitido y `confirm_tuning` confirmando el `tuned_params.json` que ya hubiera.
# Un paso que decide hiperparámetros no puede ser opcional: o corre, o la campaña se detiene.
run_req $ANTE -m vp_model.run_tuning --n-trials 150 --mlflow
run_req $ANTE -m vp_model.run_tuning --rank-check --mlflow
run_req $ANTE -m vp_model.run_tuning --select-by-deploy   # fix #20: re-elige por deploy-score antes de confirmar
run_req $ANTE -m vp_model.confirm_tuning --holdout-report --mlflow

stage 6.5 "GATE de INPUTS (pools/semillas/HPO/finalists frescos y con métricas finitas)"
# Candado 1: si ya fallo CUALQUIER etapa obligatoria (0-6), NO correr los consumidores
# (significancia/champion) sobre outputs parciales — aborta antes.
if [ "$REQ_FAILS" -gt 0 ]; then
  echo "✗ $REQ_FAILS etapa(s) obligatoria(s) fallaron antes de significancia. Aborta." >&2
  exit 5
fi
# Candado 2: gate de inputs (ABORTA, no run_req): significancia/champion/key_facts NO deben
# correr sobre inputs incompletos, stale, con NaN o con el conjunto de semillas equivocado.
if ! $ANTE -m tools.check_campaign_completeness --phase inputs; then
  echo "✗ GATE DE INPUTS FALLIDO: inputs incompletos/stale/invalidos. Aborta antes de significancia." >&2
  exit 4
fi

stage 7 "significancia (Friedman-Nemenyi + MCS + DM) y champion-challenger"
run_req $ANTE experiments/significance_tables.py
run_req $ANTE experiments/run_champion_challenger.py --mlflow

stage 8 "fuente única de verdad: key_facts + model card + drift"
run_req $ANTE experiments/build_key_facts.py
run_req $ANTE experiments/build_model_card.py
run $ANTE experiments/check_drift.py

stage 8b "campeón por horizonte (horizon_facts) — estaba PRE-F1 y fuera de la transacción"
# H5: `horizon_facts.json` tenía un único commit, de julio, sobre el panel de 27 611 filas y FE
# 1.1.0: exactamente el régimen que `retro_protocol: pre-F1` describe. El guardián sólo comparaba
# tex↔json, así que nadie miraba json↔panel. Ahora se re-deriva con la campaña.
run_req $ANTE experiments/build_horizon_facts.py

stage 8c "cadena de cohortes E1–E5 (decisión #58, opción A: dentro de la MISMA transacción)"
# ★ La etapa 2 reescribe `model_comparison_*21.csv`, que es lo que `cohort_scan.json` (E2) sella en
# su procedencia: sin re-derivar la cadena, E2→E4→E5 quedarían midiendo sobre scorecards que ya no
# existen, y el guardián no lo detecta. El resultado negativo publicado no es una constante a
# proteger: si cambian sus entradas, vuelve a someterse a prueba.
run_req $ANTE experiments/build_cohorts.py
run_req $ANTE experiments/scan_cohorts.py
run_req $ANTE experiments/run_e3_campaign.py     # 42 lanes derivados del deck congelado (~78 min)
run_req $ANTE experiments/score_e3_campaign.py
run_req $ANTE experiments/run_e4_router.py
run_req $ANTE experiments/build_e5_facts.py

stage 8d "las dos promesas del paper (efecto del cono · naïve estacional OOS en los mismos orígenes)"
# H8: `paper.tex` ata a «the causal re-derivation» dos análisis que NADIE producía. Retirar las
# frases era el camino corto; se producen. El efecto del cono sólo es medible sobre pares maduros
# —el marco pre-proyección no se persistía hasta M74-B— y el módulo lo DECLARA en vez de fabricarlo.
run_req $ANTE experiments/analyze_paper_promises.py

stage 8.5 "GATE de OUTPUTS (significancia/champion/key_facts frescos + identidad)"
run_req $ANTE -m tools.check_campaign_completeness --phase outputs

stage 9 "figuras de resultados (las EDA no cambian: mismo panel)"
run_req $ANTE experiments/make_result_figures.py
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
  txn fail --if-open --stage "etapas obligatorias" --exit-code 1 \
      --reason "$REQ_FAILS etapa(s) obligatoria(s) rota(s)" >&2
  echo "✗ CAMPAÑA FALLIDA: $REQ_FAILS etapa(s) obligatoria(s) rota(s). NO publicar." >&2
  exit 1
fi
# ★ H2: que las cifras cambien es el resultado ESPERADO de una re-derivación, no un fallo. Antes
# esto marcaba `failed`, que es TERMINAL, y dejaba la campaña sin salida salvo repetir ≥16 h.
# Ahora llega a `computed` con la consistencia PENDIENTE, y es `txn validate` quien la vuelve a
# exigir —re-ejecutando el guardián— después de propagar.
CONSISTENCY_STATE=$([ "$CONSISTENCY_OK" = 1 ] && echo passed || echo pending)
BE_ARGS=()
for etapa in ${BEST_EFFORT_FAILED[@]+"${BEST_EFFORT_FAILED[@]}"}; do
  BE_ARGS+=(--best-effort-failure "$etapa")
done
txn compute --input-gate passed --output-gate passed --consistency "$CONSISTENCY_STATE" \
    ${BE_ARGS[@]+"${BE_ARGS[@]}"} || exit 7
if [ "$CONSISTENCY_OK" = 0 ]; then
  echo "⚠ Campaña COMPLETA con cifras nuevas: propaga (regla #0) y valida. La transacción queda" >&2
  echo "  en 'computed' con consistency=pending; 'txn validate' re-ejecuta el guardián." >&2
  exit 2
fi
echo "✓ Campaña completa y consistente. Queda en 'computed': publicar exige validación humana"
echo "  explícita y después el publicador:"
echo "    # 1. escribe el recibo de revisión (esquema cerrado, ligado a ESTA campaña):"
echo "    #    {\"schema\":\"campaign-validation-receipt/2\", \"campaign_id\":\"$CAMPAIGN_ID\","
echo "    #     \"source_git_sha\":\"$CAMPAIGN_SHA\", \"panel_sha256\":\"<el del estado>\","
echo "    #     \"input_seal_sha256\":\"$PREFLIGHT_SHA256\","
echo "    #     \"reviewed_by\":\"<persona>\", \"decision\":\"aprobada\", \"reviewed_at\":\"<RFC3339>\"}"
echo "    $ANTE -m tools.campaign_txn status   # de ahí salen panel_sha256 e input_seal_sha256"
echo "    $ANTE -m tools.campaign_txn validate --receipt <recibo.json>"
echo "    bash experiments/sync_all.sh --publish"
exit 0
