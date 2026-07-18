"""B247: esquema del recibo de diagnostico B233 (build gobernado). El recibo (en scratchpad, NO en el repo) debe ser
auditable: HEAD, plataforma, toolchain, comando, rc, freeze crudo + su sha256, inventarios, extras exactos, pip check."""

from __future__ import annotations

import hashlib
import json

import pytest

_REQUIRED = {
    "git_head": str,
    "platform": dict,
    "toolchain": dict,
    "command": str,
    "return_code": int,
    "raw_freeze": str,
    "raw_freeze_sha256": str,
    "expected_inventory_size": int,
    "observed_inventory_size": int,
    "extras_exact": list,
    "pip_check": object,
    "conclusion": str,
}


def validate_b233_receipt(d: dict) -> list[str]:
    """Devuelve la lista de problemas de esquema (vacia = valido). El sha256 debe corresponder al freeze crudo."""
    probs = []
    for k, t in _REQUIRED.items():
        if k not in d:
            probs.append(f"falta {k}")
        elif t is not object and not isinstance(d[k], t):
            probs.append(f"{k} no es {t.__name__}")
    if "raw_freeze" in d and "raw_freeze_sha256" in d:
        if hashlib.sha256(d["raw_freeze"].encode()).hexdigest() != d["raw_freeze_sha256"]:
            probs.append("raw_freeze_sha256 no corresponde a raw_freeze")
    tc = d.get("toolchain", {})
    if not all(k in tc for k in ("pip", "setuptools", "wheel")):
        probs.append("toolchain incompleto (pip/setuptools/wheel)")
    return probs


def test_b233_receipt_schema_validator():
    ok = {
        "git_head": "a" * 40, "platform": {"system": "Darwin"}, "toolchain": {"pip": "26.1.2", "setuptools": "83.0.0", "wheel": "0.47.0"},  # fmt: skip
        "command": "python -m tools.python_env build --profile dev", "return_code": 1,
        "raw_freeze": "pandas==3.0.0\nvisapredictai==1.0.0\n", "expected_inventory_size": 39,
        "observed_inventory_size": 40, "extras_exact": ["visapredictai"], "pip_check": "ok", "conclusion": "real extra",
    }  # fmt: skip
    ok["raw_freeze_sha256"] = hashlib.sha256(ok["raw_freeze"].encode()).hexdigest()
    assert validate_b233_receipt(ok) == []
    bad = dict(ok, raw_freeze_sha256="0" * 64)
    assert any("no corresponde" in p for p in validate_b233_receipt(bad))
    assert "falta git_head" in validate_b233_receipt({k: v for k, v in ok.items() if k != "git_head"})


def test_b233_scratchpad_receipt_if_present():
    # si el recibo generado esta en scratchpad, valida su esquema (no falla si no esta: es un artefacto de sesion).
    import glob
    import os

    hits = glob.glob(os.path.expanduser("/private/tmp/claude-*/**/scratchpad/b233_receipt.json"), recursive=True)
    hits += glob.glob(os.path.expanduser("/tmp/claude-*/**/scratchpad/b233_receipt.json"), recursive=True)
    if not hits:
        pytest.skip("recibo B233 no presente en scratchpad")
    probs = validate_b233_receipt(json.load(open(hits[0])))
    assert probs == [], f"recibo B233 con esquema invalido: {probs}"
