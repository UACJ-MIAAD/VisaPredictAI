"""C9: cada archivo gobernado tiene exactamente un papel, y el tamaño se mide.

La afirmación que abrió esta épica —que el tooling llegó a pesar 1.85 veces el producto— se midió
a mano una tarde de agosto y no volvió a comprobarse. Aquí se fija: el universo es lo que
`git ls-files` enumera, cada ruta cae en un rol y solo uno, y los cuatro modos de fallo (sin
clasificar, reclamada por dos globs, regla exacta duplicada, regla exacta sin archivo) rompen el
gate en vez de resolverse en silencio.

Las pruebas de fallo trabajan sobre un repositorio de juguete: mutar el real para ver el rojo
dejaría el árbol sucio, y un `git ls-files` prestado no probaría el mismo camino.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

try:
    import check_loc_by_role as loc
except ModuleNotFoundError as exc:  # C9 todavía no existe: cada prueba falla por su cuenta y lo dice
    loc = None  # type: ignore[assignment]
    _POR_QUE = str(exc)


@pytest.fixture(autouse=True)
def _c9_existe() -> None:
    if loc is None:
        pytest.fail(f"C9 no está en el árbol: {_POR_QUE}")


REGLAS_MINIMAS = {
    "roles": {"producto": "el sistema", "herramientas": "los gates"},
    "policy": {"tooling_ratio_max": 0.5},
    "rules": [{"glob": "src/**/*.py", "role": "producto"}, {"glob": "tools/**/*.py", "role": "herramientas"}],
}


def _repo(tmp_path: pathlib.Path, archivos: dict[str, str]) -> pathlib.Path:
    """Un repositorio de juguete, versionado: `governed_files` habla con git de verdad."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for ruta, contenido in archivos.items():
        destino = tmp_path / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _reglas(tmp_path: pathlib.Path, data: dict) -> pathlib.Path:
    destino = tmp_path / "reglas.json"
    destino.write_text(json.dumps(data))
    return destino


class TestTheMatcherIsExactAndDeterministic:
    @pytest.mark.parametrize(
        ("ruta", "patron", "esperado"),
        [
            ("reports/a.csv", "reports/**/*.csv", True),  # `**` acepta CERO segmentos
            ("reports/x/y/a.csv", "reports/**/*.csv", True),  # y también varios
            ("tools/a/b.py", "tools/*.py", False),  # un segmento no cruza la barra
            ("tools/b.py", "tools/*.py", True),
            ("docs/A.md", "docs/*.md", True),
            ("docs/a.MD", "docs/*.md", False),  # distingue mayúsculas
            ("a.py", "**/*.py", True),
            ("x/a.py", "*.py", False),
        ],
    )
    def test_segments_never_cross_a_slash(self, ruta: str, patron: str, esperado: bool) -> None:
        assert loc.matches(ruta, patron) is esperado

    def test_the_same_input_gives_the_same_output(self) -> None:
        una, otra = loc.governed_files(ROOT), loc.governed_files(ROOT)
        assert una == otra == sorted(una)


class TestTheGateIsFailClosed:
    def test_a_file_no_rule_claims_is_named(self, tmp_path) -> None:
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n", "huerfano.txt": "hola\n"})
        _, problemas = loc.report(repo, _reglas(tmp_path, REGLAS_MINIMAS))
        assert any("SIN CLASIFICAR: huerfano.txt" in p for p in problemas)

    def test_a_file_two_globs_claim_is_named_with_both(self, tmp_path) -> None:
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["rules"].append({"glob": "**/*.py", "role": "herramientas"})
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n"})
        _, problemas = loc.report(repo, _reglas(tmp_path, reglas))
        duplicado = [p for p in problemas if "CLASIFICADO DOS VECES: src/a.py" in p]
        assert duplicado and "src/**/*.py" in duplicado[0] and "**/*.py" in duplicado[0]

    def test_two_globs_of_the_SAME_role_still_break_it(self, tmp_path) -> None:
        """Que coincidan en el rol no las vuelve una sola regla: la ambigüedad es la falla."""
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["rules"].append({"glob": "**/*.py", "role": "producto"})
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n"})
        _, problemas = loc.report(repo, _reglas(tmp_path, reglas))
        assert any("CLASIFICADO DOS VECES" in p for p in problemas)

    def test_a_duplicated_exact_rule_is_rejected(self, tmp_path) -> None:
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["rules"] += [{"glob": "LICENSE", "role": "producto"}, {"glob": "LICENSE", "role": "herramientas"}]
        with pytest.raises(ValueError, match="regla exacta duplicada"):
            loc.load_rules(json.dumps(reglas))

    def test_an_exact_rule_with_no_file_behind_it_is_a_ghost(self, tmp_path) -> None:
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["rules"].append({"glob": "borrado.py", "role": "producto"})
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n"})
        _, problemas = loc.report(repo, _reglas(tmp_path, reglas))
        assert any("FANTASMA: la regla exacta borrado.py" in p for p in problemas)

    def test_an_undeclared_role_is_rejected(self) -> None:
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["rules"].append({"glob": "otro/*.py", "role": "inventado"})
        with pytest.raises(ValueError, match="no declarado"):
            loc.load_rules(json.dumps(reglas))

    @pytest.mark.parametrize("techo", [0, -1, True, "0.5", None])
    def test_a_ceiling_that_is_not_a_positive_number_is_rejected(self, techo) -> None:
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["policy"]["tooling_ratio_max"] = techo
        with pytest.raises(ValueError, match="tooling_ratio_max"):
            loc.load_rules(json.dumps(reglas))

    def test_a_versioned_environment_invalidates_the_measurement(self, tmp_path) -> None:
        """Un entorno versionado metería millones de líneas ajenas: se detiene antes de contar."""
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["rules"].append({"glob": "ante/**/*.py", "role": "producto"})
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n", "ante/lib/tercero.py": "y = 2\n"})
        _, problemas = loc.report(repo, _reglas(tmp_path, reglas))
        assert any("ENTORNO GOBERNADO: ante/lib/tercero.py" in p for p in problemas)


class TestTheBenignCasesStaySilent:
    def test_an_exact_rule_may_override_a_glob_without_complaint(self, tmp_path) -> None:
        """Es el mecanismo declarado de excepción: el generado que vive entre la prosa."""
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["roles"]["generados"] = "lo que emite un generador"
        reglas["rules"] += [
            {"glob": "docs/*.md", "role": "producto"},
            {"glob": "docs/GENERADO.md", "role": "generados"},
        ]
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n", "docs/manual.md": "a\n", "docs/GENERADO.md": "b\n"})
        medicion, problemas = loc.report(repo, _reglas(tmp_path, reglas))
        assert problemas == []
        assert medicion["por_rol"]["generados"]["files"] == 1

    def test_two_globs_that_claim_different_files_are_fine(self, tmp_path) -> None:
        # El producto lleva holgura sobre el techo a propósito: aquí se mira la clasificación,
        # no la política, y una razón de 1.0 haría fallar por otra cosa.
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n" * 10, "tools/b.py": "y = 2\n"})
        medicion, problemas = loc.report(repo, _reglas(tmp_path, REGLAS_MINIMAS))
        assert problemas == []
        assert medicion["por_rol"]["producto"]["files"] == medicion["por_rol"]["herramientas"]["files"] == 1

    def test_a_non_code_file_counts_as_a_file_and_not_as_lines(self, tmp_path) -> None:
        """Un contrato JSON pertenece a su rol, pero no infla numerador ni denominador."""
        reglas = json.loads(json.dumps(REGLAS_MINIMAS))
        reglas["rules"].append({"glob": "src/**/*.json", "role": "producto"})
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n", "src/contrato.json": "{\n}\n"})
        medicion, _ = loc.report(repo, _reglas(tmp_path, reglas))
        assert medicion["por_rol"]["producto"] == {"files": 2, "code_files": 1, "code_lines": 1}


class TestThePolicyIsTheOnlyHandWrittenNumber:
    def test_crossing_the_ceiling_turns_the_gate_red(self, tmp_path) -> None:
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n", "tools/b.py": "y = 2\n" * 3})
        _, problemas = loc.report(repo, _reglas(tmp_path, REGLAS_MINIMAS))
        assert any("TOOLING DESBORDADO" in p and "3.000" in p for p in problemas)

    def test_staying_under_the_ceiling_does_not(self, tmp_path) -> None:
        repo = _repo(tmp_path, {"src/a.py": "x = 1\n" * 10, "tools/b.py": "y = 2\n"})
        _, problemas = loc.report(repo, _reglas(tmp_path, REGLAS_MINIMAS))
        assert problemas == []

    def test_no_rule_carries_a_count(self) -> None:
        """Las cifras se miden; en el archivo de reglas solo hay globs, roles y razones."""
        data = json.loads((ROOT / "docs" / "loc_roles.json").read_text())
        for regla in data["rules"]:
            assert set(regla) <= {"glob", "role", "why"}, f"campo de más en {regla}"
            assert not any(isinstance(v, (int, float)) for v in regla.values())

    def test_the_ceiling_is_the_one_the_author_set(self) -> None:
        data = json.loads((ROOT / "docs" / "loc_roles.json").read_text())
        assert data["policy"]["tooling_ratio_max"] == 0.5


class TestTheRealRepositoryIsFullyClassified:
    def test_every_governed_file_has_exactly_one_role(self) -> None:
        medicion, problemas = loc.report()
        assert problemas == [], f"{len(problemas)} problemas: {problemas[:5]}"
        assert sum(v["files"] for v in medicion["por_rol"].values()) == medicion["archivos"]

    def test_no_governed_path_lives_in_an_environment(self) -> None:
        """Recorre lo gobernado y exige que ninguna ruta empiece por un prefijo de entorno.

        La versión anterior comprobaba primero que `ante/` existiera en disco, para que la
        aserción no fuera vacua; eso ataba la prueba a la máquina del autor y en el runner no hay
        venv. La guarda contra la vacuidad es ahora la lista misma, tomada del módulo para que
        ampliarla no deje la prueba sin objeto, y el caso discriminante vive donde debe: en la
        mutación sintética que mete una ruta de entorno y exige que el contrato se rompa."""
        assert loc.ENV_PREFIXES, "sin prefijos declarados, esta comprobación no afirmaría nada"
        intrusos = [p for p in loc.governed_files(ROOT) for pre in loc.ENV_PREFIXES if p.startswith(pre)]
        assert intrusos == [], f"entornos versionados: {intrusos[:5]}"

    def test_the_tooling_is_under_the_ceiling(self) -> None:
        medicion, _ = loc.report()
        assert medicion["ratio_herramientas_producto"] <= medicion["techo"]

    def test_the_five_roles_the_epic_names_all_have_files(self) -> None:
        medicion, _ = loc.report()
        for rol in ("producto", "pruebas", "herramientas", "experimentos", "generados"):
            assert medicion["por_rol"][rol]["files"] > 0, f"{rol} quedó vacío"

    def test_the_excluded_roles_are_declared_and_not_silent(self) -> None:
        data = json.loads((ROOT / "docs" / "loc_roles.json").read_text())
        for rol in ("evidencia", "vendor", "datos"):
            assert rol in data["roles"], f"{rol} debe estar declarado, no excluido en silencio"

    def test_vendor_files_left_with_the_latex_repository(self) -> None:
        medicion, _ = loc.report()
        assert medicion["por_rol"]["vendor"]["code_lines"] == 0
        assert medicion["por_rol"]["vendor"]["files"] == 0
        latex = ROOT / "latex_repo"
        if not latex.exists():
            latex = ROOT.parent / "VisaPredictAI_LaTeX"
        assert (latex / "reports/paper_micai/llncs.cls").is_file()
        assert (latex / "reports/paper_micai/splncs04.bst").is_file()
