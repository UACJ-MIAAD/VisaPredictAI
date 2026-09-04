"""D2-C: los warnings de la suite son un contrato verificable (tools/check_warnings.py).

Cubre el contrato completo del verificador: esquema y tipos cerrados, claves JSON duplicadas,
ids y filtros duplicados, filtros amplios, expiración, pines exactos y biyección
registro <-> FILTERWARNINGS. Todo con copias en `tmp_path`: el registro real nunca se toca.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
from pathlib import Path

import pytest

from tools import check_warnings as cw

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.date(2026, 9, 4)


def _sandbox(tmp_path: Path) -> Path:
    """Copia mínima del repo: registro, conftest y las fuentes de pines."""
    (tmp_path / "security").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "locks").mkdir()
    shutil.copy2(ROOT / "security" / "warnings_registry.json", tmp_path / "security" / "warnings_registry.json")
    shutil.copy2(ROOT / "tests" / "conftest.py", tmp_path / "tests" / "conftest.py")
    shutil.copy2(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    for lock in ("model-cpu.txt", "deep-macos-arm64.txt"):
        shutil.copy2(ROOT / "locks" / lock, tmp_path / "locks" / lock)
    _plant_broad_suppressions(tmp_path, _registry(tmp_path)["deferred_debt"]["sites"])
    return tmp_path


def _plant_broad_suppressions(root: Path, sites: list[str]) -> None:
    """Siembra un árbol mínimo de productores cuyas supresiones amplias caen en las líneas
    declaradas: el verificador deriva la deuda del CÓDIGO, así que el sandbox debe tenerlo."""
    by_file: dict[str, list[int]] = {}
    for site in sites:
        rel, _, line_no = site.rpartition(":")
        by_file.setdefault(rel, []).append(int(line_no))
    for rel, numbers in by_file.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        body = ["import warnings"] + [""] * (max(numbers) - 1)
        for number in numbers:  # un archivo puede tener VARIAS supresiones (p. ej. 120, 136 y 258)
            body[number - 1] = 'warnings.simplefilter("ignore")'
        path.write_text("\n".join(body) + "\n", encoding="utf-8")


def _registry(root: Path) -> dict:
    return json.loads((root / "security" / "warnings_registry.json").read_text(encoding="utf-8"))


def _write(root: Path, data: dict) -> None:
    (root / "security" / "warnings_registry.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _set_filters(root: Path, filters: list[str]) -> None:
    text = (root / "tests" / "conftest.py").read_text(encoding="utf-8")
    start = text.index("FILTERWARNINGS = [")
    end = text.index("]", start) + 1
    body = "FILTERWARNINGS = [\n" + "".join(f"    {json.dumps(f)},\n" for f in filters) + "]"
    (root / "tests" / "conftest.py").write_text(text[:start] + body + text[end:], encoding="utf-8")


# ----------------------------------------------------------------- estado vigente
def test_real_repository_satisfies_the_contract() -> None:
    """7 excepciones: las 4 acreditadas en R9, las 2 de arranque de statsmodels y la de
    convergencia del MLE, que afloró justo al activar `error` global (regla 10: upstream,
    reproducible y estrecha; corregirla exigiría tocar vp_model/, fuera del alcance)."""
    entries = cw.verify(ROOT, TODAY)
    assert len(entries) == 7
    assert {e["package"] for e in entries} == {"scikit-learn", "optuna", "scipy", "statsmodels"}
    assert sum(e["package"] == "statsmodels" for e in entries) == 3


def test_error_is_the_global_default_and_no_broad_suppression_exists() -> None:
    filters = cw.conftest_filters(ROOT / "tests" / "conftest.py")
    assert filters[0] == "error"
    assert all(f.startswith("ignore:") and not f.startswith("ignore::") for f in filters[1:])
    assert len(filters) == 8  # error + 7 excepciones


def test_the_two_statsmodels_exceptions_are_registered() -> None:
    ids = {e["id"] for e in cw.verify(ROOT, TODAY)}
    assert {"statsmodels-nonstationary-ar-start", "statsmodels-noninvertible-ma-start"} <= ids


def test_registry_documents_the_deferred_producer_debt() -> None:
    """D2-C gobierna la suite y su CI; NO afirma gobernar los warnings de los productores."""
    data = _registry(ROOT)
    debt = data["deferred_debt"]
    assert debt["count"] == 9 and len(debt["sites"]) == 9
    assert "suite" in data["scope"].lower() and "productores" in data["scope"].lower()


# --------------------------------------------------------------- fallos del contrato
def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    (root / "security" / "warnings_registry.json").write_text(
        '{"schema_version": 2, "warnings": [], "warnings": []}', encoding="utf-8"
    )
    with pytest.raises(cw.ContractError, match="clave JSON duplicada"):
        cw.verify(root, TODAY)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda d: d.update(schema_version=1), "schema_version"),
        (lambda d: d.update(warnings=[]), "lista no vacía"),
        (lambda d: d.update(extra_key="x"), r"sobran \['extra_key'\]"),
    ],
)
def test_schema_violations_are_rejected(tmp_path: Path, mutate, match: str) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    mutate(data)
    _write(root, data)
    with pytest.raises(cw.ContractError, match=match):
        cw.verify(root, TODAY)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("review", "31-01-2027", "YYYY-MM-DD"),
        ("id", "MalFormado", "kebab-case"),
        ("category", "Warning", "amplia"),
        ("category", "algo", "amplia"),
        ("message_prefix", "corto", "demasiado corto"),
        ("message_prefix", "prefijo con dos puntos: aqui rompe el filtro", "no puede contener"),
        ("version", "9.9.9", "pin exacto"),
        ("pin_source", "otro/archivo.txt", "fuera de pyproject"),
    ],
)
def test_field_violations_are_rejected(tmp_path: Path, field: str, value: str, match: str) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    data["warnings"][0][field] = value
    _write(root, data)
    with pytest.raises(cw.ContractError, match=match):
        cw.verify(root, TODAY)


def test_wrong_types_are_rejected(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    data["warnings"][0]["version"] = 1.9
    _write(root, data)
    with pytest.raises(cw.ContractError, match="cadena no vacía"):
        cw.verify(root, TODAY)


def test_missing_and_unknown_entry_fields_are_rejected(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    del data["warnings"][0]["reason"]
    data["warnings"][0]["nuevo"] = "x"
    _write(root, data)
    with pytest.raises(cw.ContractError, match="faltan.*sobran"):
        cw.verify(root, TODAY)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    clone = copy.deepcopy(data["warnings"][0])
    clone["message_prefix"] = "Otro mensaje distinto y suficientemente largo"
    data["warnings"].append(clone)
    _write(root, data)
    with pytest.raises(cw.ContractError, match="ids duplicados"):
        cw.verify(root, TODAY)


def test_duplicate_filters_are_rejected(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    clone = copy.deepcopy(data["warnings"][0])
    clone["id"] = "otro-id-distinto"
    data["warnings"].append(clone)
    _write(root, data)
    with pytest.raises(cw.ContractError, match="MISMO filtro"):
        cw.verify(root, TODAY)


def test_expired_entries_are_rejected(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    with pytest.raises(cw.ContractError, match="EXPIRADAS"):
        cw.verify(root, dt.date(2027, 2, 1))


def test_pin_mismatch_between_registry_and_pyproject_is_rejected(tmp_path: Path) -> None:
    """Una versión de lock que contradice el pin de pyproject se rechaza."""
    root = _sandbox(tmp_path)
    data = _registry(root)
    entry = next(e for e in data["warnings"] if e["package"] == "scikit-learn")
    entry["pin_source"] = "pyproject.toml"
    _write(root, data)
    with pytest.raises(cw.ContractError, match="pin exacto"):
        cw.verify(root, TODAY)


# --------------------------------------------------------------------- biyección
def test_missing_filter_breaks_the_bijection(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    filters = cw.conftest_filters(root / "tests" / "conftest.py")
    _set_filters(root, filters[:-1])
    with pytest.raises(cw.ContractError, match="biyección rota"):
        cw.verify(root, TODAY)


def test_extra_filter_breaks_the_bijection(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    filters = cw.conftest_filters(root / "tests" / "conftest.py")
    _set_filters(root, [*filters, "ignore:Un warning que nadie registro jamas:UserWarning"])
    with pytest.raises(cw.ContractError, match="biyección rota"):
        cw.verify(root, TODAY)


@pytest.mark.parametrize("broad", ["ignore", "ignore::Warning", "ignore::DeprecationWarning"])
def test_broad_filters_are_rejected(tmp_path: Path, broad: str) -> None:
    root = _sandbox(tmp_path)
    filters = cw.conftest_filters(root / "tests" / "conftest.py")
    _set_filters(root, [*filters, broad])
    with pytest.raises(cw.ContractError, match="amplio|global prohibida"):
        cw.verify(root, TODAY)


def test_error_must_be_first(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    filters = cw.conftest_filters(root / "tests" / "conftest.py")
    _set_filters(root, filters[1:])
    with pytest.raises(cw.ContractError, match="empezar por 'error'"):
        cw.verify(root, TODAY)


def test_duplicate_filter_lines_are_rejected(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    filters = cw.conftest_filters(root / "tests" / "conftest.py")
    _set_filters(root, [*filters, filters[-1]])
    with pytest.raises(cw.ContractError, match="filtros duplicados"):
        cw.verify(root, TODAY)


def test_missing_registry_or_conftest_fails_closed(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    (root / "security" / "warnings_registry.json").unlink()
    with pytest.raises(cw.ContractError, match="ilegible"):
        cw.verify(root, TODAY)


def test_filterwarnings_must_be_a_literal_list(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    text = (root / "tests" / "conftest.py").read_text(encoding="utf-8")
    start = text.index("FILTERWARNINGS = [")
    end = text.index("]", start) + 1
    (root / "tests" / "conftest.py").write_text(
        text[:start] + "FILTERWARNINGS = list(_x)" + text[end:], encoding="utf-8"
    )
    with pytest.raises(cw.ContractError, match="lista literal|no se encontró"):
        cw.verify(root, TODAY)


def test_cli_returns_zero_on_the_real_repository() -> None:
    assert cw.main() == 0


# ------------------------------------------------- M11-R1: endurecimiento del contrato
def test_missing_top_level_key_is_rejected(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    del data["scope"]
    _write(root, data)
    with pytest.raises(cw.ContractError, match=r"faltan \['scope'\]"):
        cw.verify(root, TODAY)


@pytest.mark.parametrize(
    "field,value",
    [("note", 5), ("note", ""), ("scope", []), ("scope", "   "), ("deferred_debt", "texto"), ("deferred_debt", None)],
)
def test_top_level_types_are_closed(tmp_path: Path, field: str, value) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    data[field] = value
    _write(root, data)
    with pytest.raises(cw.ContractError, match="cadena no vacía|debe ser un objeto"):
        cw.verify(root, TODAY)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda d: d["deferred_debt"].update(count=8), "count 8"),
        (lambda d: d["deferred_debt"].update(count=True), "entero"),
        (
            lambda d: d["deferred_debt"].update(sites=d["deferred_debt"]["sites"] + [d["deferred_debt"]["sites"][0]]),
            "duplicados",
        ),
        (lambda d: d["deferred_debt"].pop("note"), "faltan"),
        (lambda d: d["deferred_debt"].update(extra="x"), "sobran"),
        (lambda d: d["deferred_debt"].update(sites=["vp_model/eda.py:79"], count=1), "desalineada"),
    ],
)
def test_deferred_debt_is_validated_against_the_code(tmp_path: Path, mutate, match: str) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    mutate(data)
    _write(root, data)
    with pytest.raises(cw.ContractError, match=match):
        cw.verify(root, TODAY)


def test_debt_detected_from_code_matches_the_declared_sites() -> None:
    """La deuda no se cree por lo escrito: se lee del código de los productores."""
    live = cw.detect_broad_suppressions(ROOT)
    declared = _registry(ROOT)["deferred_debt"]["sites"]
    assert sorted(live) == sorted(declared) and len(live) == 9
    assert not any(s.startswith("tools/check_warnings.py") for s in live), "el detector no se cuenta a sí mismo"
    assert not any("walkforward" in s for s in live), 'simplefilter("always") no suprime nada'


def test_message_prefix_is_literal_not_a_regex(tmp_path: Path) -> None:
    """RED: un prefijo con `.*` no puede convertirse en comodín."""
    import warnings as _warnings

    entry = {"message_prefix": "Mensaje con comodin .* peligroso", "category": "UserWarning"}
    filt = cw.filter_expression(entry)
    assert r"\.\*" in filt, f"el prefijo debe viajar escapado: {filt}"
    _, message, category = filt.split(":", 2)
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.resetwarnings()
        _warnings.filterwarnings("ignore", message=message, category=UserWarning)
        _warnings.simplefilter("always", append=True)
        _warnings.warn("Mensaje con comodin XYZ peligroso", UserWarning, stacklevel=2)  # comodín lo tragaría
        _warnings.warn("Mensaje con comodin .* peligroso", UserWarning, stacklevel=2)  # este sí calla
    assert len(caught) == 1 and "XYZ" in str(caught[0].message)
    assert category == "UserWarning"


@pytest.mark.parametrize("suffix", ["1.9.0.post1", "1.9.0+local", "1.9.01", "1.9.0rc1"])
def test_partial_version_matches_are_rejected(tmp_path: Path, suffix: str) -> None:
    """Un pin `scikit-learn==1.9.0` NO acredita una versión declarada con sufijo."""
    root = _sandbox(tmp_path)
    data = _registry(root)
    entry = next(e for e in data["warnings"] if e["package"] == "scikit-learn")
    entry["version"] = suffix
    _write(root, data)
    with pytest.raises(cw.ContractError, match="pin exacto"):
        cw.verify(root, TODAY)


def test_pin_terminator_accepts_the_real_lock_and_pyproject_forms() -> None:
    """El terminador no puede ser tan estricto que rechace las formas reales."""
    entries = cw.verify(ROOT, TODAY)
    assert {e["pin_source"] for e in entries} == {"pyproject.toml", "locks/model-cpu.txt", "locks/deep-macos-arm64.txt"}


@pytest.mark.parametrize(
    "source",
    [
        "/etc/passwd",
        "locks/../pyproject.toml",
        "../locks/model-cpu.txt",
        "locks/sub/dir.txt",
        "locks/",
        "C:\\locks\\x.txt",
    ],
)
def test_pin_source_traversal_is_rejected(tmp_path: Path, source: str) -> None:
    root = _sandbox(tmp_path)
    data = _registry(root)
    data["warnings"][0]["pin_source"] = source
    _write(root, data)
    with pytest.raises(cw.ContractError, match="ruta absoluta|traversal|fuera de pyproject"):
        cw.verify(root, TODAY)
