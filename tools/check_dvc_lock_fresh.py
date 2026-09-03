#!/usr/bin/env python3
"""Gate pre-push D1: bloquea el push si ``dvc.lock`` está desfasado del pipeline commiteado.

Reproduce EXACTAMENTE el paso «DVC lock matches the committed pipeline (E2)» de
``.github/workflows/ci.yml``: ``dvc status --json`` sobre los cinco stages *git-only*
(``panel bulletins key_facts eda_facts fe_facts``). ``scrape`` y ``database`` quedan fuera a
propósito: dependen de ``data/snapshots`` (privado) y de la caché DVC (S3), que no existen en
un clon limpio. El gate E2 detonó cuatro veces en julio por locks desfasados; este hook lo
frena ANTES de publicar (ver ``docs/DVC.md``).

Contrato (fail-closed):
- Usa el DVC gobernado del proyecto (``ante/bin/dvc`` relativo a la raíz, o la ruta en
  ``$VP_DVC``); si no existe o no es ejecutable, falla.
- Si ``dvc status`` termina con código distinto de cero, falla.
- Solo acepta un objeto JSON vacío (``{}``); JSON inválido, un tipo distinto de objeto o
  cualquier estado no vacío fallan y listan los stages desfasados.
- No toca la red ni modifica nada; el remedio se imprime, nunca se aplica.

Uso: ``python tools/check_dvc_lock_fresh.py`` (sale 0 si el lock está al día, 1 si no).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Mismos cinco targets, en el mismo orden, que el paso E2 de CI (tests/test_dvc_lock_fresh.py
# verifica la igualdad contra el YAML del workflow).
STAGES: tuple[str, ...] = ("panel", "bulletins", "key_facts", "eda_facts", "fe_facts")
DVC_ENV = "VP_DVC"
DEFAULT_DVC = Path("ante/bin/dvc")
REMEDY = "corre `make repro` y commitea dvc.lock junto con las salidas git-only que cambien"

Runner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


def resolve_dvc(root: Path, env: Mapping[str, str]) -> Path | None:
    """DVC gobernado: ``$VP_DVC`` si está definido, si no ``<root>/ante/bin/dvc``. ``None`` si falta."""
    raw = env.get(DVC_ENV)
    candidate = Path(raw) if raw else DEFAULT_DVC
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def default_runner(cmd: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Ejecuta DVC sin red: ``status`` local, analítica desactivada, sin ``check`` (el código se evalúa)."""
    env = dict(os.environ)
    env["DVC_NO_ANALYTICS"] = "1"
    return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, env=env, check=False)


def _describe(status: Mapping[str, object]) -> str:
    lines = []
    for stage in sorted(status):
        detail = json.dumps(status[stage], ensure_ascii=False)
        lines.append(f"  - {stage}: {detail[:200]}")
    return "\n".join(lines)


def evaluate(root: Path, runner: Runner, env: Mapping[str, str]) -> tuple[bool, str]:
    """Devuelve ``(ok, mensaje)``; ``ok`` solo con ``dvc status --json`` == ``{}``."""
    dvc = resolve_dvc(root, env)
    if dvc is None:
        return False, (
            f"✗ dvc-lock-fresh: no se encontró el DVC gobernado ({DEFAULT_DVC} bajo {root} o ${DVC_ENV}); "
            "fail-closed: el push se bloquea hasta tener el venv del proyecto"
        )
    cmd = [str(dvc), "status", "--json", *STAGES]
    result = runner(cmd, root)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return False, f"✗ dvc-lock-fresh: `dvc status` terminó con código {result.returncode}\n  {err[:400]}"
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return False, f"✗ dvc-lock-fresh: la salida de `dvc status --json` no es JSON válido ({exc})"
    if not isinstance(status, dict):
        return False, f"✗ dvc-lock-fresh: la salida de `dvc status --json` no es un objeto ({type(status).__name__})"
    if status:
        return False, (
            f"✗ dvc.lock desfasado del pipeline commiteado — stages: {', '.join(sorted(status))}\n"
            f"{_describe(status)}\n  Remedio: {REMEDY}"
        )
    return True, f"✓ dvc.lock al día ({' '.join(STAGES)})"


def main(argv: Sequence[str] | None = None) -> int:
    ok, message = evaluate(ROOT, default_runner, os.environ)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
