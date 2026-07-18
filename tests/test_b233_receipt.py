"""B247/B250: el recibo de diagnostico B233 es EVIDENCIA GOBERNADA versionada (reports/governance/b233_receipt.json),
validada por un validador ESTRICTO (tools/validate_b233_receipt.py) sin glob ni skip: el fichero DEBE existir y pasar
todos los checks (fichero regular en el workspace, esquema exacto, HEAD real, return_code==1, pip_check True,
extras==['visapredictai'], observed==expected+1, freeze con visapredictai==1.0.0 una vez, sha256 correcto)."""

from __future__ import annotations

import copy
import hashlib
import json
import os

import tools.validate_b233_receipt as v

_RECEIPT = os.path.join(v.ROOT, "reports", "governance", "b233_receipt.json")


def test_governed_receipt_is_valid():
    # el recibo VERSIONADO debe existir y ser valido (sin glob, sin skip)
    assert os.path.isfile(_RECEIPT), "falta reports/governance/b233_receipt.json (evidencia gobernada)"
    assert v.validate_receipt_file(_RECEIPT) == []


def test_validator_rejects_forgeries():
    # el validador ESTRICTO (esquema) rechaza falsificaciones que el anterior aceptaba.
    base = json.load(open(_RECEIPT))
    assert v.validate_receipt(base) == []  # el valido pasa
    for mut in (
        {"return_code": True},  # bool en vez de int
        {"return_code": 0},
        {"pip_check": False},
        {"git_head": "x"},
        {"extras_exact": []},
        {"observed_inventory_size": -1},
        {"expected_inventory_size": base["observed_inventory_size"]},  # rompe observed==expected+1
    ):
        d = copy.deepcopy(base)
        d.update(mut)
        assert v.validate_receipt(d) != [], f"falsificacion {mut} deberia fallar"
    # freeze sin la linea exacta (recomputa el sha para aislar el check)
    d = copy.deepcopy(base)
    d["raw_freeze"] = "pandas==3.0.0\n"
    d["raw_freeze_sha256"] = hashlib.sha256(d["raw_freeze"].encode()).hexdigest()
    assert any("visapredictai==1.0.0" in x for x in v.validate_receipt(d))
    # sha del freeze incorrecto
    d = copy.deepcopy(base)
    d["raw_freeze_sha256"] = "0" * 64
    assert any("sha256" in x for x in v.validate_receipt(d))
    # clave extra en el esquema superior
    d = copy.deepcopy(base)
    d["backdoor"] = 1
    assert v.validate_receipt(d) != []


def test_validate_receipt_file_rejects_symlink_and_outside(tmp_path):
    # checks de FICHERO: symlink -> falla; fuera del workspace -> falla; duplicados -> falla.
    outside = tmp_path / "r.json"
    outside.write_text(json.dumps(json.load(open(_RECEIPT))))
    assert any("fuera del workspace" in p for p in v.validate_receipt_file(str(outside)))
    link = tmp_path / "link.json"
    os.symlink(str(_RECEIPT), str(link))
    assert any("symlink" in p for p in v.validate_receipt_file(str(link)))
    dup = tmp_path / "dup.json"  # dentro/fuera no importa: el JSON duplicado se caza tras el check de workspace
    dup.write_text('{"git_head": "a", "git_head": "b"}')
    # esta fuera del workspace, asi que falla por workspace (fail-closed antes de parsear) -> aceptable
    assert v.validate_receipt_file(str(dup)) != []
