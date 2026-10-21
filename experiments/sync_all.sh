#!/bin/bash
# "TODO MACHIN" en dos modos que NO se mezclan (G7 · desbloqueo, fase 3):
#
#   PREPARAR (por defecto): deja todo sincronizado tras una corrida de experimentos —
#     1. MLflow   : staging JSONL -> mlflow.db (todas las métricas/params/tags);
#     2. DVC      : re-hashea modelos + mlflow.db con el DVC gobernado (`dvc add`);
#     3. git      : deja los pointers .dvc STAGED para revisión humana. NO commitea ni empuja nada.
#   Esto cambia el árbol, así que ocurre ANTES del candidato, del sello del corte, del CI y de la firma.
#
#   PUBLICAR (--publish o SYNC_PUBLISH=1): consume el permiso de una release YA APROBADA E INTEGRADA. No sincroniza,
#   no re-hashea, no commitea y no empuja git: la release entró a `main` por su PR con EXACTAMENTE el árbol que firmó el
#   recibo /3, y este checkout debe estar en ese commit, limpio. `campaign_txn publish-release` comprueba, bajo el lock
#   de la campaña y ANTES de cualquier efecto externo, la puerta (estado, historial, recibo, manifiesto, vínculo), el
#   árbol de HEAD, el checkout limpio y que HEAD esté en `origin/main`; después hace `dvc push` (direccionado por
#   contenido) y escribe `published`. Antes, `--publish` commiteaba y empujaba git y SÓLO DESPUÉS `txn publish` comprobaba
#   el árbol: con un recibo /3 eso se bloqueaba siempre, pero con el commit ya en el remoto.
#
# NO toca los artefactos del pipeline de datos (panel/parquet/duckdb): esos los gobierna dvc.yaml (`dvc repro`).
#
# Uso:  bash experiments/sync_all.sh                   # preparar (local-only)
#       bash experiments/sync_all.sh --publish         # publicar la release aprobada que hay en HEAD
# Entorno: CAMPAIGN_TXN y CAMPAIGN_MANIFEST (rutas de la transacción y del manifiesto de la campaña; por defecto las de
# reports/campaign/), SYNC_MAIN_REF (por defecto origin/main), VP_DVC (DVC gobernado),
# VP_PUBLISH_PYTHON (perfil model; ante/bin/python), VP_TRACKING_PYTHON (MLflow; ante_nf/bin/python).
set -euo pipefail
cd "$(dirname "$0")/.."
# ★ R14 · el DVC que re-hashea los punteros es el GOBERNADO (`$VP_DVC` o `ante/bin/dvc`, la misma
# regla que `tools/check_dvc_lock_fresh.py` y el Makefile), no un `dvc` a secas del PATH del
# operador: en el worktree de ejecución no hay `ante/bin/dvc` y el PATH resolvía a un Homebrew
# 3.66.1 que ninguna puerta veía. Sin binario gobernado, fail-closed antes de tocar nada.
DVC="${VP_DVC:-ante/bin/dvc}"
[ -x "$DVC" ] || { echo "ERROR: sin DVC gobernado (\$VP_DVC o ante/bin/dvc): no se re-hashea nada" >&2; exit 1; }

PUBLISH="${SYNC_PUBLISH:-0}"
if [ "${1:-}" = "--publish" ]; then PUBLISH=1; shift; fi
[ "$#" = 0 ] || { echo "ERROR: argumentos desconocidos: $*" >&2; exit 2; }
case "$PUBLISH" in 0|1) ;; *) echo "ERROR: SYNC_PUBLISH debe ser 0 o 1" >&2; exit 2 ;; esac
if [ "$PUBLISH" = 1 ]; then
  PY="${VP_PUBLISH_PYTHON:-ante/bin/python}"
  PROFILE=publish
  PY_VAR=VP_PUBLISH_PYTHON
else
  PY="${VP_TRACKING_PYTHON:-ante_nf/bin/python}"
  PROFILE=prepare
  PY_VAR=VP_TRACKING_PYTHON
fi
[ -x "$PY" ] || { echo "ERROR: $PY_VAR no es un intérprete ejecutable: $PY" >&2; exit 12; }
# Importar los lectores reales ANTES de MLflow, DVC, staging o transiciones. Perfil incompleto: fail-closed.
"$PY" tools/publication_runtime.py --profile "$PROFILE" || exit 12
MANIFEST="${CAMPAIGN_MANIFEST:-reports/campaign/campaign_manifest.json}"
MAIN_REF="${SYNC_MAIN_REF:-origin/main}"

# ★ G7 · el índice tiene que llegar LIMPIO: la decisión de commitear mira todo lo preparado, así que un archivo
# ajeno ya en staging se commiteaba (y con --publish se publicaba) junto a los punteros. Se rechaza ANTES de
# tocar MLflow, DVC o el índice, y lo preparado se preserva tal cual.
if ! git diff --cached --quiet; then
    echo "ERROR: hay cambios preparados ajenos a sync_all: $(git diff --cached --name-only | tr '\n' ' ')" >&2
    echo "       se preservan sin tocar; sácalos del índice o commitéalos antes de sincronizar." >&2
    exit 11
fi

# ⚠️ Publicar exige un manifiesto de campaña que EXISTA, sea JSON válido y selle dirty=false
# BOOLEANO explícito (contrato único fail-closed: tools/campaign_manifest.py). CAMPAIGN_DIAGNOSTIC NO llega a producción.
publishable() { "$PY" -m tools.campaign_manifest --assert-publishable "$MANIFEST"; }
# ⚠️ SEGUNDA puerta, y la que manda (ADR 0003): el manifiesto acredita la IDENTIDAD de la campaña; la transacción
# acredita su ESTADO. Sólo `validated` autoriza publicar; la ausencia de transacción también bloquea.
CAMPAIGN_TXN="${CAMPAIGN_TXN:-reports/campaign/campaign.json}"
txn_guard() { "$PY" -m tools.campaign_txn --path "$CAMPAIGN_TXN" guard --manifest "$MANIFEST"; }
# H3/H10: publicar CONSUME el permiso, y sólo después del efecto externo (si `dvc push` falla, sigue `validated`).
# ★ G7 · desbloqueo: una sola operación bajo el lock de la campaña: puerta + árbol + limpio + integrada → dvc push → published.
txn_publish_release() {
  "$PY" -m tools.campaign_txn --path "$CAMPAIGN_TXN" publish-release --manifest "$MANIFEST" \
    --repo . --main-ref "$MAIN_REF"
}

if [ "$PUBLISH" = 1 ]; then
  publishable || exit 7
  txn_guard || exit 8
  txn_publish_release || exit 9
  echo "    ✓ PUBLICADO: release aprobada en HEAD, dvc push y transacción en 'published' (sin commit ni git push)"
  echo "✓ sync_all OK ($(date +%H:%M))"
  exit 0
fi

echo ">>> [1/3] MLflow: staging -> mlflow.db"
"$PY" experiments/sync_mlflow.py

echo ">>> [2/3] DVC: re-hash local (modelos + tracking)"
"$DVC" add models mlflow.db

echo ">>> [3/3] git: pointers .dvc"
git add models.dvc mlflow.db.dvc .gitignore
if git diff --cached --quiet; then
    echo "    sin cambios que commitear"
else
    echo "    ⏸ PREPARADO: pointers .dvc staged, sin commit/push/dvc-push."
    echo "      Commitéalos en la rama del candidato ANTES de sellar el corte y pedir la firma; publicar es --publish."
fi
echo "✓ sync_all OK ($(date +%H:%M))"
