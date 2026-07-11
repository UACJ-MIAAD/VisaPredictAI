#!/usr/bin/env bash
# C3 (plan auditoría 2026-07-11): locks TRANSITIVOS por perfil de instalación.
#
#   bash tools/make_locks.sh          # (o: make lock)
#
# - locks/runtime.txt   : venv FRESCO con `pip install -e .`        (perfil de datos puros)
# - locks/dev.txt       : venv FRESCO con `pip install -e .[dev]`   (perfil CI/lint/tests)
# - locks/model-cpu.txt : freeze del venv ante/ (dev+model CPU) — el ENTORNO DE REFERENCIA
#                         que produjo las cifras publicadas, congelado tal cual.
# - GPU/deep            : ya versionado en aws_gpu/ante_nf-requirements.lock (bundle EC2).
#
# Los locks NO llevan hashes ni secretos (se verifica); regenerarlos es una decisión
# deliberada (upgrade auditado), no parte de ningún build. Invocar con `bash` (100644).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-ante/bin/python}"
STAMP="$(date -u +%Y-%m-%d)"
mkdir -p locks

header() {
  echo "# Lock transitivo del perfil '$1' — generado por tools/make_locks.sh el $STAMP"
  echo "# Python $($2 --version 2>&1 | cut -d' ' -f2) · $(uname -sm). Instalar con:"
  echo "#   pip install -r locks/$1.txt && pip install -e . --no-deps"
}

freeze_profile() {  # $1 = perfil, $2 = extras pip ("" | "[dev]")
  local tmp
  tmp="$(mktemp -d)"
  "$PY" -m venv "$tmp/env"
  "$tmp/env/bin/pip" install -q --upgrade pip >/dev/null
  "$tmp/env/bin/pip" install -q -e ".$2" >/dev/null
  { header "$1" "$tmp/env/bin/python"; "$tmp/env/bin/pip" freeze --exclude-editable; } > "locks/$1.txt"
  rm -rf "$tmp"
  echo "  ✓ locks/$1.txt ($(grep -vc '^#' "locks/$1.txt") paquetes)"
}

echo "make_locks: perfiles runtime y dev (venvs frescos)…"
freeze_profile runtime ""
freeze_profile dev "[dev]"

echo "make_locks: perfil model-cpu (freeze del entorno de referencia ante/)…"
{ header "model-cpu" "$PY"; "${PY%python}pip" freeze --exclude-editable; } > locks/model-cpu.txt
echo "  ✓ locks/model-cpu.txt ($(grep -vc '^#' locks/model-cpu.txt) paquetes)"

# Ningún secreto: las líneas `nombre==versión` son seguras por construcción (el paquete
# `tokenizers` cazó el primer grep ingenuo); se escanea todo lo que NO tenga esa forma.
# Clase POSIX portable: `]` va PRIMERO dentro de [...] — con `\[` el grep BSD cerraba la
# clase prematuramente y el filtro no excluía nada (cazado en el estreno del guard).
if grep -hv -e '^#' -e '^[][A-Za-z0-9_.-]\{1,\}==' locks/*.txt | grep -qiE "secret|token|password|aws_access|://.*@"; then
  echo "✗ posible secreto en locks/ — revisar" && exit 1
fi
echo "make_locks: OK — sin secretos, 3 perfiles + GPU en aws_gpu/ante_nf-requirements.lock"
