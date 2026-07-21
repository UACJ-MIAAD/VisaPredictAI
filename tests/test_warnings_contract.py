"""Fase 4 (P0R.5) REDs: contrato de warnings (`tools/check_warnings.py`).

El árbol real está en verde (registro ⇔ filterwarnings de conftest, sin supresión global, expiry vigente). Cada RED
monkeypatchea `problems()` sobre datos sintéticos para violar una regla: primer filtro != error, filtro amplio,
biyección rota (registro sin filtro / filtro sin registro), categoría no-Warning, review expirado, mensaje distinto de
categoría correcta y viceversa (message-prefix vs categoría desalineados). Complementa la verificación viva del guardián."""

from __future__ import annotations

import json
import pathlib

import tools.check_warnings as cw

_GOOD_ENTRY = {
    "id": "x",
    "package": "p",
    "version": "1",
    "category": "UserWarning",
    "message_prefix": "some benign message",
    "origin": "vp_model/tune.py",
    "reason": "r",
    "issue": "i",
    "review": "2999-01-01",
}


def _fake(monkeypatch, tmp_path, registry, filters):
    root = str(tmp_path)
    (tmp_path / "security").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "security" / "warnings_registry.json").write_text(json.dumps(registry))
    body = "FILTERWARNINGS = [\n" + ",\n".join(f"  {json.dumps(f)}" for f in filters) + "\n]\n"
    (tmp_path / "tests" / "conftest.py").write_text(body)
    monkeypatch.setattr(cw, "_ROOT", root)


def _reg(entries):
    return {"schema_version": 1, "note": "n", "warnings": entries}


def test_real_contract_is_green():
    assert cw.problems() == [], cw.problems()


def test_clean_synthetic_passes(tmp_path, monkeypatch):
    _fake(monkeypatch, tmp_path, _reg([_GOOD_ENTRY]), ["error", "ignore:some benign message:UserWarning"])
    assert cw.problems() == [], cw.problems()


def test_first_filter_must_be_error(tmp_path, monkeypatch):
    _fake(monkeypatch, tmp_path, _reg([_GOOD_ENTRY]), ["ignore:some benign message:UserWarning"])
    assert any("PRIMER FILTERWARNINGS debe ser 'error'" in p for p in cw.problems()), cw.problems()


def test_broad_ignore_all_warnings_rejected(tmp_path, monkeypatch):
    _fake(monkeypatch, tmp_path, _reg([_GOOD_ENTRY]), ["error", "ignore:some benign message:UserWarning", "ignore::Warning"])  # fmt: skip
    assert any("amplio PROHIBIDO" in p for p in cw.problems()), cw.problems()


def test_registry_without_conftest_filter_fails(tmp_path, monkeypatch):
    # el registro declara el warning pero conftest no lo ignora
    _fake(monkeypatch, tmp_path, _reg([_GOOD_ENTRY]), ["error"])
    assert any("registro sin filtro en conftest" in p for p in cw.problems()), cw.problems()


def test_conftest_filter_without_registry_fails(tmp_path, monkeypatch):
    # conftest ignora un warning que el registro no declara
    _fake(monkeypatch, tmp_path, _reg([]), ["error", "ignore:ghost message:UserWarning"])
    assert any("sin entrada en el registro" in p for p in cw.problems()), cw.problems()


def test_category_not_warning_name_fails(tmp_path, monkeypatch):
    # último componente NO termina en "Warning" → malformado (la subclase real la impone pytest al colectar)
    bad = {**_GOOD_ENTRY, "category": "builtins.int"}
    _fake(monkeypatch, tmp_path, _reg([bad]), ["error", "ignore:some benign message:builtins.int"])
    assert any("no es un nombre de Warning bien formado" in p for p in cw.problems()), cw.problems()


def test_malformed_category_string_fails(tmp_path, monkeypatch):
    bad = {**_GOOD_ENTRY, "category": "not a category!"}
    _fake(monkeypatch, tmp_path, _reg([bad]), ["error", "ignore:some benign message:not a category!"])
    assert any("no es un nombre de Warning bien formado" in p for p in cw.problems()), cw.problems()


def test_expired_review_fails(tmp_path, monkeypatch):
    bad = {**_GOOD_ENTRY, "review": "2000-01-01"}
    _fake(monkeypatch, tmp_path, _reg([bad]), ["error", "ignore:some benign message:UserWarning"])
    assert any("EXPIRADO" in p for p in cw.problems()), cw.problems()


def test_message_and_category_must_align(tmp_path, monkeypatch):
    # message-prefix correcto pero categoría distinta a la del filtro conftest → biyección rota (el filtro derivado del
    # registro incluye la categoría, así que un cambio de categoría en el registro deja el filtro conftest huérfano).
    entry = {**_GOOD_ENTRY, "category": "FutureWarning"}
    _fake(monkeypatch, tmp_path, _reg([entry]), ["error", "ignore:some benign message:UserWarning"])
    probs = cw.problems()
    assert any("registro sin filtro en conftest" in p for p in probs) and any(
        "sin entrada en el registro" in p for p in probs
    ), probs


def test_missing_entry_keys_fails(tmp_path, monkeypatch):
    incomplete = {"id": "x", "category": "UserWarning"}
    _fake(monkeypatch, tmp_path, _reg([incomplete]), ["error"])
    assert any("claves !=" in p for p in cw.problems()), cw.problems()


def test_duplicate_registry_json_key_fails(tmp_path, monkeypatch):
    root = str(tmp_path)
    (tmp_path / "security").mkdir()
    (tmp_path / "security" / "warnings_registry.json").write_text(
        '{"schema_version": 1, "note": "n", "note": "d", "warnings": []}'
    )
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\nfilterwarnings = ["error"]\n')
    monkeypatch.setattr(cw, "_ROOT", root)
    assert any("duplicad" in p.lower() for p in cw.problems()), cw.problems()


def test_own_code_resource_warnings_are_fixed_not_registered():
    # política: los warnings de código PROPIO se CORRIGEN, no se registran. El registro sólo lleva upstream; ninguna
    # entrada apunta a un fichero de tests/ (sería un warning propio disfrazado).
    reg = json.loads(pathlib.Path("security/warnings_registry.json").read_text())
    for e in reg["warnings"]:
        assert not e["origin"].startswith("tests/"), e
        assert e["package"] in ("scikit-learn", "optuna", "scipy"), e
