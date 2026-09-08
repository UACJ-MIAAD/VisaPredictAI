"""C8: lo que el tooling AFIRMA medir es lo que mide.

Tres huecos concretos, que estas pruebas fijan:

* la complejidad no se medía en ninguna parte, ni con umbral ni con informe;
* las supresiones se contaban en un total agregado que mezclaba un orden de imports
  deliberado con una captura amplia justificada, así que el número no significaba nada;
* `except:` y `except BaseException` —peores que `except Exception`— no se contaban.

Y un cuarto, de lectura: el `fail_under` de pytest gobierna tres módulos de parseo, pero su
titular se lee como cobertura del repo. Aquí se comprueba que el alcance real y el declarado
coinciden.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_debt  # noqa: E402

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
_RUFF_TOML = ROOT / ".ruff.toml"
# Defensivo a propósito: si el archivo no existe, cada aserción falla por su cuenta y dice cuál
# es la que falta. Un error de colección solo diría «no hay archivo».
RUFF_TOML = tomllib.loads(_RUFF_TOML.read_text()) if _RUFF_TOML.exists() else {}
FLOORS = json.loads((ROOT / "docs" / "coverage_floors.json").read_text())
PRODUCTO = ("vp_data", "pipeline", "vp_model")


def _ruff(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shutil.which("ruff") or "ruff", "check", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        input=stdin,
    )


def _codigos(ruta: str, fuente: str) -> set[str]:
    """Las reglas que dispara ESE fuente COMO SI viviera en esa ruta: se prueba la configuración
    efectiva (per-file-ignores incluidos), no el texto de un TOML."""
    salida = _ruff("--stdin-filename", ruta, "--output-format", "concise", "-", stdin=fuente)
    return {linea.split(": ", 1)[1].split(" ", 1)[0] for linea in salida.stdout.splitlines() if ": " in linea}


CAPTURA_AMPLIA = "try:\n    pass\nexcept Exception:\n    pass\n"
SUPRESION_INSERVIBLE = "import os\n\nprint(os)  # noqa: F401\n"
COMPLEJA = "def f(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(25)) + "    return 0\n"


sin_ruff = pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff no está en este entorno")


class TestTheRulesAreActuallyEnabled:
    def test_the_config_lives_beside_pyproject_and_extends_it(self) -> None:
        """`locks/lockset.json` pinnea `pyproject.toml` por sha256: editarlo invalida el
        manifiesto de locks. Por eso los añadidos van en `.ruff.toml`, heredando la base."""
        assert RUFF_TOML.get("extend") == "pyproject.toml"
        assert PYPROJECT["tool"]["ruff"]["line-length"] == 120, "la base heredada sigue siendo la del proyecto"

    @sin_ruff
    @pytest.mark.parametrize(
        ("regla", "fuente"),
        [("BLE001", CAPTURA_AMPLIA), ("RUF100", SUPRESION_INSERVIBLE), ("C901", COMPLEJA)],
    )
    def test_the_rule_actually_fires_on_the_product(self, regla: str, fuente: str) -> None:
        """Se comprueba DISPARANDO la regla con la config real, no leyendo el TOML."""
        assert regla in _codigos("vp_model/_sonda.py", fuente), f"{regla} no dispara en el producto"

    @sin_ruff
    def test_no_suppression_in_the_repo_is_inert(self) -> None:
        """RUF100 es la demostración: una directiva que no suprime nada es ruido que envejece."""
        salida = _ruff("--output-format", "concise", ".")
        assert "RUF100" not in salida.stdout, f"quedan supresiones inservibles:\n{salida.stdout}"

    @sin_ruff
    def test_the_broad_catch_rule_leaves_no_unexplained_site(self) -> None:
        salida = _ruff("--select", "BLE001", "--output-format", "concise", ".")
        assert salida.returncode == 0, f"capturas amplias sin razón declarada:\n{salida.stdout}"


class TestComplexityIsMeasuredOnTheProduct:
    def test_the_threshold_exists_and_is_a_number(self) -> None:
        assert isinstance(RUFF_TOML.get("lint", {}).get("mccabe", {}).get("max-complexity"), int)

    @sin_ruff
    @pytest.mark.parametrize("capa", PRODUCTO)
    def test_no_product_layer_is_exempted(self, capa: str) -> None:
        """Un per-file-ignore sobre el producto convertiría el umbral en adorno. Se comprueba
        disparando la regla en cada capa, no leyendo la lista de exenciones."""
        assert "C901" in _codigos(f"{capa}/_sonda.py", COMPLEJA), f"C901 no rige en {capa}"

    @sin_ruff
    def test_the_metric_is_declared_only_for_the_product(self) -> None:
        """Y fuera del producto NO se afirma nada: el máximo real de tools es 36."""
        assert "C901" not in _codigos("tools/_sonda.py", COMPLEJA)

    @sin_ruff
    def test_the_threshold_has_no_slack_over_the_measured_maximum(self) -> None:
        """El umbral ES el máximo medido. Si alguien lo sube sin medir, esto falla; si la
        complejidad real baja, hay que bajarlo en el mismo PR (trinquete, no meta)."""
        salida = _ruff(
            "--select", "C901", "--config", "lint.mccabe.max-complexity=1", "--output-format", "concise", *PRODUCTO
        )
        medidos = [
            int(linea.rsplit("(", 1)[1].split(" >")[0]) for linea in salida.stdout.splitlines() if "C901" in linea
        ]
        assert medidos, "no se pudo medir la complejidad del producto"
        umbral = RUFF_TOML.get("lint", {}).get("mccabe", {}).get("max-complexity")
        assert umbral == max(medidos), f"umbral {umbral} vs máximo medido {max(medidos)}"


class TestSuppressionsAreCountedByRule:
    def test_there_is_no_aggregate_total(self) -> None:
        """El total agregado sumaba naturalezas distintas: bajarlo no significaba nada."""
        assert "noqa" not in check_debt.METRICS

    @pytest.mark.parametrize(
        ("fuente", "esperado"),
        [
            ("import os  # noqa: E402\n", {"noqa_E402": 1}),
            ("import os  # noqa: F401\n", {"noqa_F401": 1}),
            ("x = 1  # noqa\n", {"noqa_bare": 1}),
            ("x = 1  # noqa: ANN001\n", {"noqa_other": 1}),
            ("import os  # noqa: E402, F401\n", {"noqa_E402": 1, "noqa_F401": 1}),
        ],
    )
    def test_each_directive_lands_in_its_own_bucket(self, fuente: str, esperado: dict[str, int]) -> None:
        conteo = check_debt.count_file(fuente)
        for clave, valor in esperado.items():
            assert conteo[clave] == valor, f"{fuente!r} → {clave}={conteo[clave]}, se esperaba {valor}"
        otros = {k: v for k, v in conteo.items() if k.startswith("noqa_") and k not in esperado}
        assert not any(otros.values()), f"{fuente!r} contaminó {otros}"

    def test_a_directive_for_a_disabled_rule_is_not_free(self) -> None:
        """Cae en `noqa_other`, que es su propia deuda: no se disuelve en un total."""
        assert check_debt.count_file("x = 1  # noqa: ANN001\n")["noqa_other"] == 1


class TestTheWorseCatchesAreCountedApart:
    def test_a_bare_except_is_its_own_metric(self) -> None:
        conteo = check_debt.count_file("try:\n    pass\nexcept:\n    pass\n")
        assert conteo["bare_except"] == 1
        assert conteo["except_exception"] == 0, "no se mezcla hacia atrás con except_exception"

    def test_base_exception_is_its_own_metric(self) -> None:
        conteo = check_debt.count_file("try:\n    pass\nexcept BaseException:\n    pass\n")
        assert conteo["base_exception"] == 1
        assert conteo["except_exception"] == 0, "no se mezcla hacia atrás con except_exception"

    def test_except_exception_keeps_its_unit(self) -> None:
        conteo = check_debt.count_file("try:\n    pass\nexcept Exception:\n    pass\n")
        assert (conteo["except_exception"], conteo["bare_except"], conteo["base_exception"]) == (1, 0, 0)

    def test_a_tuple_that_names_base_exception_counts(self) -> None:
        conteo = check_debt.count_file("try:\n    pass\nexcept (ValueError, BaseException):\n    pass\n")
        assert conteo["base_exception"] == 1


class TestJustificationDoesNotRideOnAnotherRule:
    def test_an_unrelated_directive_does_not_justify_a_broad_catch(self) -> None:
        """RED del mecanismo viejo: `noqa` de CUALQUIER código valía como razón."""
        conteo = check_debt.count_file("try:\n    pass\nexcept Exception:  # noqa: E402\n    pass\n")
        assert conteo["except_exception_unjustified"] == 1

    def test_the_directive_that_really_silences_it_justifies_it(self) -> None:
        conteo = check_debt.count_file("try:\n    pass\nexcept Exception:  # noqa: BLE001 — razón\n    pass\n")
        assert conteo["except_exception_unjustified"] == 0

    def test_the_explicit_marker_justifies_it_where_the_linter_never_fires(self) -> None:
        """BLE001 no marca los handlers que re-lanzan: ahí la razón necesita marcador propio."""
        fuente = "try:\n    pass\nexcept Exception:  # broad-catch: se re-lanza salvo 409\n    raise\n"
        assert check_debt.count_file(fuente)["except_exception_unjustified"] == 0

    def test_an_empty_marker_is_not_a_reason(self) -> None:
        fuente = "try:\n    pass\nexcept Exception:  # broad-catch:\n    pass\n"
        assert check_debt.count_file(fuente)["except_exception_unjustified"] == 1


class TestNothingEscapesTheMeasurement:
    def test_modules_inside_subpackages_are_counted(self) -> None:
        """`glob('*.py')` dejaba fuera todo subpaquete sin decirlo."""
        medidos = {f.relative_to(check_debt.ROOT) for f in check_debt._source_files()}
        anidados = {f for f in medidos if len(f.parts) > 2}
        assert anidados, "ningún módulo anidado entró en la medida"

    def test_every_product_package_has_a_coverage_floor(self) -> None:
        """Un paquete de producto nuevo no puede quedar fuera de la métrica en silencio."""
        con_piso = {p.rstrip("/") for capa in FLOORS["layers"].values() for p in capa["paths"]}
        paquetes = {p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()} - {"tests"}
        assert paquetes <= con_piso, f"sin piso de cobertura: {sorted(paquetes - con_piso)}"

    def test_the_narrow_gate_declares_the_scope_it_governs(self) -> None:
        """El `fail_under` de pytest gobierna tres módulos; su titular se lee como el repo entero."""
        addopts = PYPROJECT["tool"]["pytest"]["ini_options"]["addopts"]
        instrumentados = sorted(a.split("=", 1)[1] for a in addopts if a.startswith("--cov="))
        assert instrumentados == sorted(FLOORS["parsing_gate"]["modules"])
        assert FLOORS["parsing_gate"]["fail_under"] == PYPROJECT["tool"]["coverage"]["report"]["fail_under"]


def test_the_baseline_matches_the_declared_metrics() -> None:
    base = check_debt.load_baseline((ROOT / "docs" / "debt_baseline.json").read_text())
    assert sorted(base) == sorted(check_debt.METRICS)


def test_the_module_under_test_is_the_repo_one() -> None:
    """Sin esto, un `check_debt` de otro sitio dejaría estas pruebas sin objeto."""
    spec = importlib.util.find_spec("check_debt")
    assert spec is not None and spec.origin == str(ROOT / "tools" / "check_debt.py")
    assert isinstance(ast.parse((ROOT / "tools" / "check_debt.py").read_text()), ast.Module)
