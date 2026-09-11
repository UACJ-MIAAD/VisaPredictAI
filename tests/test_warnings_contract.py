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
    """8 excepciones: las 4 acreditadas en R9 y 4 de statsmodels (arranque AR y MA,
    convergencia del MLE y convergencia de Holt-Winters). La última entró tras el CI rojo
    `33844476552`: el mismo árbol pasaba en la PR y fallaba en el push porque la convergencia
    numérica depende del runner."""
    entries = cw.verify(ROOT, TODAY)
    assert len(entries) == 8
    assert {e["package"] for e in entries} == {"scikit-learn", "optuna", "scipy", "statsmodels"}
    assert sum(e["package"] == "statsmodels" for e in entries) == 4


def test_error_is_the_global_default_and_no_broad_suppression_exists() -> None:
    filters = cw.conftest_filters(ROOT / "tests" / "conftest.py")
    assert filters[0] == "error"
    assert all(f.startswith("ignore:") and not f.startswith("ignore::") for f in filters[1:])
    assert len(filters) == 9  # error + 8 excepciones


def test_the_statsmodels_exceptions_are_registered() -> None:
    ids = {e["id"] for e in cw.verify(ROOT, TODAY)}
    assert {
        "statsmodels-nonstationary-ar-start",
        "statsmodels-noninvertible-ma-start",
        "statsmodels-mle-convergence",
        "statsmodels-holtwinters-convergence",
    } <= ids


def test_registry_documents_the_deferred_producer_debt() -> None:
    """M72 cerró la deuda: cero supresiones amplias, y el cero se deriva del código."""
    data = _registry(ROOT)
    debt = data["deferred_debt"]
    assert debt["count"] == 0 and debt["sites"] == []
    assert cw.detect_broad_suppressions(ROOT) == []
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
            # la deuda vigente está vacía: el duplicado se declara aquí, no se toma de ella
            lambda d: d["deferred_debt"].update(sites=["vp_model/eda.py:79"] * 2, count=2),
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
    assert sorted(live) == sorted(declared) and live == []
    assert not any("walkforward" in s for s in live), 'simplefilter("always") no suprime nada'


def test_the_detector_still_finds_a_planted_suppression(tmp_path: Path) -> None:
    """Un cero sólo vale si el detector sigue cazando: se siembra y debe aparecer, con su línea."""
    (tmp_path / "vp_model").mkdir()
    (tmp_path / "vp_model" / "x.py").write_text(
        "import warnings\n\n\nwarnings.simplefilter('ignore')\n", encoding="utf-8"
    )
    assert cw.detect_broad_suppressions(tmp_path) == ["vp_model/x.py:4"]


def test_the_detector_reads_the_syntax_tree_not_the_text(tmp_path: Path) -> None:
    """Nombrar el patrón en un docstring o en un comentario NO es suprimir.

    Es el defecto que cazó M72 en su primera corrida: el inventario por texto acusaba a
    `vp_model/noise.py` por su propia documentación, y obligaba a que este módulo se eximiera a
    sí mismo. Leyendo el AST, ninguna exención hace falta.
    """
    (tmp_path / "vp_model").mkdir()
    (tmp_path / "vp_model" / "doc.py").write_text(
        '''"""Explica que warnings.filterwarnings("ignore") está prohibido."""\n'''
        '# tampoco cuenta en un comentario: warnings.simplefilter("ignore")\n'
        "PATRON = 'warnings.filterwarnings(\"ignore\")'\n",
        encoding="utf-8",
    )
    assert cw.detect_broad_suppressions(tmp_path) == []
    # …y el propio verificador, que cita el patrón en su documentación, no se acusa.
    assert not any(s.startswith("tools/check_warnings.py") for s in cw.detect_broad_suppressions(ROOT))


def test_the_detector_catches_a_call_split_across_lines(tmp_path: Path) -> None:
    """RED del detector viejo: partida en varias líneas, la regex no la veía y el AST sí."""
    (tmp_path / "experiments").mkdir()
    (tmp_path / "experiments" / "y.py").write_text(
        "import warnings\n\nwarnings.filterwarnings(\n    'ignore',\n)\n", encoding="utf-8"
    )
    assert cw.detect_broad_suppressions(tmp_path) == ["experiments/y.py:3"]


@pytest.mark.parametrize(
    "llamada,amplia",
    [
        ("warnings.filterwarnings('ignore', category=Warning)", True),  # categoría raíz: lo traga todo
        ("warnings.filterwarnings('ignore', message='')", True),  # mensaje vacío: casa con todo
        ("warnings.filterwarnings('ignore', **opciones)", True),  # opaco: no se puede acreditar
        ("warnings.filterwarnings('ignore', message='boom', category=Warning)", False),
        ("warnings.filterwarnings('ignore', category=DeprecationWarning)", False),
        ("warnings.simplefilter('ignore', DeprecationWarning)", False),
        ("warnings.simplefilter('always')", False),  # no suprime nada
        ("warnings.filterwarnings('error')", False),
    ],
)
def test_the_detector_separates_broad_from_narrow(tmp_path: Path, llamada: str, amplia: bool) -> None:
    """La frontera se prueba por casos, no se afirma: acotar por mensaje O por categoría basta."""
    (tmp_path / "vp_data").mkdir()
    (tmp_path / "vp_data" / "z.py").write_text(f"import warnings\n\nopciones = {{}}\n{llamada}\n", encoding="utf-8")
    encontrado = cw.detect_broad_suppressions(tmp_path)
    assert bool(encontrado) is amplia, f"{llamada!r} clasificada al revés: {encontrado}"


@pytest.mark.parametrize(
    "llamada,amplia,motivo",
    [
        # ── H12: huecos que el detector de M72-R2 dejaba pasar ──────────────────────────────
        ("warnings.filterwarnings(action='ignore')", True, "`action` por keyword"),
        ("warnings.simplefilter('ignore', Warning)", True, "2.º posicional de simplefilter es la CATEGORÍA"),
        ("warnings.filterwarnings('ignore', '.*')", True, "patrón de mensaje universal"),
        ("warnings.filterwarnings('ignore', '^')", True, "patrón que casa con todo"),
        ("warnings.filterwarnings('ignore', message='.*')", True, "universal por keyword"),
        ("warnings.catch_warnings(action='ignore')", True, "catch_warnings(action=…) de 3.11+"),
        ("warnings.filterwarnings('ignore', *opciones)", True, "posicionales opacos"),
        ("warnings.filterwarnings('ignore', category=builtins.Warning)", True, "categoría raíz por atributo"),
        # ── y lo que SIGUE siendo estrecho, para que el endurecimiento no se pase de frenada ──
        ("warnings.simplefilter('ignore', DeprecationWarning)", False, "categoría concreta en simplefilter"),
        ("warnings.filterwarnings('ignore', 'boom concreto')", False, "mensaje literal como 2.º posicional"),
        ("warnings.filterwarnings('ignore', message='boom', category=Warning)", False, "mensaje acota"),
        ("warnings.catch_warnings(action='ignore', category=DeprecationWarning)", False, "categoría acota"),
        ("warnings.catch_warnings()", False, "sin acción no filtra nada"),
        ("warnings.filterwarnings('always')", False, "no suprime"),
    ],
)
def test_the_detector_understands_each_signature(tmp_path: Path, llamada: str, amplia: bool, motivo: str) -> None:
    """Cada firma se liga por su nombre real; un patrón universal no acota aunque lo parezca."""
    (tmp_path / "vp_model").mkdir()
    (tmp_path / "vp_model" / "s.py").write_text(
        f"import builtins\nimport warnings\n\nopciones = ()\n{llamada}\n", encoding="utf-8"
    )
    encontrado = cw.detect_broad_suppressions(tmp_path)
    assert bool(encontrado) is amplia, f"{motivo}: {llamada!r} clasificada al revés ({encontrado})"


def test_the_detector_reaches_nested_packages(tmp_path: Path) -> None:
    """`rglob`: un paquete anidado también es productor; el `glob` plano no lo veía."""
    (tmp_path / "vp_model" / "sub").mkdir(parents=True)
    (tmp_path / "vp_model" / "sub" / "hondo.py").write_text(
        "import warnings\n\nwarnings.simplefilter('ignore')\n", encoding="utf-8"
    )
    assert cw.detect_broad_suppressions(tmp_path) == ["vp_model/sub/hondo.py:3"]


@pytest.mark.parametrize(
    "linea,detecta",
    [
        ("export PYTHONWARNINGS=ignore", True),
        ('export PYTHONWARNINGS="ignore"', True),
        ("$PY -W ignore experiments/x.py", True),
        ("# export PYTHONWARNINGS=ignore  (histórico)", False),
        ("export PYTHONWARNINGS=error", False),
        ("echo 'sin filtros'", False),
    ],
)
def test_environment_suppression_is_caught_where_no_ast_can_see_it(tmp_path: Path, linea: str, detecta: bool) -> None:
    """`PYTHONWARNINGS=ignore` en un `.sh` apaga todo lo que ese guion lance, y el AST no lo ve.

    Existían dos en el árbol (`run_experiments.sh`, `run_overnight_global.sh`) desde antes de M72:
    el inventario las declaraba en cero porque miraba sólo Python.
    """
    (tmp_path / "experiments").mkdir()
    (tmp_path / "experiments" / "x.sh").write_text(f"#!/bin/bash\n{linea}\n", encoding="utf-8")
    encontrado = cw.detect_broad_suppressions(tmp_path)
    assert bool(encontrado) is detecta, f"{linea!r} → {encontrado}"


def test_the_real_tree_has_no_environment_suppression_left() -> None:
    """Las dos históricas se retiraron en M74-B; el inventario real queda en cero."""
    assert cw.detect_env_suppressions(ROOT) == []


def test_the_detector_fails_closed_on_unparseable_code(tmp_path: Path) -> None:
    """Un productor que no compila no puede pasar por «sin supresiones»."""
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / "roto.py").write_text("def (:\n", encoding="utf-8")
    with pytest.raises(cw.ContractError, match="no se pudo analizar"):
        cw.detect_broad_suppressions(tmp_path)


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


# ------------------- M13-R1: la convergencia de Holt-Winters no puede tumbar el CI
HOLTWINTERS_MSG = "Optimization failed to converge. Check mle_retvals."
MLE_MSG = "Maximum Likelihood optimization failed to converge. Check mle_retvals"
HOLTWINTERS_CATEGORY = "statsmodels.tools.sm_exceptions.ConvergenceWarning"


class _SyntheticConvergenceWarning(Warning):
    """Sustituta local de ``ConvergenceWarning`` para probar el COMPORTAMIENTO del filtro.

    M14-R1: lo que se verifica aquí es el patrón de mensaje (que `re.escape` lo vuelve literal y
    que sólo calla lo declarado), no la clase concreta de statsmodels. Importar la real ataba estas
    pruebas al extra ``model``: el job base de CI instala sólo ``.[dev]`` y fallaban con
    ``ModuleNotFoundError``. La categoría real se sigue verificando como TEXTO en el registro.
    """


def _silences(message_prefix: str, category, emitted: str) -> bool:
    """¿El filtro derivado de `message_prefix` silencia el mensaje `emitted`?"""
    import warnings as _warnings

    _, msg, _ = cw.filter_expression({"message_prefix": message_prefix, "category": "x"}).split(":", 2)
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.resetwarnings()
        _warnings.filterwarnings("ignore", message=msg, category=category)
        _warnings.simplefilter("always", append=True)
        _warnings.warn(emitted, category, stacklevel=2)
    return not caught


def test_holtwinters_message_is_covered_by_its_own_exception() -> None:
    """El mensaje EXACTO que tumbó el CI 33844476552 queda cubierto."""
    entry = next(e for e in cw.verify(ROOT, TODAY) if e["id"] == "statsmodels-holtwinters-convergence")
    assert entry["message_prefix"] == HOLTWINTERS_MSG
    assert entry["category"] == HOLTWINTERS_CATEGORY  # la categoría real, verificada como texto
    assert _silences(entry["message_prefix"], _SyntheticConvergenceWarning, HOLTWINTERS_MSG)


@pytest.mark.parametrize(
    "emitted",
    [
        MLE_MSG,  # el otro ConvergenceWarning: tiene SU PROPIA entrada, no la de Holt-Winters
        "Optimization failed to converge. Something else entirely",
        "Optimization failed",
        "Convergence failed. Check mle_retvals.",
    ],
)
def test_neighbouring_messages_are_not_silenced_by_the_holtwinters_filter(emitted: str) -> None:
    """La excepción es estrecha: sólo calla su mensaje, no la vecindad."""
    assert not _silences(HOLTWINTERS_MSG, _SyntheticConvergenceWarning, emitted)


def test_each_convergence_message_has_its_own_narrow_entry() -> None:
    """Los dos mensajes de convergencia son distintos y cada uno tiene su entrada."""
    entries = {e["id"]: e for e in cw.verify(ROOT, TODAY)}
    holt = entries["statsmodels-holtwinters-convergence"]["message_prefix"]
    mle = entries["statsmodels-mle-convergence"]["message_prefix"]
    assert holt != mle and not mle.startswith(holt) and not holt.startswith(mle)


def test_deferred_debt_matches_the_code_exactly() -> None:
    """La deuda declarada y la viva son el mismo conjunto; desde M72, el vacío."""
    debt = _registry(ROOT)["deferred_debt"]
    assert debt["count"] == len(debt["sites"])
    assert sorted(debt["sites"]) == sorted(cw.detect_broad_suppressions(ROOT))


def test_filter_behaviour_checks_run_without_statsmodels_importable() -> None:
    """M14-R1: estas comprobaciones no dependen de que ``statsmodels`` sea importable.

    Se demuestra de dos formas: (1) el archivo no importa statsmodels en absoluto, y (2) con el
    módulo BLOQUEADO en ``sys.modules`` la verificación del filtro sigue corriendo y dando el
    mismo veredicto. Así el contrato es verificable en el job base (`.[dev]`) y no sólo en
    `model-tests`.
    """
    import importlib
    import sys

    source = Path(__file__).read_text(encoding="utf-8")
    # Agujas construidas en tiempo de ejecución: escritas literales, esta misma línea se
    # autodetectaría y la prueba sería un falso rojo.
    needles = ("import " + "statsmodels", "from " + "statsmodels")
    hits = [n for n in needles if n in source]
    assert not hits, f"las pruebas del contrato no pueden depender del extra `model`: {hits}"

    saved = {name: mod for name, mod in sys.modules.items() if name == "statsmodels" or name.startswith("statsmodels.")}
    for name in saved:
        sys.modules[name] = None  # type: ignore[assignment]  # bloquea el import durante la prueba
    try:
        with pytest.raises(ImportError):
            importlib.import_module("statsmodels.tools.sm_exceptions")
        assert _silences(HOLTWINTERS_MSG, _SyntheticConvergenceWarning, HOLTWINTERS_MSG)
        assert not _silences(HOLTWINTERS_MSG, _SyntheticConvergenceWarning, MLE_MSG)
    finally:
        for name, mod in saved.items():
            sys.modules[name] = mod
        for name in [n for n in sys.modules if sys.modules[n] is None and n.startswith("statsmodels")]:
            del sys.modules[name]
