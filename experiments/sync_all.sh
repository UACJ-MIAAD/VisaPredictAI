#!/bin/bash
# "TODO MACHIN": deja todo sincronizado tras una corrida de experimentos —
#   1. MLflow   : staging JSONL -> mlflow.db (todas las métricas/params/tags).
#   2. DVC -> S3: re-hashea y sube modelos + mlflow.db (el tracking entero).
#   3. git      : commitea+pushea los pointers .dvc (para que cualquiera haga `dvc pull`).
# Idempotente: si nada cambió, no commitea.
# NO toca los artefactos del pipeline de datos (panel/parquet/duckdb): esos los gobierna
# dvc.yaml (`dvc repro`); `dvc add` sobre salidas de stage lo rechaza DVC (E1/M3) y el
# .duckdb está des-trackeado a propósito (no determinista).
#
# ⚠️ PUBLICAR ES OPT-IN (auditoría 12-jul-2026). Por defecto sync_all opera SOLO en local:
# sincroniza MLflow, re-hashea con `dvc add` y DEJA los pointers .dvc staged para revisión
# humana. NO commitea, NO hace `git push` ni `dvc push`. Publicar exige `--publish` explícito
# (o SYNC_PUBLISH=1). Motivo: un runbook nunca debe auto-publicar cifras sin validación
# (consistencia + DVC + cobertura + identidad + revisión humana) — ver run_rederivation.sh.
#
# Uso:  bash experiments/sync_all.sh ["mensaje"]            # local-only (default)
#       bash experiments/sync_all.sh --publish ["mensaje"]  # commit + git push + dvc push
set -euo pipefail
cd "$(dirname "$0")/.."
# R9.4: bootstrap orquestador (stdlib-only tools.python_env). La LÓGICA DE PRODUCTO corre en los entornos
# content-addressed que abre `run-command`; el CLI DVC sigue AISLADO en dvc-tool vía `python_env exec`.
PYBOOT=${PYBOOT:-python3.14}
command -v "$PYBOOT" >/dev/null 2>&1 || { echo "ERROR: falta $PYBOOT (bootstrap del orquestador)" >&2; exit 1; }
runc() { "$PYBOOT" -m tools.python_env run-command --id "$1" -- "${@:2}"; }

PUBLISH="${SYNC_PUBLISH:-0}"
if [ "${1:-}" = "--publish" ]; then PUBLISH=1; shift; fi
MSG="${1:-experiments: sync MLflow + DVC->S3 ($(date +%Y-%m-%d' '%H:%M))}"
MANIFEST=reports/campaign/campaign_manifest.json

# P0R.5 R3: DVC corre EXCLUSIVAMENTE desde su entorno content-addressed aislado (.vp_envs/dvc-tool),
# vía la interfaz única `python_env exec` (aplica el cache guard, prohíbe el binario legacy y evita
# que las deps de dvc degraden el producto). NUNCA un `dvc` suelto ni de PATH.
DVC="$PYBOOT -m tools.python_env exec --profile dvc-tool -- dvc"

# ⚠️ Publicar exige un manifiesto de campaña que EXISTA, sea JSON válido y selle dirty=false
# BOOLEANO explícito (contrato único fail-closed: tools/campaign_manifest.py). Fail-closed ante
# manifiesto ausente/ilegible/malformado, sin la clave `dirty`, o dirty!=false (auditoría
# 13-jul-2026 ronda 8: el grep '"dirty": *true' era FAIL-OPEN — no cubría ausencia/malformado/
# `{"dirty" : true}` con espacios ni cambios TOCTOU). CAMPAIGN_DIAGNOSTIC NO llega a producción.
publishable() { runc campaign_manifest --assert-publishable "$MANIFEST"; }
manifest_sha() { runc hash_file "$MANIFEST"; }   # R9.4/B66: extraído del `-c` hashlib (tools/hash_file.py)
if [ "$PUBLISH" = 1 ]; then
  publishable || exit 7
  PUB_SHA0="$(manifest_sha)"
fi

echo ">>> [1/3] MLflow: staging -> mlflow.db"
runc sync_mlflow

echo ">>> [2/3] DVC: re-hash local (modelos + tracking)"
$DVC add models mlflow.db

echo ">>> [3/3] git: pointers .dvc"
git add models.dvc mlflow.db.dvc .gitignore
if git diff --cached --quiet; then
    echo "    sin cambios que commitear"
elif [ "$PUBLISH" = 1 ]; then
    # Re-valida el manifiesto JUSTO antes de publicar (cierra TOCTOU: pudo cambiar entre el
    # gate inicial y aquí) y exige que su contenido siga BYTE-idéntico al validado.
    publishable || exit 7
    [ "$(manifest_sha)" = "$PUB_SHA0" ] || {
        echo "ERROR: el manifiesto de campaña cambió durante sync_all (TOCTOU). Aborta." >&2
        exit 7
    }
    $DVC push
    git commit -q -m "$MSG"
    git push
    echo "    ✓ PUBLICADO: dvc push + commit + git push"
else
    echo "    ⏸ LOCAL-ONLY: pointers .dvc staged, sin commit/push/dvc-push."
    echo "      Revisa (consistencia, cobertura, identidad) y publica con: bash experiments/sync_all.sh --publish"
fi
echo "✓ sync_all OK ($(date +%H:%M))"
