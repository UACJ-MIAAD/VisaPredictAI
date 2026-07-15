#!/usr/bin/env python
"""Contrato gobernado de comandos del call graph (P0R.5, R9.1). `environments/execution_contract.json`
fija, por `command_id`, el perfil MÍNIMO, el modo (`module`|`script`), el target, el working_directory y la
política de argumentos. La interfaz oficial es
`python -m tools.python_env run-command --id <id> -- <args>`.

Validación ESTRICTA (fail-closed): claves superiores exactas, `schema_version` int==1, rechazo de claves
duplicadas; por comando las claves exactas; el perfil DEBE existir en `python_profiles.json`; un perfil con
variantes (deep) EXIGE variante explícita válida y uno sin variantes EXIGE `variant==null`; `mode` ∈
{module, script}; un `module` debe tener nombre Python canónico Y resolver a un fichero del repo; un `script`
debe ser GOBERNADO (relativo a ROOT, versionado, regular, sin symlink) vía `python_env._governed_script`;
`working_directory` ∈ {root}; `args_policy` ∈ {none, passthrough}. No hay modo `code`/stdin en el contrato.

El sha256 de este fichero entra en `env_id`/READY.json/recibos (gobernanza en el descriptor).

    python -m tools.execution_contract            # valida el contrato (exit 1 si algo no cuadra)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from tools import lock_contracts as lc
from tools import python_env as pe

ROOT = lc.ROOT
CONTRACT = ROOT / "environments" / "execution_contract.json"
_TOP_KEYS = {"schema_version", "note", "commands"}
_CMD_KEYS = {"profile", "variant", "mode", "target", "working_directory", "args_policy"}
_MODES = {"module", "script"}
_WORKING_DIRS = {"root"}
_ARGS_POLICIES = {"none", "passthrough"}
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MODULE_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")


def _no_dup(pairs):
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise SystemExit(f"execution_contract: clave duplicada {k!r}")
        d[k] = v
    return d


def _module_resolves(mod: str) -> bool:
    """Un nombre de módulo canónico debe resolver a `<rel>.py` o `<rel>/__init__.py` en el repo."""
    rel = mod.replace(".", "/")
    return (ROOT / f"{rel}.py").is_file() or (ROOT / rel / "__init__.py").is_file()


def load_contract(path: Path = CONTRACT) -> dict:
    doc = json.loads(path.read_text(), object_pairs_hook=_no_dup)
    if set(doc) != _TOP_KEYS:
        raise SystemExit(f"execution_contract: claves superiores {sorted(doc)} != {sorted(_TOP_KEYS)}")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise SystemExit("execution_contract: schema_version no es int == 1")
    if not isinstance(doc["note"], str):
        raise SystemExit("execution_contract: note no-string")
    cmds = doc["commands"]
    if not isinstance(cmds, dict) or not cmds:
        raise SystemExit("execution_contract: `commands` no es objeto no vacío")
    profiles = pe.load_profiles()["profiles"]
    for cid, c in cmds.items():
        if not _ID_RE.fullmatch(cid):
            raise SystemExit(f"execution_contract: command_id no canónico {cid!r}")
        if not isinstance(c, dict) or set(c) != _CMD_KEYS:
            raise SystemExit(f"execution_contract: {cid!r} con claves {sorted(c) if isinstance(c, dict) else c}")
        prof = c["profile"]
        if prof not in profiles:
            raise SystemExit(f"execution_contract: {cid!r} perfil {prof!r} inexistente en python_profiles.json")
        var = c["variant"]
        has_variants = "variants" in profiles[prof]
        if has_variants:
            if var is None or var not in profiles[prof]["variants"]:
                raise SystemExit(f"execution_contract: {cid!r} perfil {prof!r} EXIGE variante válida (dado {var!r})")
        elif var is not None:
            raise SystemExit(f"execution_contract: {cid!r} perfil {prof!r} no admite variante (dado {var!r})")
        if c["mode"] not in _MODES:
            raise SystemExit(f"execution_contract: {cid!r} mode {c['mode']!r} ∉ {sorted(_MODES)}")
        if c["working_directory"] not in _WORKING_DIRS:
            raise SystemExit(f"execution_contract: {cid!r} working_directory {c['working_directory']!r} inválido")
        if c["args_policy"] not in _ARGS_POLICIES:
            raise SystemExit(f"execution_contract: {cid!r} args_policy {c['args_policy']!r} inválido")
        tgt = c["target"]
        if c["mode"] == "module":
            if not (isinstance(tgt, str) and _MODULE_RE.fullmatch(tgt) and _module_resolves(tgt)):
                raise SystemExit(f"execution_contract: {cid!r} módulo {tgt!r} no canónico o inexistente")
        else:
            pe._governed_script(tgt)  # relativo a ROOT, versionado, regular, sin symlink (fail-closed)
    return doc


def command(cid: str, path: Path = CONTRACT) -> dict:
    doc = load_contract(path)
    c = doc["commands"].get(cid)
    if c is None:
        raise SystemExit(f"execution_contract: command_id desconocido {cid!r}")
    return c


def contract_sha256(path: Path = CONTRACT) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    doc = load_contract()
    print(f"✓ execution_contract OK: {len(doc['commands'])} comandos gobernados ({contract_sha256()[:19]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
