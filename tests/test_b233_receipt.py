"""B247/B250/B253: el recibo de diagnostico B233 es EVIDENCIA GOBERNADA versionada
(reports/governance/b233_receipt.json), validada por un validador ESTRICTO y DERIVADO (tools/validate_b233_receipt.py):
lectura gobernada (openat encadenado + snapshot), esquema EXACTO y tipado, git_head ES un commit, shas gobernados
RECALCULADOS == actual == blob@git_head, inventario DERIVADO (observed(raw_freeze) - expected(dev.txt ∪ toolchain) ==
{visapredictai: 1.0.0}). NUNCA revienta ante tipos basura."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess

import tools.validate_b233_receipt as v

_RECEIPT = os.path.join(v.ROOT, "reports", "governance", "b233_receipt.json")


def _base() -> dict:
    return json.load(open(_RECEIPT))


def test_governed_receipt_is_valid():
    assert os.path.isfile(_RECEIPT), "falta reports/governance/b233_receipt.json (evidencia gobernada)"
    assert v.validate_receipt_file(_RECEIPT) == []
    assert v.validate_receipt(_base()) == []


def test_validator_rejects_scalar_and_schema_forgeries():
    base = _base()
    for mut in (
        {"schema_version": True},  # bool no cuenta como int
        {"schema_version": 1},
        {"schema_version": "2"},
        {"return_code": True},
        {"return_code": 0},
        {"pip_check": False},
        {"pip_check": 1},
        {"git_head": "x"},  # no 40-hex
        {"git_head": "0" * 40},  # 40-hex pero no existe/no es commit
        {"extras_exact": []},
        {"extras_exact": ["foo"]},
        {"purpose": ""},
        {"purpose": 5},
        {"error": None},
        {"conclusion": 0},
    ):
        d = copy.deepcopy(base)
        d.update(mut)
        assert v.validate_receipt(d) != [], f"falsificacion {mut} deberia fallar"
    # clave extra en el esquema superior
    d = copy.deepcopy(base)
    d["backdoor"] = 1
    assert v.validate_receipt(d) != []


def test_validator_rejects_blob_as_head():
    # B253: un objeto git que existe pero NO es un commit (un blob) no debe pasar como git_head.
    blob = subprocess.run(
        ["git", "-C", v.ROOT, "rev-parse", "HEAD:tools/python_env.py"], capture_output=True, text=True
    ).stdout.strip()
    assert len(blob) == 40, "no se pudo obtener un sha de blob para el test"
    d = _base()
    d["git_head"] = blob
    assert any("commit" in p for p in v.validate_receipt(d)), "un blob como git_head debe fallar (B253)"


def test_validator_recalculates_governed_hashes():
    # B253: un sha gobernado a ceros (que el validador viejo aceptaba por formato) debe fallar al recalcular.
    d = _base()
    first = next(iter(d["governed_files"]))
    d["governed_files"][first] = "sha256:" + "0" * 64
    assert any(first in p for p in v.validate_receipt(d)), "sha gobernado a ceros debe fallar (B253)"
    # clave gobernada faltante/extra
    d = _base()
    d["governed_files"].pop(first)
    assert v.validate_receipt(d) != []


def test_validator_rejects_command_and_nested_extra_keys():
    # B253: comando inyectado / estructura de comando mal / claves extra en platform/toolchain/command.
    for mut in (
        {
            "command": {
                "argv": ["python", "-m", "tools.python_env", "build", "--profile", "dev", "; rm -rf /"],
                "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
            }
        },  # fmt: skip
        {
            "command": {
                "argv": ["python", "-m", "tools.python_env", "build", "--profile", "dev"],
                "environment": {"X": "1"},
            }
        },  # fmt: skip
        {"command": "python -m tools.python_env build --profile dev"},  # string en vez de estructura
        {"command": {"argv": ["python"], "environment": {}, "shell": True}},  # clave extra
        {"platform": {"system": "Darwin", "machine": "arm64", "python": "3.14.2", "extra": "x"}},
        {"toolchain": {"pip": "26.1.2", "setuptools": "83.0.0"}},  # falta wheel
        {"toolchain": {"pip": "26.1.2", "setuptools": "83.0.0", "wheel": "0.47.0", "extra": "x"}},
    ):
        d = _base()
        d.update(mut)
        assert v.validate_receipt(d) != [], f"falsificacion {mut} deberia fallar"


def test_validator_derives_inventory_and_never_crashes():
    base = _base()
    # freeze basura (int) -> NO revienta (AttributeError), devuelve problema
    d = copy.deepcopy(base)
    d["raw_freeze"] = 7
    probs = v.validate_receipt(d)
    assert probs and any("raw_freeze" in p for p in probs), "raw_freeze int debe dar problema, no traceback"
    # freeze que rompe el delta derivado (recomputa el sha para aislar)
    d = copy.deepcopy(base)
    d["raw_freeze"] = "pandas==3.0.0\n"
    d["raw_freeze_sha256"] = hashlib.sha256(d["raw_freeze"].encode()).hexdigest()
    assert any("observed - expected" in p or "ausentes" in p for p in v.validate_receipt(d))
    # tamaños no derivados
    d = copy.deepcopy(base)
    d["observed_inventory_size"] = base["observed_inventory_size"] + 5
    assert any("observed_inventory_size" in p for p in v.validate_receipt(d))
    # sha del freeze incorrecto
    d = copy.deepcopy(base)
    d["raw_freeze_sha256"] = "0" * 64
    assert any("sha256" in p for p in v.validate_receipt(d))


def test_validate_receipt_file_rejects_noncanonical(tmp_path):
    # B253: la lectura es GOBERNADA sobre la ruta canonica; cualquier otra ruta se rechaza.
    outside = tmp_path / "r.json"
    outside.write_text(json.dumps(_base()))
    assert any("versionado" in p for p in v.validate_receipt_file(str(outside)))
    # un symlink hacia OTRO fichero (realpath != canonico) se rechaza
    other = tmp_path / "other.json"
    other.write_text("{}")
    link = tmp_path / "link.json"
    os.symlink(str(other), str(link))
    assert v.validate_receipt_file(str(link)) != []
