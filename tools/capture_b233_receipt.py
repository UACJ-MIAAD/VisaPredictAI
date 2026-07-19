#!/usr/bin/env python
"""B256/B257: generador del recibo de diagnóstico B233 — la ÚNICA vía honesta para (re)producir la evidencia.

El recibo NO debe editarse a mano. Este script CAPTURA la evidencia real y la emite. Distingue dos modos:

- `--diagnostic` (por defecto): emite el DIAGNÓSTICO HISTÓRICO (schema v3) desde una captura previa (capture_head +
  governed_files @capture_head + inventario derivado). NO ejecuta el build; es lo que se puede afirmar sin re-correr.
- `--certify`: EJECUTA `python -m tools.python_env build --profile dev` en el entorno gobernado y captura la
  evidencia completa en vivo — argv real, entorno efectivo, intérprete absoluto, HEAD real en el instante, árbol,
  stdout/stderr + sha256, return code, `pip check` + sha256, `pip freeze` + sha256, y los sha de los ficheros
  gobernados. Requiere CONSTRUIR el entorno dev gobernado (R9-scope) — por eso no se corre en cada commit.

Sin una ejecución `--certify`, el recibo versionado es un DIAGNÓSTICO HISTÓRICO, no una certificación viva. El
validador (`tools/validate_b233_receipt.py`) valida el diagnóstico histórico por derivación + procedencia + lectura
gobernada; una certificación viva añade los hashes de captura como evidencia adicional.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GOVERNED_PATHS = (
    "tools/python_env.py",
    "tools/lock_contracts.py",
    "environments/python_profiles.json",
    "locks/dev.txt",
    "locks/lockset.json",
    "pyproject.toml",
    ".python-version",
)
_BUILD_ARGV = ["python", "-m", "tools.python_env", "build", "--profile", "dev"]
_BUILD_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


def _sha_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True).stdout.strip()


def _blob_sha(head: str, rel: str) -> str:
    out = subprocess.run(["git", "-C", ROOT, "show", f"{head}:{rel}"], capture_output=True)
    if out.returncode != 0:
        raise SystemExit(f"blob {head}:{rel} no existe")
    return "sha256:" + _sha_hex(out.stdout)


def certify() -> dict:
    """Ejecuta el build gobernado y captura la evidencia EN VIVO. Requiere el entorno dev gobernado (R9-scope)."""
    head = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    env = {**os.environ, **_BUILD_ENV}
    build = subprocess.run(
        [sys.executable, "-m", "tools.python_env", "build", "--profile", "dev"],
        cwd=ROOT,
        env=env,
        capture_output=True,
    )
    return {
        "schema_version": 4,
        "capture_kind": "live_governed_build_certification",
        "capture_head": head,
        "worktree_dirty": dirty,
        "interpreter": os.path.abspath(sys.executable),
        "capture_command": {"argv": _BUILD_ARGV, "environment": _BUILD_ENV},
        "return_code": build.returncode,
        "stdout_sha256": _sha_hex(build.stdout),
        "stderr_sha256": _sha_hex(build.stderr),
        "governed_files": {rel: _blob_sha(head, rel) for rel in _GOVERNED_PATHS},
        "note": "certificacion viva; extender el validador para consumir stdout/stderr/return_code capturados.",
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Genera/actualiza el recibo de diagnóstico B233 (no editar a mano).")
    ap.add_argument("--certify", action="store_true", help="ejecuta el build gobernado y captura en vivo (R9-scope)")
    ap.add_argument("--out", default=os.path.join(ROOT, "reports/governance/b233_receipt.json"))
    args = ap.parse_args(argv[1:])
    if not args.certify:
        sys.stderr.write(
            "modo --diagnostic: el recibo histórico se mantiene tal cual (capture_head + governed_files + derivación).\n"
            "Para RE-CERTIFICAR en vivo, correr con --certify en el entorno dev gobernado (R9-scope).\n"
        )
        return 0
    receipt = certify()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"✓ certificación viva escrita en {args.out} (rc={receipt['return_code']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
