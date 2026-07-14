"""P0R.4R: los bootstraps de torch en los workflows CPU no pueden divergir del pin canónico.

ci.yml (job model-tests) y freeze_and_rebuild.yml instalan `torch==X+cpu` del índice CPU antes de
`.[model]`. Si X divergiera del `torch==X` de pyproject.toml, `.[model]` reemplazaría el wheel CPU
por el CUDA de PyPI en silencio (el bug P0R.4R). Este guard estático lo impide sin instalar nada.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
_BOOTSTRAP = re.compile(r"pip install torch==([0-9][^\s+]*)(?:\+\w+)?\s+--index-url")


def _pyproject_torch_public() -> str:
    m = re.search(r'"torch==([0-9][^"+]*)', (ROOT / "pyproject.toml").read_text())
    assert m, "no se halló el pin torch en pyproject.toml"
    return m.group(1)


def _workflow_torch_public(name: str) -> str:
    m = _BOOTSTRAP.search((WF / name).read_text())
    assert m, f"no se halló el bootstrap `pip install torch==...+cpu` en {name}"
    return m.group(1)


def test_ci_torch_matches_pyproject():
    assert _workflow_torch_public("ci.yml") == _pyproject_torch_public()


def test_freeze_torch_matches_pyproject():
    assert _workflow_torch_public("freeze_and_rebuild.yml") == _pyproject_torch_public()


def test_workflows_assert_cpu_variant_survives():
    # tras `.[model]`, un assert exige que torch siga siendo +cpu (no el CUDA de PyPI).
    for name in ("ci.yml", "freeze_and_rebuild.yml"):
        text = (WF / name).read_text()
        assert 'torch.__version__ == "2.12.1+cpu"' in text, f"{name} sin assert +cpu"
        assert "pip check" in text, f"{name} sin pip check tras .[model]"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
