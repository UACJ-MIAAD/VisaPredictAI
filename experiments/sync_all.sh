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
[ -x ante_nf/bin/python ] || { echo "ERROR: falta el venv ante_nf/ en la raíz del repo" >&2; exit 1; }

PUBLISH="${SYNC_PUBLISH:-0}"
if [ "${1:-}" = "--publish" ]; then PUBLISH=1; shift; fi
MSG="${1:-experiments: sync MLflow + DVC->S3 ($(date +%Y-%m-%d' '%H:%M))}"

# ⚠️ Publicar una campaña DIAGNÓSTICA (dirty=true) queda BLOQUEADO (auditoría 13-jul-2026):
# los ledgers pudieron sellar dirty=False mientras el manifiesto marca dirty=true — publicar
# esas cifras sería una mentira. CAMPAIGN_DIAGNOSTIC no debe poder llegar a producción.
if [ "$PUBLISH" = 1 ] && [ -f reports/campaign/campaign_manifest.json ]; then
  if grep -q '"dirty": *true' reports/campaign/campaign_manifest.json; then
    echo "ERROR: el manifiesto de campaña marca dirty=true (campaña diagnóstica). Publicar" >&2
    echo "       está BLOQUEADO — re-lanza una campaña OFICIAL desde árbol limpio antes de" >&2
    echo "       --publish. Aborta." >&2
    exit 7
  fi
fi

echo ">>> [1/3] MLflow: staging -> mlflow.db"
ante_nf/bin/python experiments/sync_mlflow.py

echo ">>> [2/3] DVC: re-hash local (modelos + tracking)"
dvc add models mlflow.db

echo ">>> [3/3] git: pointers .dvc"
git add models.dvc mlflow.db.dvc .gitignore
if git diff --cached --quiet; then
    echo "    sin cambios que commitear"
elif [ "$PUBLISH" = 1 ]; then
    dvc push
    git commit -q -m "$MSG"
    git push
    echo "    ✓ PUBLICADO: dvc push + commit + git push"
else
    echo "    ⏸ LOCAL-ONLY: pointers .dvc staged, sin commit/push/dvc-push."
    echo "      Revisa (consistencia, cobertura, identidad) y publica con: bash experiments/sync_all.sh --publish"
fi
echo "✓ sync_all OK ($(date +%H:%M))"
