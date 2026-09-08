"""F8 · el orden DATA -> WEB -> PROD deja de ser costumbre y pasa a comprobarse.

El job `consistency` valida contra la rama por defecto del repo WEB, así que una regla nueva que
prohíba algo que el web todavía dice deja el PR de datos rojo por construcción (M33). Y el sitio no
queda `fresh` hasta que el manifiesto vive en `main` de datos. Aquí se fija ese orden ANTES de
mover nada, con un comando de **solo lectura**: nada de modificar, regenerar, descargar, pushear o
desplegar. Cada modo de fallo tiene su RED sobre un par de repositorios de juguete.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

try:
    import check_propagation as cp
except ModuleNotFoundError as exc:  # F8 aún no está: cada prueba falla por su cuenta y lo dice
    cp = None  # type: ignore[assignment]
    _POR_QUE = str(exc)


@pytest.fixture(autouse=True)
def _f8_existe() -> None:
    if cp is None:
        pytest.fail(f"F8 no está en el árbol: {_POR_QUE}")


MANIFIESTO = {
    "schema_version": 1,
    "release_id": "2026-09-abc",
    "git_sha": "deadbeef",
    "artifacts": [{"path": "facts.json", "sha256": "", "criticality": "critical"}],
}


def _repo(base: Path, nombre: str, archivos: dict[str, str]) -> Path:
    repo = base / nombre
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    for ruta, contenido in archivos.items():
        destino = repo / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=repo, check=True)
    return repo


def _par(tmp_path: Path, *, facts="{}", release="2026-09-abc", pin_release=None, data_main=None, sha=None):
    import hashlib

    pin_release = pin_release or release
    facts_sha = sha if sha is not None else hashlib.sha256(facts.encode()).hexdigest()
    manifiesto = {
        **MANIFIESTO,
        "release_id": release,
        "artifacts": [{"path": "facts.json", "sha256": facts_sha, "criticality": "critical"}],
    }
    datos = _repo(
        tmp_path,
        "datos",
        {
            "facts.json": facts,
            "reports/release/release_manifest.json": json.dumps(manifiesto),
        },
    )
    head = subprocess.run(
        ["git", "-C", str(datos), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    web = _repo(
        tmp_path,
        "web",
        {
            "lib/content/data-pins.generated.mjs": f'export const DATA_PINS = {{"releaseId": "{pin_release}", "releaseStatus": "fresh"}};\n',
            "lib/plan-data.ts": f'export const PLAN_META = {{\n  dataMain: "{data_main or head}",\n}};\n',
        },
    )
    return datos, web


class TestTheLiveTreeIsUnderstood:
    def test_the_web_repo_is_found_without_a_personal_path(self) -> None:
        fuente = (ROOT / "tools" / "check_propagation.py").read_text()
        assert "VP_WEB_DIR" in fuente
        assert "/Users/" not in fuente and "/home/" not in fuente

    def test_the_command_is_read_only(self) -> None:
        """Sobre el CÓDIGO, no sobre el texto: la primera versión de esta prueba se disparaba con
        la palabra «pushea» del propio docstring que declara la propiedad."""
        import ast

        arbol = ast.parse((ROOT / "tools" / "check_propagation.py").read_text())
        escrituras = {"write_text", "write_bytes", "mkdir", "unlink", "rmtree", "urlopen", "urlretrieve"}
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute):
                assert nodo.func.attr not in escrituras, f"llamada que escribe: .{nodo.func.attr}()"
            if isinstance(nodo, ast.Import):
                for alias in nodo.names:
                    assert alias.name.split(".")[0] not in {"requests", "urllib", "httpx"}, (
                        f"import de red: {alias.name}"
                    )

    def test_every_subprocess_is_a_read_only_git_command(self) -> None:
        import ast

        arbol = ast.parse((ROOT / "tools" / "check_propagation.py").read_text())
        # git solo se invoca con subcomandos que LEEN; el dispatcher es uno solo y se comprueba.
        vistos = 0
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "run":
                vistos += 1
                literales = [e.value for e in getattr(nodo.args[0], "elts", []) if isinstance(e, ast.Constant)]
                assert literales and literales[0] == "git", f"subproceso que no es git: {literales}"
        assert vistos == 1, f"se esperaba una sola invocación de subproceso, hay {vistos}"

    def test_the_real_repos_report_their_state(self) -> None:
        estado, _ = cp.check()
        assert estado["release_id"] and estado["n_critical"] >= 1
        assert "web_release_id" in estado


class TestTheHappyPathIsSilent:
    def test_a_coherent_pair_passes(self, tmp_path) -> None:
        datos, web = _par(tmp_path)
        estado, problemas = cp.check(datos, web)
        assert problemas == []
        assert estado["release_id"] == estado["web_release_id"] == "2026-09-abc"

    def test_a_web_that_lags_is_fine(self, tmp_path) -> None:
        """Control benigno: el web PUEDE ir por detrás; lo que no puede es ir por delante."""
        datos, web = _par(tmp_path)
        (datos / "otro.txt").write_text("x")
        subprocess.run(["git", "-C", str(datos), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(datos), "commit", "-qm", "avanza"], check=True)
        _, problemas = cp.check(datos, web)
        assert problemas == []


class TestEveryFailureModeIsCaught:
    def test_a_dirty_tree_blocks(self, tmp_path) -> None:
        datos, web = _par(tmp_path)
        (datos / "sucio.txt").write_text("x")
        _, problemas = cp.check(datos, web)
        assert any("árbol sucio" in p for p in problemas)

    def test_a_missing_source_blocks(self, tmp_path) -> None:
        datos, web = _par(tmp_path)
        (datos / "facts.json").unlink()
        _, problemas = cp.check(datos, web)
        assert any("crítico ausente" in p for p in problemas)

    def test_a_tampered_artifact_blocks(self, tmp_path) -> None:
        datos, web = _par(tmp_path, facts='{"n": 1}', sha="0" * 64)
        _, problemas = cp.check(datos, web)
        assert any("manipulado" in p for p in problemas)

    def test_a_divergent_pin_blocks_and_says_the_order(self, tmp_path) -> None:
        datos, web = _par(tmp_path, release="2026-10-nuevo", pin_release="2026-09-viejo")
        _, problemas = cp.check(datos, web)
        assert any("pin divergente" in p and "datos ANTES que web" in p for p in problemas)

    def test_a_web_ahead_of_data_blocks(self, tmp_path) -> None:
        """Orden invertido: el web declara un corte de datos que este repo no tiene."""
        datos, web = _par(tmp_path, data_main="0" * 40)
        _, problemas = cp.check(datos, web)
        assert any("orden invertido" in p for p in problemas)

    def test_an_unavailable_web_repo_blocks_instead_of_being_skipped(self, tmp_path) -> None:
        datos, _ = _par(tmp_path)
        _, problemas = cp.check(datos, tmp_path / "no-existe")
        assert any("no responde a git" in p for p in problemas)
        assert any("pin del web ausente" in p for p in problemas)

    def test_an_unreadable_manifest_blocks(self, tmp_path) -> None:
        datos, web = _par(tmp_path)
        (datos / "reports/release/release_manifest.json").write_text("{no json")
        _, problemas = cp.check(datos, web)
        assert any("ilegible" in p for p in problemas)

    def test_a_manifest_with_no_critical_artifact_blocks(self, tmp_path) -> None:
        datos, web = _par(tmp_path)
        m = json.loads((datos / "reports/release/release_manifest.json").read_text())
        m["artifacts"] = []
        (datos / "reports/release/release_manifest.json").write_text(json.dumps(m))
        _, problemas = cp.check(datos, web)
        assert any("sin artefactos `critical`" in p for p in problemas)


class TestItLeavesEverythingExactlyAsItFoundIt:
    def test_hashes_and_git_state_are_untouched(self, tmp_path) -> None:
        import hashlib

        datos, web = _par(tmp_path)

        def huella(repo: Path) -> tuple:
            archivos = sorted(p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts)
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout
            estado = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True
            ).stdout
            return (
                tuple((str(p.relative_to(repo)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in archivos),
                head,
                estado,
            )

        antes = (huella(datos), huella(web))
        cp.check(datos, web)
        cp.check(datos, web)
        assert (huella(datos), huella(web)) == antes

    def test_two_runs_report_the_same(self, tmp_path) -> None:
        datos, web = _par(tmp_path)
        assert cp.check(datos, web) == cp.check(datos, web)


def test_the_runbook_exists_and_states_the_order() -> None:
    doc = (ROOT / "docs" / "RUNBOOK_PROPAGATION.md").read_text()
    for exigido in ("DATA → WEB → PROD", "Precondiciones", "Condiciones de parada", "Verificación", "Rollback"):
        assert exigido in doc
    assert "make propagate-check" in doc


def test_the_makefile_exposes_the_gate() -> None:
    assert "propagate-check:" in (ROOT / "Makefile").read_text()


@pytest.mark.parametrize("orden", ["DATA", "WEB", "PROD"])
def test_the_runbook_names_each_stage(orden: str) -> None:
    assert orden in (ROOT / "docs" / "RUNBOOK_PROPAGATION.md").read_text()
