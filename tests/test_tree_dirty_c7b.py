"""C7b: una sola definición comprobable de «árbol sucio».

Había tres implementaciones sueltas del mismo `git status --porcelain`, y una de ellas
excluía `reports/` y `data/` a propósito. La diferencia era legítima; lo que no lo era es
que hubiera que leer tres funciones para descubrirla.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
from vp_data.tree_dirty import REPO_ROOT, Scope, porcelain, tree_dirty  # noqa: E402


class TestTheDefinition:
    def test_the_scope_decides_the_pathspec(self) -> None:
        assert Scope.ALL.pathspec == ()
        assert Scope.CODE.pathspec == ("--", ".", ":(exclude)reports", ":(exclude)data")

    def test_a_clean_repository_is_not_dirty(self, tmp_path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "a.txt").write_text("hola\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], cwd=tmp_path, check=True
        )
        assert tree_dirty(Scope.ALL, root=tmp_path) is False

    def test_an_uncommitted_change_makes_it_dirty(self, tmp_path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "a.txt").write_text("hola\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], cwd=tmp_path, check=True
        )
        (tmp_path / "a.txt").write_text("otra cosa\n")
        assert tree_dirty(Scope.ALL, root=tmp_path) is True

    def test_the_code_scope_ignores_reports_and_data(self, tmp_path) -> None:
        """Lo que justifica que exista un segundo alcance, dicho como prueba."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        for sub in ("reports", "data"):
            (tmp_path / sub).mkdir()
            (tmp_path / sub / "salida.txt").write_text("v1\n")
        (tmp_path / "codigo.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], cwd=tmp_path, check=True
        )
        (tmp_path / "reports" / "salida.txt").write_text("v2\n")
        (tmp_path / "data" / "salida.txt").write_text("v2\n")
        assert tree_dirty(Scope.ALL, root=tmp_path) is True, "el árbol entero SÍ está sucio"
        assert tree_dirty(Scope.CODE, root=tmp_path) is False, "el código no cambió"
        (tmp_path / "codigo.py").write_text("x = 2\n")
        assert tree_dirty(Scope.CODE, root=tmp_path) is True

    def test_without_git_it_says_it_does_not_know(self, tmp_path) -> None:
        """`None` no es `False`: grabar «limpio» sin saberlo fabrica identidad de build."""
        assert tree_dirty(Scope.ALL, root=tmp_path) is None
        assert porcelain(Scope.ALL, root=tmp_path) is None

    def test_the_repo_root_points_at_the_repository(self) -> None:
        assert (REPO_ROOT / "pyproject.toml").exists()


class TestTheConsumersUseIt:
    CONSUMIDORES = ("vp_model/ledger.py", "pipeline/db_governance.py")

    @pytest.mark.parametrize("path", CONSUMIDORES)
    def test_no_consumer_reimplements_the_porcelain_call(self, path: str) -> None:
        """Anti-resurrección: nadie vuelve a escribir su propio `status --porcelain`."""
        src = (ROOT / path).read_text(encoding="utf-8")
        assert '"status", "--porcelain"' not in src, f"{path} reimplementa la comprobación"
        assert "from vp_data.tree_dirty import" in src, f"{path} no usa la definición única"

    def test_the_ledger_marks_a_dirty_tree_in_its_sha(self, monkeypatch) -> None:
        from vp_model import ledger

        monkeypatch.setattr(ledger, "tree_dirty", lambda scope, root=None: True)
        assert ledger.git_sha().endswith("-dirty")
        monkeypatch.setattr(ledger, "tree_dirty", lambda scope, root=None: False)
        assert not ledger.git_sha().endswith("-dirty")

    def test_the_ledger_says_it_does_not_know_instead_of_guessing(self, monkeypatch) -> None:
        from vp_model import ledger

        monkeypatch.setattr(ledger, "tree_dirty", lambda scope, root=None: None)
        assert ledger.git_sha() == "n/d"

    def test_the_build_identity_propagates_the_unknown_as_null(self, monkeypatch) -> None:
        from pipeline import db_governance

        monkeypatch.setattr(db_governance, "tree_dirty", lambda scope, root=None: None)
        sha, dirty = db_governance._git_identity()
        assert sha is not None and len(sha) == 40
        assert dirty is None, "un dirty desconocido no puede grabarse como False"

    def test_only_the_code_scope_excludes_generated_output(self) -> None:
        """El alcance restringido sigue existiendo y sigue documentado en un solo sitio."""
        src = (ROOT / "vp_data/tree_dirty.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        assert any(isinstance(n, ast.ClassDef) and n.name == "Scope" for n in tree.body)
        assert "reports" in src and "data" in src
