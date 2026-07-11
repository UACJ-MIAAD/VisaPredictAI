#!/usr/bin/env bash
# Valida que el repo conserve la estructura cookiecutter documentada en el README.
# v2 (plan V3, U1): además de exigir lo canónico, la RAÍZ es un whitelist cerrado —
# el guardián anterior tenía los 19 scripts sueltos en su lista `required`, o sea
# que codificaba el desorden en lugar de impedirlo. Falla (exit 1) si:
#   (a) falta un dir/archivo canónico,
#   (b) aparece CUALQUIER cosa en la raíz fuera del whitelist (p. ej. un .py nuevo),
#   (c) hay archivos sueltos untracked-y-no-ignorados en el working tree.
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0

# (a) Directorios y archivos canónicos que SIEMPRE deben existir.
required=(
  data/raw data/processed docs tests tools experiments
  pipeline vp_data vp_model
  reports/latex/Figures reports/campaign reports/eval reports/prospective reports/governance reports/eda .github/workflows
  Makefile pyproject.toml README.md schema.sql dvc.yaml
  pipeline/freeze_snapshots.py pipeline/scrape_all.py
  pipeline/build_panel.py pipeline/build_database.py pipeline/mega_audit.py
  vp_data/config.py vp_data/visa_common.py vp_data/tracking.py
  reports/latex/ProyectoI_VisaPredictAI.tex reports/governance/key_facts.json
  reports/eda/eda_facts.json
)
for p in "${required[@]}"; do
  if [ ! -e "$p" ]; then echo "FALTA: $p"; fail=1; fi
done

# (b) Whitelist EXACTO de la raíz. Todo lo demás es deriva estructural.
#     (Incluye artefactos locales gitignored conocidos: venvs, caches, MLflow, DVC.)
allowed_root=(
  .claude .coverage .DS_Store .dvc .dvcignore .editorconfig .git .github
  .gitignore .mypy_cache .pre-commit-config.yaml .pytest_cache .python-version
  .ruff_cache __pycache__ htmlcov lightning_logs
  ante ante_nf ante_tab ante_tfm aws_gpu data docs experiments locks pipeline
  reports tests tools vp_data vp_model
  mlartifacts mlflow.db mlflow.db.dvc mlruns mlruns_staging models models.dvc
  visapredictai.egg-info
  CHANGELOG.md CLAUDE.md CODE_OF_CONDUCT.md CONTRIBUTING.md LICENSE Makefile
  README.md SECURITY.md dvc.lock dvc.yaml pyproject.toml schema.sql
)
while IFS= read -r entry; do
  ok=0
  case "$entry" in .coverage.*) ok=1 ;; esac   # shards de coverage paralelo
  for a in "${allowed_root[@]}"; do
    if [ "$entry" = "$a" ]; then ok=1; break; fi
  done
  if [ "$ok" -eq 0 ]; then
    echo "RAÍZ FUERA DE WHITELIST: $entry (¿script suelto? va en pipeline/, vp_data/, experiments/ o tools/)"
    fail=1
  fi
done < <(ls -A .)

# (b bis) Cero módulos Python en la raíz — la regla que motivó el plan V3.
if compgen -G "*.py" > /dev/null; then
  echo "PY EN RAÍZ (prohibido): $(echo *.py)"
  fail=1
fi

# (c) Clutter real: untracked-y-no-ignorado. Lo que escapó a la estructura.
loose=$(git status --porcelain --untracked-files=all | grep '^??' | cut -c4- || true)
if [ -n "$loose" ]; then
  echo "SUELTOS (ni trackeados ni gitignored):"
  echo "$loose" | sed 's/^/  /'
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "OK estructura cookiecutter intacta · raíz limpia (0 .py) · 0 archivos sueltos"
else
  echo "--- estructura ROTA (ver arriba) ---"
fi
exit "$fail"
