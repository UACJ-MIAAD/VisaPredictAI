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
#
# M65: era FAIL-OPEN fuera de Linux. Los .log de pdfTeX son ISO-8859 (basta un acento:
# el de ProyectoI lleva 7 bytes >0x7F por «público»), y el `grep` de macOS bajo un locale
# UTF-8 los declara binarios: cuando el patrón SÍ coincide no imprime nada y sale con 1.
# El conteo quedaba VACÍO, `[ "" -gt 0 ]` fallaba dentro de un `if` (exento de `set -e`) y
# el gate pasaba igual. De ahí las tres reglas de abajo: LC_ALL=C, `grep -a` (lectura
# textual) y un conteo que sólo puede ser un entero — cualquier otra cosa aborta.
set -euo pipefail
export LC_ALL=C

if [ "$#" -lt 1 ]; then
  echo "uso: bash tools/check_latex_log.sh <doc.log> [MAX_OVERFULL_COUNT] [MAX_OVERFULL_PT]" >&2
  exit 2
fi

LOG="$1"
MAX_N="${2:-999}"
MAX_PT="${3:-999}"
fail=0

# Un log ausente o ilegible es un FALLO del gate, no un censo en cero: sin esto, un typo en
# la ruta o un artefacto que no se generó se leían como "documento limpio".
if [ ! -f "$LOG" ] || [ ! -r "$LOG" ]; then
  echo "✗ no se puede leer el log: $LOG" >&2
  exit 2
fi

# Cuenta coincidencias y devuelve SIEMPRE un entero. grep: 0 = hubo coincidencias,
# 1 = ninguna, >=2 = error de lectura. Un error de lectura aborta; una salida que no sea
# un entero también. Nunca puede quedarse vacío en silencio.
contar() {
  local pat="$1" n rc=0
  n=$(grep -a -c -e "$pat" "$LOG") || rc=$?
  if [ "$rc" -ge 2 ]; then
    echo "✗ error leyendo $LOG (grep rc=$rc) con el patrón: $pat" >&2
    exit 2
  fi
  case "$n" in
    '' | *[!0-9]*)
      echo "✗ conteo no numérico ('$n') para el patrón: $pat" >&2
      exit 2
      ;;
  esac
  printf '%s' "$n"
}

# `exit` dentro de $( ) mata el SUBSHELL, no el script: hay que propagar su estado a mano
# o el conteo volvería a quedar vacío, que es exactamente el fail-open que esto cierra.
undef_ref=$(contar "LaTeX Warning: Reference .* undefined") || exit $?
undef_cit=$(contar "LaTeX Warning: Citation .* undefined") || exit $?
multi=$(contar "multiply.defined") || exit $?
rerun=$(contar "Rerun to get") || exit $?
over_n=$(contar '^Overfull \\hbox') || exit $?

# El máximo sólo se busca cuando hay overfulls: así el pipeline no depende de que un grep
# sin coincidencias "salga bien", y el valor que se compara siempre es un número real.
over_max=0
if [ "$over_n" -gt 0 ]; then
  over_max=$(grep -a -o '^Overfull \\hbox ([0-9.]*pt' "$LOG" | grep -a -o '[0-9][0-9.]*' | sort -g | tail -1 || true)
  case "$over_max" in
    '' | *[!0-9.]*)
      echo "✗ no se pudo leer el overfull máximo de $LOG (valor: '$over_max')" >&2
      exit 2
      ;;
  esac
fi

echo "censo $(basename "$LOG"): undef_ref=$undef_ref undef_cit=$undef_cit multiply=$multi rerun=$rerun overfull_n=$over_n overfull_max=${over_max}pt (umbral: n<=$MAX_N, max<=${MAX_PT}pt)"

if [ "$undef_ref" -gt 0 ]; then
  echo "✗ referencias indefinidas:"
  grep -a "LaTeX Warning: Reference" "$LOG" | head -8
  fail=1
fi
if [ "$undef_cit" -gt 0 ]; then
  echo "✗ citas indefinidas:"
  grep -a "LaTeX Warning: Citation" "$LOG" | head -8
  fail=1
fi
if [ "$multi" -gt 0 ]; then
  echo "✗ labels multiply-defined:"
  grep -a -i "multiply.defined" "$LOG" | head -8
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
  grep -a '^Overfull \\hbox' "$LOG" | sort -t'(' -k2 -gr | head -3
  fail=1
fi

exit $fail
