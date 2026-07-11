#!/usr/bin/env bash
# H1 (plan auditoría 2026-07-11): gate de calidad sobre el .log de pdflatex.
#
#   bash tools/check_latex_log.sh <doc.log> [MAX_OVERFULL_COUNT] [MAX_OVERFULL_PT]
#
# Falla si el log tiene referencias o citas indefinidas, labels multiply-defined,
# o pide otra pasada ("Rerun to get"), o si el censo de overfull supera el umbral
# documentado del documento. Imprime SIEMPRE el censo (es la baseline auditable).
# El gate es de VERIFICACIÓN: Overleaf sigue siendo el compilador oficial del PDF.
# (Archivo en modo 100644 a propósito — invocar con `bash`, jamás chmod +x.)
set -euo pipefail

LOG="$1"
MAX_N="${2:-999}"
MAX_PT="${3:-999}"
fail=0

undef_ref=$(grep -c "LaTeX Warning: Reference .* undefined" "$LOG" || true)
undef_cit=$(grep -c "LaTeX Warning: Citation .* undefined" "$LOG" || true)
multi=$(grep -c "multiply.defined" "$LOG" || true)
rerun=$(grep -c "Rerun to get" "$LOG" || true)
over_n=$(grep -c '^Overfull \\hbox' "$LOG" || true)
over_max=$(grep -o '^Overfull \\hbox ([0-9.]*pt' "$LOG" | grep -o '[0-9.]*' | sort -g | tail -1)
over_max=${over_max:-0}

echo "censo $(basename "$LOG"): undef_ref=$undef_ref undef_cit=$undef_cit multiply=$multi rerun=$rerun overfull_n=$over_n overfull_max=${over_max}pt (umbral: n<=$MAX_N, max<=${MAX_PT}pt)"

if [ "$undef_ref" -gt 0 ]; then
  echo "✗ referencias indefinidas:"
  grep "LaTeX Warning: Reference" "$LOG" | head -8
  fail=1
fi
if [ "$undef_cit" -gt 0 ]; then
  echo "✗ citas indefinidas:"
  grep "LaTeX Warning: Citation" "$LOG" | head -8
  fail=1
fi
if [ "$multi" -gt 0 ]; then
  echo "✗ labels multiply-defined:"
  grep -i "multiply.defined" "$LOG" | head -8
  fail=1
fi
if [ "$rerun" -gt 0 ]; then
  echo "✗ el documento pide otra pasada (refs inestables tras 3 pasadas)"
  fail=1
fi
if [ "$over_n" -gt "$MAX_N" ]; then
  echo "✗ $over_n overfull hbox (> umbral $MAX_N)"
  fail=1
fi
if ! awk -v a="$over_max" -v b="$MAX_PT" 'BEGIN{exit !(a<=b)}'; then
  echo "✗ overfull máximo ${over_max}pt (> umbral ${MAX_PT}pt):"
  grep '^Overfull \\hbox' "$LOG" | sort -t'(' -k2 -gr | head -3
  fail=1
fi

exit $fail
