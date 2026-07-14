#!/usr/bin/env bash
# Matriz de 9 locks TRANSITIVOS por perfil (P0R.4, ronda 10). Invocar con `bash` (100644):
#
#   bash tools/make_locks.sh          # (o: make lock)
#
# Perfiles del proyecto base (pandas 3, pyproject.toml):
#   locks/runtime.txt              venv FRESCO `pip install -e .`             (datos puros)
#   locks/dev.txt                  venv FRESCO `pip install -e .[dev]`        (CI/lint/tests)
#   locks/model-cpu.txt            venv FRESCO `pip install -e .[dev,model]`  (modelado CPU)
#   locks/{runtime,dev,model-cpu}-linux-x86_64.txt   espejos Linux (uv pip compile)
#
# Perfil DEEP (pandas 2.x, requirements/deep.in — AISLADO del base):
#   locks/deep-macos-arm64.txt         cierre nativo macOS arm64 (uv, HASHEADO)
#   locks/deep-linux-x86_64-cpu.txt    Linux CPU  torch 2.12.1+cpu   (uv, HASHEADO)
#   locks/deep-linux-x86_64-cu126.txt  Linux CUDA torch 2.12.1+cu126 (uv, HASHEADO)
#
# CONTRATOS P0R.4 / P0R.4R:
#  - toolchain PINEADO (python 3.14 + pip/setuptools/wheel/uv exactos) — sin flotar al del día;
#  - SIN fecha en los headers -> REGENERAR es repetible bajo el MISMO estado del índice (bytes
#    idénticos); la instalación desde los locks sí es byte-reproducible. NO se promete que el
#    resolver produzca los mismos transitivos meses después (el índice upstream cambia);
#  - los 9 se resuelven en STAGING; la promoción a locks/ tiene ROLLBACK TRANSACCIONAL y DETECCIÓN
#    DE MATRIZ PARCIAL (tools/promote_lockset.py valida el staging con tools/lock_contracts.py,
#    escribe el manifiesto locks/lockset.json AL FINAL y se autovalida) — NO es atomicidad de bundle;
#  - ninguna ruta temporal de staging se filtra a los locks (espejos Linux con --no-annotate);
#  - regenerarlos es una decisión DELIBERADA (upgrade auditado), nunca parte de un build.
set -euo pipefail
cd "$(dirname "$0")/.."

# --- 5.1 bootstrap: intérprete + toolchain PINEADOS (no ante/, no versiones del día) ----------
PY="${PY:-python3.14}"
PIP_VERSION="26.1.2"
SETUPTOOLS_VERSION="81.0.0"
WHEEL_VERSION="0.47.0"
UV_VERSION="0.11.28"

command -v "$PY" >/dev/null 2>&1 || { echo "✗ intérprete '$PY' no está en PATH" >&2; exit 1; }
PY_VER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
PY_FULL="$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"   # X.Y.Z para el manifiesto
[ "$PY_VER" = "3.14" ] || { echo "✗ se requiere Python 3.14 (hay $PY_VER en '$PY')" >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "✗ uv no está en PATH (brew install uv==$UV_VERSION)" >&2; exit 1; }
UV_VER="$(uv --version | awk '{print $2}')"
[ "$UV_VER" = "$UV_VERSION" ] || { echo "✗ se requiere uv $UV_VERSION (hay $UV_VER)" >&2; exit 1; }
PLATFORM="$(uname -sm)"
[ "$PLATFORM" = "Darwin arm64" ] || {
  echo "✗ los locks de referencia se generan en macOS arm64 (aquí: $PLATFORM)" >&2; exit 1; }
UV_CMD="bash tools/make_locks.sh"   # header determinista de los locks uv (--custom-compile-command)

# --- 5.2 staging aislado + limpieza garantizada (traps) ---------------------------------------
STAGED="$(mktemp -d "${TMPDIR:-/tmp}/vp_locks_staged.XXXXXX")"
cleanup() { rm -rf "$STAGED"; }
trap cleanup EXIT INT TERM HUP

header() {  # $1 = perfil, $2 = python del venv. SIN fecha (determinista). Locks macOS SIN hashes.
  echo "# Lock transitivo del perfil '$1' — tools/make_locks.sh (P0R.4R). NO editar a mano."
  echo "# Python $("$2" --version 2>&1 | cut -d' ' -f2) · plataforma de referencia macOS arm64. Instalar con:"
  echo "#   pip install -r locks/$1.txt && pip install -e . --no-deps   # (macOS base: sin hashes)"
}

freeze_profile() {  # $1 = perfil, $2 = extras (""|"[dev]"|"[dev,model]") -> STAGED/$1.txt
  local env="$STAGED/env-$1"
  "$PY" -m venv "$env"
  "$env/bin/python" -m pip install -q --disable-pip-version-check \
    "pip==$PIP_VERSION" "setuptools==$SETUPTOOLS_VERSION" "wheel==$WHEEL_VERSION"
  "$env/bin/python" -m pip install -q -e ".$2"
  { header "$1" "$env/bin/python"; "$env/bin/python" -m pip freeze --exclude-editable; } > "$STAGED/$1.txt"
  rm -rf "$env"
  echo "  ✓ staged $1.txt ($(grep -vc '^#' "$STAGED/$1.txt") paquetes)"
}

echo "make_locks: perfiles base macOS (venvs FRESCOS, toolchain pineado)…"
freeze_profile runtime ""
freeze_profile dev "[dev]"
freeze_profile model-cpu "[dev,model]"

# --- 5.4 espejos Linux x86_64: uv constreñido por los locks macOS STAGED. --no-annotate evita
#         que la ruta temporal de staging (-c "$STAGED/...") se filtre a las anotaciones del lock.
#         runtime/dev LLEVAN hashes (CI/cron los instalan con `pip --require-hashes -r`); model-cpu
#         va SIN hashes porque CI/cron lo consumen como CONSTRAINTS (`-c`) y pip RECHAZA hashes en
#         constraints files (torch del perfil model viene del índice PyPI, no del de PyTorch).
echo "make_locks: espejos Linux x86_64 (uv pip compile, --no-annotate)…"
uv pip compile -q pyproject.toml --python-version 3.14 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-annotate \
  --custom-compile-command "$UV_CMD" -c "$STAGED/runtime.txt" -o "$STAGED/runtime-linux-x86_64.txt"
echo "  ✓ staged runtime-linux-x86_64.txt ($(grep -c '==' "$STAGED/runtime-linux-x86_64.txt") pins)"
uv pip compile -q pyproject.toml --extra dev --python-version 3.14 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-annotate \
  --custom-compile-command "$UV_CMD" -c "$STAGED/dev.txt" -o "$STAGED/dev-linux-x86_64.txt"
echo "  ✓ staged dev-linux-x86_64.txt ($(grep -c '==' "$STAGED/dev-linux-x86_64.txt") pins)"
uv pip compile -q pyproject.toml --extra dev --extra model --python-version 3.14 \
  --python-platform x86_64-unknown-linux-gnu --no-annotate \
  --custom-compile-command "$UV_CMD" -c "$STAGED/model-cpu.txt" -o "$STAGED/model-cpu-linux-x86_64.txt"
echo "  ✓ staged model-cpu-linux-x86_64.txt ($(grep -c '==' "$STAGED/model-cpu-linux-x86_64.txt") pins, sin hashes: se consume como -c)"

# --- 5.5 perfil DEEP (aislado): 3 cierres HASHEADOS. deep no usa -c constraint (resuelve de
#         deep.in), así que sus anotaciones de índice (URLs públicas) NO llevan ruta temporal.
echo "make_locks: perfil deep (uv pip compile, HASHEADO)…"
uv pip compile -q requirements/deep.in --python "$PY" --generate-hashes \
  --emit-index-url --emit-index-annotation \
  --custom-compile-command "$UV_CMD" -o "$STAGED/deep-macos-arm64.txt"
echo "  ✓ staged deep-macos-arm64.txt ($(grep -c '==' "$STAGED/deep-macos-arm64.txt") pins)"
uv pip compile -q requirements/deep-linux-cpu.in --python-version 3.14 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  --torch-backend cpu --index-strategy unsafe-best-match \
  --emit-index-url --emit-index-annotation \
  --custom-compile-command "$UV_CMD" -o "$STAGED/deep-linux-x86_64-cpu.txt"
echo "  ✓ staged deep-linux-x86_64-cpu.txt ($(grep -c '==' "$STAGED/deep-linux-x86_64-cpu.txt") pins)"
uv pip compile -q requirements/deep-linux-cu126.in --python-version 3.14 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  --torch-backend cu126 --index-strategy unsafe-best-match \
  --emit-index-url --emit-index-annotation \
  --custom-compile-command "$UV_CMD" -o "$STAGED/deep-linux-x86_64-cu126.txt"
echo "  ✓ staged deep-linux-x86_64-cu126.txt ($(grep -c '==' "$STAGED/deep-linux-x86_64-cu126.txt") pins)"

# --- guard de secretos sobre TODO lo staged (líneas `nombre==versión` seguras por construcción;
#     se escanea lo que NO tenga esa forma). `]` primero en la clase POSIX (BSD grep). ---------
if grep -hv -e '^[[:space:]]*#' -e '^[][A-Za-z0-9_.-]\{1,\}==' "$STAGED"/*.txt \
     | grep -qiE "secret|token|password|aws_access|://.*@"; then
  echo "✗ posible secreto en los locks staged — abortando (nada se promovió)" >&2; exit 1
fi

# --- promoción con ROLLBACK TRANSACCIONAL + DETECCIÓN DE MATRIZ PARCIAL. El promotor valida el
#     staging con tools/lock_contracts.py ANTES del primer rename, escribe el manifiesto al final
#     (con Python COMPLETO + plataforma + toolchain) y se autovalida. Invocar en modo -m para que
#     `from tools import lock_contracts` resuelva. -----------------------------------------------
"$PY" -m tools.promote_lockset --staged "$STAGED" \
  --python "$PY_FULL" --platform "$PLATFORM" --pip "$PIP_VERSION" \
  --setuptools "$SETUPTOOLS_VERSION" --wheel "$WHEEL_VERSION" --uv "$UV_VERSION"
echo "make_locks: OK — 9 locks promovidos + manifest (contrato OK). Perfil deep en requirements/deep.in."
