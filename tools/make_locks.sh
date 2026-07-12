#!/usr/bin/env bash
# C3 (plan auditoría 2026-07-11): locks TRANSITIVOS por perfil de instalación.
#
#   bash tools/make_locks.sh          # (o: make lock)
#
# - locks/runtime.txt   : venv FRESCO con `pip install -e .`        (perfil de datos puros)
# - locks/dev.txt       : venv FRESCO con `pip install -e .[dev]`   (perfil CI/lint/tests)
# - locks/model-cpu.txt : freeze del venv ante/ (dev+model CPU) — el ENTORNO DE REFERENCIA
#                         que produjo las cifras publicadas, congelado tal cual.
# - locks/*-linux-x86_64.txt : espejos Linux de los tres perfiles (uv pip compile, A5
#                         2026-07-12); runtime/dev con hashes para --require-hashes en CI.
# - GPU/deep            : ya versionado en aws_gpu/ante_nf-requirements.lock (bundle EC2).
#
# Los locks macOS NO llevan hashes ni secretos (se verifica); los Linux runtime/dev SÍ
# llevan hashes. Regenerarlos es una decisión deliberada (upgrade auditado), no parte de
# ningún build. Invocar con `bash` (100644).
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

# A5 (plan auditoría 2026-07-12): perfiles Linux x86_64 para que CI instale EXACTAMENTE
# lo verificado. Derivados con `uv pip compile` CONSTREÑIDOS por los locks macOS de
# referencia (-c): mismas versiones, wheels de la otra plataforma. runtime/dev llevan
# hashes (CI instala con --require-hashes); model-cpu se consume como constraints (-c),
# donde pip no admite hashes. Requiere `uv` en PATH (brew install uv).
if ! command -v uv >/dev/null 2>&1; then
  echo "✗ uv no está en PATH: faltan los locks Linux x86_64 (brew install uv)" && exit 1
fi
echo "make_locks: perfiles Linux x86_64 (uv pip compile, constreñidos por los de referencia)…"
uv pip compile -q pyproject.toml --python-version 3.14 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  -c locks/runtime.txt -o locks/runtime-linux-x86_64.txt
echo "  ✓ locks/runtime-linux-x86_64.txt ($(grep -c '==' locks/runtime-linux-x86_64.txt) pins)"
uv pip compile -q pyproject.toml --extra dev --python-version 3.14 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  -c locks/dev.txt -o locks/dev-linux-x86_64.txt
echo "  ✓ locks/dev-linux-x86_64.txt ($(grep -c '==' locks/dev-linux-x86_64.txt) pins)"
uv pip compile -q pyproject.toml --extra dev --extra model --python-version 3.14 \
  --python-platform x86_64-unknown-linux-gnu \
  -c locks/model-cpu.txt -o locks/model-cpu-linux-x86_64.txt
echo "  ✓ locks/model-cpu-linux-x86_64.txt ($(grep -c '==' locks/model-cpu-linux-x86_64.txt) pins)"

# Ningún secreto: las líneas `nombre==versión` son seguras por construcción (el paquete
# `tokenizers` cazó el primer grep ingenuo); se escanea todo lo que NO tenga esa forma.
# Clase POSIX portable: `]` va PRIMERO dentro de [...] — con `\[` el grep BSD cerraba la
# clase prematuramente y el filtro no excluía nada (cazado en el estreno del guard).
if grep -hv -e '^#' -e '^[][A-Za-z0-9_.-]\{1,\}==' locks/*.txt | grep -qiE "secret|token|password|aws_access|://.*@"; then
  echo "✗ posible secreto en locks/ — revisar" && exit 1
fi
echo "make_locks: OK — sin secretos, 3 perfiles + GPU en aws_gpu/ante_nf-requirements.lock"
