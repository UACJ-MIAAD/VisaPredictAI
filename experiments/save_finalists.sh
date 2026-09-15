#!/bin/bash
# Guarda TODOS los modelos finalistas (globales deep + locales por serie) para comparar,
# graficar y explotar; exporta sus pronósticos hold-out; y re-hashea localmente.
# Correr en background:  bash experiments/save_finalists.sh > reports/finalists.log 2>&1
#
# ★ FAIL-FAST INTERNO + AISLAMIENTO POR CAMPAÑA (M74-E, puntos 1 y 6).
#
# Hasta M74-E cada paso ACUMULABA su fallo y el script seguía. Medido en la corrida
# `rederiv_1022c9d_20260911T212150`: el paso [1/4] (globales deep) murió por un ImportError y los
# pasos [2/4], [3/4] y [4/4] corrieron IGUAL. Consecuencias reales, todas con aspecto de éxito:
#
#   · `models/` quedó como un árbol MIXTO: 301 archivos nuevos y CERO globales, con los globales
#     anteriores intactos y disponibles. ⚠️ Este comentario decía «del 26-ago»: al clasificar la
#     escena se MIDIERON las añadas reales y son **2026-06-18, 06-19 y 07-03** (46 archivos). La
#     fecha de agosto era el mtime de los DIRECTORIOS, que cambia con cualquier alta o baja de un
#     hijo directo y no dice nada del contenido;
#   · `export_forecasts.py` leyó esos globales viejos y los metió en el CSV junto a los locales
#     nuevos, mezclando dos añadas en un mismo artefacto;
#   · `sync_all LOCAL` re-hasheó y stageó ese árbol mixto en `models.dvc`;
#   · el fail-fast EXTERIOR de `run_rederivation.sh` no ayudaba: el daño ocurre DENTRO de la etapa.
#
# Dos guardas, porque una sola no basta:
#   1. `step` sale INMEDIATAMENTE. Ningún consumidor corre tras un productor roto.
#   2. Los productores escriben en `models/.staging/<campaign_id>/` y el árbol servido se promueve
#      SÓLO tras acreditar cobertura. Sin esto, un productor que falla deja los directorios
#      anteriores ahí, listos para que el siguiente paso los lea creyéndolos frescos: la frescura
#      NUNCA se infiere por existencia ni por mtime.
#
# FAIL-CLOSED (auditoría 12-jul-2026): sync_all se invoca con SYNC_PUBLISH=0 explícito (NO publica
# aunque el entorno herede SYNC_PUBLISH=1). El guard de venvs es obligatorio: sin él, un cwd/venv
# equivocado era un no-op "exitoso" (E1).
set -uo pipefail
cd "$(dirname "$0")/.."
# ★ M74-E-R5 (B7) · misma resolución de imports que el runbook y el smoke, explícita.
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
[ -x ante/bin/python ] && [ -x ante_nf/bin/python ] || { echo "ERROR: faltan venvs ante/ y/o ante_nf/ en la raíz" >&2; exit 1; }

# ── identidad de la campaña: sin ella no hay dónde aislar, y no se adivina
CAMPAIGN_ID="${CAMPAIGN_ID:-}"
if [ -z "$CAMPAIGN_ID" ]; then
  echo "ERROR: sin CAMPAIGN_ID. Los finalistas se aíslan por campaña; correr suelto dejaría" >&2
  echo "       artefactos sin procedencia mezclados con los de la última corrida. Aborta." >&2
  exit 1
fi
STAGING="models/.staging/${CAMPAIGN_ID}"

step() {
  local label="$1"; shift
  echo ">>> $label"
  "$@" && return 0
  local rc=$?
  echo "##### PASO FALLIDO (exit $rc): $label :: $*" >&2
  echo "##### FAIL-FAST INTERNO: se detiene aquí. NADA se exporta, sincroniza ni promueve." >&2
  echo "##### El árbol servido queda INTACTO; lo a medias vive en $STAGING y no lo lee nadie." >&2
  exit "$rc"
}

echo "=== GUARDAR FINALISTAS $(date) · campaña $CAMPAIGN_ID ==="
rm -rf "$STAGING"
mkdir -p "$STAGING"
# ★ R14 · hasta aquí estas dos variables NO las leía nadie: los productores escribían en `models/`
# y el paso [2b/4] moría buscando `$STAGING/manifest.jsonl`, tras las horas de la etapa deep.
# `save_finalists.py` y `save_finalists_deep.py` las honran ahora (`models_root()`/`manifest_path()`).
export VP_MODELS_DIR="$STAGING"   # los productores escriben AQUÍ, no en models/
export VP_MODELS_MANIFEST="$STAGING/manifest.jsonl"

step "[1/4] modelos GLOBALES deep (ante_nf)" ante_nf/bin/python experiments/save_finalists_deep.py
step "[2/4] modelos LOCALES por serie (ante)" ante/bin/python experiments/save_finalists.py

# ── promoción: SÓLO con cobertura acreditada. Antes de esto, `models/` no ha cambiado.
step "[2b/4] acreditar cobertura y promover el árbol" \
  ante/bin/python experiments/promote_finalists.py --staging "$STAGING" --campaign-id "$CAMPAIGN_ID"

# ★ §8.7 · el PRODUCTOR del transporte, ANTES de su único consumidor. Sin este paso el
# exportador falla cerrado buscando un artefacto que nadie escribe, y `ets`/`theta` —que
# AutoETS/AutoTheta impiden recalcular— quedarían fuera del CSV como llevaban quedando.
step "[2c/4] transporte de ETS/Theta desde el walk-forward (ante)" \
  ante/bin/python experiments/persist_transported_forecasts.py

step "[3/4] pronosticos finalistas -> CSV tidy (ante)" ante/bin/python experiments/export_forecasts.py
echo ">>> [4/4] stage forecasts + sync LOCAL (sin push)"
git add reports/eval/finalist_forecasts_*.csv 2>/dev/null
step "sync_all LOCAL (sin push)" env SYNC_PUBLISH=0 bash experiments/sync_all.sh \
  "finalistas: $(ls models -R 2>/dev/null | grep -c model) modelos + forecasts ($(date +%Y-%m-%d))"
echo "=== FINALISTAS $(date) · todos los pasos en verde ==="
exit 0
