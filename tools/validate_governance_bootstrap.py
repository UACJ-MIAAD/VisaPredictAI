#!/usr/bin/env python
"""B284/B290: validador INDEPENDIENTE del bootstrap gobernado de PyYAML (`tools/install_governance_bootstrap.py`).

Re-observa el venv y su recibo SIN confiar en el productor: resuelve el venv de `$GOV_ENV` (o argv[1]); lee y valida el
recibo; RE-lee por `GovernanceSnapshot` el lock y `pyproject.toml` y exige que sus sha256 y el HEAD coincidan con el
recibo; re-ejecuta la verificación aislada (`-I`) en el venv y exige versión/origen/RECORD idénticos al recibo; exige que
el venv sea 0700 bajo un ancestro NO escribible por grupo/otros y que `yaml.__spec__.origin` esté bajo su prefijo. Stdlib
-only (+ `GovernanceSnapshot`). Fail-closed: cualquier desviación termina != 0."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(
        0, _ROOT
    )  # B284: raíz del repo en sys.path para importar `tools.governance_snapshot` en forma script
_PYPROJECT = "pyproject.toml"
_LOCK = "locks/dev-linux-x86_64.txt"
_RECEIPT_NAME = "governance-bootstrap-receipt.json"
_RECEIPT_KEYS = {
    "schema_version",
    "distribution",
    "version",
    "head_commit",
    "lock",
    "lock_sha256",
    "pyproject_sha256",
    "n_hashes",
    "origin",
    "venv_prefix",
    "platform",
    "python_version",
}

_VERIFY_SRC = r"""
import importlib.metadata as m, json, os, stat, sys
d = "pyyaml"
info = {"version": m.version(d)}
files = m.files(d) or []
info["record_present"] = any(str(f).endswith("RECORD") for f in files)
import yaml
origin = yaml.__spec__.origin
info["origin"] = origin
prefix = sys.prefix + os.sep
info["origin_governable"] = bool(
    isinstance(origin, str)
    and origin.startswith(prefix)
    and not os.path.islink(origin)
    and stat.S_ISREG(os.stat(origin).st_mode)
)
print(json.dumps(info))
"""


def _fail(msg: str) -> int:
    print(f"✗ validación del bootstrap fail-closed: {msg}", file=sys.stderr)
    return 1


def _ancestors_not_world_writable(venv: str) -> str | None:
    """El venv es 0700 y cada ancestro hasta la raíz es real (no symlink) y sin escritura de grupo/otros."""
    st = os.lstat(venv)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return f"{venv!r} no es un directorio real"
    if stat.S_IMODE(st.st_mode) != 0o700:
        return f"{venv!r} modo {oct(stat.S_IMODE(st.st_mode))} != 0o700"
    d = os.path.dirname(os.path.realpath(venv))
    while True:
        a = os.lstat(d)
        if a.st_mode & 0o022 and not (a.st_mode & stat.S_ISVTX):  # escribible g/o sin sticky (p.ej. /tmp 01777 sí pasa)
            return f"ancestro {d!r} escribible por grupo/otros ({oct(stat.S_IMODE(a.st_mode))})"
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def main() -> int:
    from tools.governance_snapshot import GovernanceSnapshot, GovernanceSnapshotError

    venv = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GOV_ENV")
    if not venv:
        return _fail("sin venv: pasa la ruta por argv[1] o $GOV_ENV")
    prob = _ancestors_not_world_writable(venv)
    if prob is not None:
        return _fail(prob)
    receipt_path = os.path.join(venv, _RECEIPT_NAME)
    try:
        with open(receipt_path, encoding="utf-8") as fh:
            receipt = json.loads(fh.read())
    except (OSError, ValueError) as exc:
        return _fail(f"recibo ilegible/no-JSON ({exc})")
    if not (isinstance(receipt, dict) and set(receipt) == _RECEIPT_KEYS):
        return _fail(f"recibo con claves != {sorted(_RECEIPT_KEYS)}")
    if receipt["venv_prefix"] != venv:
        return _fail(f"recibo venv_prefix {receipt['venv_prefix']!r} != venv observado {venv!r}")

    # RE-observación gobernada del lock/pyproject/HEAD (una observación sellada, independiente del productor)
    try:
        with GovernanceSnapshot(_ROOT) as snap:
            lock = snap.read(_LOCK, category="source").data
            pyproject = snap.read(_PYPROJECT, category="source").data
            snap.reverify()
        head = GovernanceSnapshot(_ROOT).head_commit()
    except (GovernanceSnapshotError, OSError) as exc:
        return _fail(f"re-observación gobernada falló ({exc})")
    if hashlib.sha256(lock).hexdigest() != receipt["lock_sha256"]:
        return _fail("lock_sha256 del recibo != el lock gobernado actual")
    if hashlib.sha256(pyproject).hexdigest() != receipt["pyproject_sha256"]:
        return _fail("pyproject_sha256 del recibo != el pyproject gobernado actual")
    if head is not None and receipt["head_commit"] != head:
        return _fail(f"head_commit del recibo {receipt['head_commit']!r} != HEAD actual {head!r}")

    # RE-ejecución aislada de la verificación en el venv (no se confía en el recibo para el estado del venv)
    py = os.path.join(venv, "bin", "python")
    try:
        proc = subprocess.run([py, "-I", "-c", _VERIFY_SRC], check=True, capture_output=True, text=True)
        info = json.loads(proc.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError) as exc:
        return _fail(f"re-verificación en el venv falló ({exc})")
    if info.get("version") != receipt["version"]:
        return _fail(f"versión re-observada {info.get('version')!r} != recibo {receipt['version']!r}")
    if info.get("origin") != receipt["origin"]:
        return _fail(f"origen re-observado {info.get('origin')!r} != recibo {receipt['origin']!r}")
    if not info.get("record_present"):
        return _fail("la distribución pyyaml no expone RECORD en la re-observación")
    if not info.get("origin_governable"):
        return _fail(f"yaml.__spec__.origin no gobernable en la re-observación ({info.get('origin')!r})")

    print(f"✓ bootstrap gobernado validado independientemente: pyyaml=={receipt['version']} en {venv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
