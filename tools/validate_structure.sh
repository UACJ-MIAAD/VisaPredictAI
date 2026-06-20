#!/usr/bin/env bash
# Valida que el repo conserve la estructura cookiecutter documentada en el README.
# Falla (exit 1) si: (a) falta un dir/archivo canónico, o (b) hay archivos sueltos
# en el working tree que no están trackeados NI gitignored ("archivos por todos lados").
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0

# (a) Directorios y archivos canónicos que SIEMPRE deben existir.
required=(
  data/raw data/processed docs tests vp_model
  reports/latex/Figures .github/workflows
  Makefile pyproject.toml README.md schema.sql
  visa_common.py config.py freeze_snapshots.py scrape_all.py
  build_panel.py build_database.py mega_audit.py
  reports/latex/ProyectoI_VisaPredictAI.tex
)
for p in "${required[@]}"; do
  if [ ! -e "$p" ]; then echo "FALTA: $p"; fail=1; fi
done

# (b) Clutter real: untracked-y-no-ignorado. Lo que escapó a la estructura.
loose=$(git status --porcelain --untracked-files=all | grep '^??' | cut -c4- || true)
if [ -n "$loose" ]; then
  echo "SUELTOS (ni trackeados ni gitignored):"
  echo "$loose" | sed 's/^/  /'
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "OK estructura cookiecutter intacta · 0 archivos sueltos"
else
  echo "--- estructura ROTA (ver arriba) ---"
fi
exit "$fail"
