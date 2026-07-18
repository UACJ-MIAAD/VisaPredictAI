#!/usr/bin/env python
"""B250: validador ESTRICTO y ejecutable del recibo de diagnóstico B233 (`reports/governance/b233_receipt.json`).

NO usa glob ni `skip`: recibe una ruta EXPLÍCITA y FALLA cerrado ante cualquier anomalía. Exige: fichero regular (no
symlink) DENTRO del workspace; JSON sin claves duplicadas y esquema superior EXACTO; `git_head` 40-hex que EXISTE en el
repo; shas gobernados (python_env.py/profiles/contrato/lockset) `sha256:…`; comando exacto; `return_code` entero real
(no bool) == 1; `pip_check is True`; toolchain/plataforma cerrados; `extras_exact == ["visapredictai"]`;
`observed == expected + 1`; freeze parseable con `visapredictai==1.0.0` EXACTAMENTE una vez; sha256 del freeze correcto.

Uso: `python -m tools.validate_b233_receipt <ruta>` → rc 0 sólo si el recibo es válido.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256_TAG = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOP_KEYS = {
    "schema_version", "purpose", "git_head", "platform", "toolchain", "governed_shas", "command", "recipe",
    "return_code", "error", "raw_freeze", "raw_freeze_sha256", "expected_inventory_size", "observed_inventory_size",
    "extras_exact", "pip_check", "conclusion",
}  # fmt: skip


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave duplicada en el recibo: {k!r}")
        seen[k] = v
    return seen


def _git_object_exists(sha: str) -> bool:
    try:
        return subprocess.run(["git", "-C", ROOT, "cat-file", "-e", sha], capture_output=True).returncode == 0
    except OSError:
        return False


def validate_receipt_file(path: str) -> list[str]:
    """Checks de FICHERO (regular, no-symlink, dentro del workspace, JSON sin duplicados) + delega el esquema a
    `validate_receipt`. Fail-closed en cada paso."""
    real = os.path.realpath(path)
    if os.path.islink(path):
        return [f"{path}: es un symlink (no gobernado)"]
    if not os.path.isfile(path):
        return [f"{path}: no es un fichero regular existente"]
    if os.path.commonpath([real, ROOT]) != ROOT:
        return [f"{path}: fuera del workspace ({real})"]
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.loads(fh.read(), object_pairs_hook=_no_dup_pairs)
    except (OSError, ValueError) as exc:
        return [f"{path}: JSON inválido/duplicado ({exc})"]
    return validate_receipt(d)


def validate_receipt(d: object) -> list[str]:
    """Checks de ESQUEMA/CONTENIDO del recibo (sin tocar disco): esquema exacto, git_head real, shas gobernados,
    return_code int==1, pip_check True, extras==['visapredictai'], observed==expected+1, freeze con la línea exacta y
    su sha256. Devuelve la lista de problemas (vacía = válido)."""
    probs: list[str] = []
    if not isinstance(d, dict) or set(d.keys()) != _TOP_KEYS:
        return [f"esquema superior != {sorted(_TOP_KEYS)} (obtenido {sorted(d) if isinstance(d, dict) else type(d)})"]
    if not (isinstance(d["git_head"], str) and _HEX40.match(d["git_head"])):
        probs.append("git_head no es 40-hex")
    elif not _git_object_exists(d["git_head"]):
        probs.append(f"git_head {d['git_head']} no existe en el repo")
    gs = d["governed_shas"]
    if not isinstance(gs, dict) or set(gs) != {"python_env.py", "profiles", "csv_contract", "lockset"}:
        probs.append("governed_shas incompleto")
    elif not all(isinstance(v, str) and _SHA256_TAG.match(v) for v in gs.values()):
        probs.append("governed_shas con valor no-sha256")
    if not (isinstance(d["command"], str) and "python_env build --profile dev" in d["command"]):
        probs.append("command no es el build gobernado del perfil dev")
    if not (type(d["return_code"]) is int and d["return_code"] == 1):  # bool es subtipo de int → type() exacto
        probs.append("return_code no es un entero == 1 (o es bool)")
    if d["pip_check"] is not True:
        probs.append("pip_check no es True")
    tc = d["toolchain"]
    if not (isinstance(tc, dict) and all(isinstance(tc.get(k), str) for k in ("pip", "setuptools", "wheel"))):
        probs.append("toolchain incompleto")
    pl = d["platform"]
    if not (isinstance(pl, dict) and all(isinstance(pl.get(k), str) for k in ("system", "machine", "python"))):
        probs.append("platform incompleta")
    if d["extras_exact"] != ["visapredictai"]:
        probs.append(f"extras_exact != ['visapredictai'] (obtenido {d['extras_exact']!r})")
    if not (type(d["expected_inventory_size"]) is int and type(d["observed_inventory_size"]) is int and d["observed_inventory_size"] == d["expected_inventory_size"] + 1 and d["expected_inventory_size"] > 0):  # fmt: skip
        probs.append("observed_inventory_size != expected_inventory_size + 1 (o tamaños no plausibles)")
    raw = d["raw_freeze"]
    if not isinstance(raw, str) or [ln for ln in raw.splitlines() if ln.strip() == "visapredictai==1.0.0"] != ["visapredictai==1.0.0"]:  # fmt: skip
        probs.append("raw_freeze no contiene 'visapredictai==1.0.0' exactamente una vez")
    if not (isinstance(d["raw_freeze_sha256"], str) and hashlib.sha256(raw.encode()).hexdigest() == d["raw_freeze_sha256"]):  # fmt: skip
        probs.append("raw_freeze_sha256 no corresponde a raw_freeze")
    return probs


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("uso: python -m tools.validate_b233_receipt <ruta>\n")
        return 2
    probs = validate_receipt_file(argv[1])
    if probs:
        print("✗ recibo B233 inválido:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ recibo B233 válido: {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
