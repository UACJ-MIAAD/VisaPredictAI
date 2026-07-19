"""B250/B253/B256/B257: el recibo B233 es un DIAGNÓSTICO HISTÓRICO (schema v3) validado por DERIVACIÓN + PROCEDENCIA +
lectura GOBERNADA fd-bound (tools/validate_b233_receipt.py): capture_head es el commit REAL de la captura (no
reetiquetado), toolchain/platform DERIVADOS de profiles+lock (no arbitrarios), inventario derivado, ficheros gobernados
recalculados == blob@capture_head == checkout actual, lecturas fd-bound sin open(ruta), nunca revienta."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import tools.validate_b233_receipt as v

_RECEIPT = os.path.join(v.ROOT, "reports", "governance", "b233_receipt.json")


def _base() -> dict:
    return json.load(open(_RECEIPT))


def test_governed_receipt_v3_is_valid():
    assert os.path.isfile(_RECEIPT)
    assert v.validate_receipt_file(_RECEIPT) == []
    assert v.validate_receipt(_base()) == []


def test_capture_head_and_imported_must_be_commits():
    # B256: capture_head e imported_into_repository_at deben ser COMMITS reales (un blob no pasa).
    blob = subprocess.run(
        ["git", "-C", v.ROOT, "rev-parse", "HEAD:tools/python_env.py"], capture_output=True, text=True
    ).stdout.strip()
    assert len(blob) == 40
    for key in ("capture_head", "imported_into_repository_at"):
        d = _base()
        d[key] = blob  # un blob, no un commit
        assert v.validate_receipt(d) != [], f"{key}=blob debe fallar (B256)"
        d = _base()
        d[key] = "0" * 40  # 40-hex inexistente
        assert v.validate_receipt(d) != [], f"{key}=inexistente debe fallar"


def test_governed_files_procedence_and_recalculation():
    # B256/B257: los shas gobernados se RECALCULAN contra blob@capture_head; un valor falso (a ceros) falla.
    d = _base()
    first = next(iter(d["governed_files"]))
    d["governed_files"][first] = "sha256:" + "0" * 64
    assert any("procedencia" in p or first in p for p in v.validate_receipt(d))
    # clave gobernada faltante (deben ser EXACTAMENTE las 7, incl. pyproject.toml y .python-version)
    d = _base()
    d["governed_files"].pop(first)
    assert v.validate_receipt(d) != []
    assert "pyproject.toml" in d["governed_files"] or first == "pyproject.toml"


def test_platform_derived_not_arbitrary():
    # B257: system/machine/python de captura se DERIVAN del lock+profiles; valores arbitrarios fallan.
    for mut in (
        {"system": "EvilOS", "machine": "arm64", "python": "3.14.2"},
        {"system": "Darwin", "machine": "quantum", "python": "3.14.2"},
        {"system": "Darwin", "machine": "arm64", "python": "99.0"},
    ):
        d = _base()
        d["capture_platform"] = mut
        assert any("capture_platform" in p for p in v.validate_receipt(d)), f"{mut} debe fallar (B257)"


def test_no_toolchain_field_toolchain_is_derived():
    # B257: el recibo NO lleva su propio toolchain — se DERIVA de python_profiles.json. Falsificar setuptools/wheel
    # en el recibo es imposible porque no hay campo; y el freeze con setuptools distinto rompe el delta derivado.
    assert "toolchain" not in _base()
    tc, err = v._derive_toolchain()
    assert err is None and tc["setuptools"] and tc["wheel"]
    # freeze con setuptools 'evil' -> delta derivado != {visapredictai}
    d = _base()
    d["raw_freeze"] = d["raw_freeze"].replace("setuptools==83.0.0", "setuptools==evil")
    d["capture_freeze_sha256"] = hashlib.sha256(d["raw_freeze"].encode()).hexdigest()
    assert any("observed - expected" in p or "setuptools" in p for p in v.validate_receipt(d))


def test_command_and_schema_forgeries():
    for mut in (
        {"schema_version": 2},
        {"schema_version": True},
        {"capture_kind": "live_governed_build_certification"},  # no es el diagnóstico
        {"return_code": 0},
        {"return_code": True},
        {"capture_command": {"argv": v._EXPECTED_ARGV + ["; rm -rf /"], "environment": v._EXPECTED_ENV}},
        {"capture_command": {"argv": v._EXPECTED_ARGV, "environment": {"X": "1"}}},
        {"capture_command": "python -m tools.python_env build --profile dev"},
        {"extras_exact": []},
        {"extras_exact": ["foo"]},
        {"observed_inventory_size": 999},
    ):
        d = _base()
        d.update(mut)
        assert v.validate_receipt(d) != [], f"forgery {mut} debe fallar"
    d = _base()
    d["backdoor"] = 1
    assert v.validate_receipt(d) != []


def test_never_crashes_on_garbage():
    d = _base()
    d["raw_freeze"] = 7  # int -> NO AttributeError, devuelve problema
    probs = v.validate_receipt(d)
    assert probs and any("raw_freeze" in p for p in probs)
    d = _base()
    d["capture_platform"] = "not a dict"
    assert v.validate_receipt(d) != []


def test_governed_read_rejects_noncanonical(tmp_path):
    outside = tmp_path / "r.json"
    outside.write_text(json.dumps(_base()))
    assert any("versionado" in p for p in v.validate_receipt_file(str(outside)))
    # _governed_bytes rechaza rutas absolutas / con .. (no gobernadas)
    assert v._governed_bytes("/etc/passwd")[0] is None
    assert v._governed_bytes("../escape")[0] is None


def test_governed_reads_are_fd_bound_no_open_by_path():
    # B257: el validador NO usa open() por ruta para ficheros gobernados — sólo _governed_bytes (openat encadenado).
    import inspect

    src = inspect.getsource(v)
    # el único open( permitido es dentro de _no_dup_pairs/json no aplica; comprobamos que no hay open( de governed files
    body = src.split("def _governed_bytes")[0] + src.split("def validate_receipt")[1]
    assert "open(os.path.join(ROOT" not in body, "no debe leerse un fichero gobernado con open() por ruta (B257)"
