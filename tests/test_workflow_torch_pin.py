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
    # tras `.[model]`, un assert exige que torch siga siendo +cpu (no el CUDA de PyPI). La versión
    # esperada se DERIVA del pin de pyproject (no se re-tipea): así no queda un literal viejo suelto.
    expected = f"{_pyproject_torch_public()}+cpu"
    for name in ("ci.yml", "freeze_and_rebuild.yml"):
        text = (WF / name).read_text()
        assert f'torch.__version__ == "{expected}"' in text, f"{name} sin assert {expected}"
        assert "pip check" in text, f"{name} sin pip check tras .[model]"
        # ningún assert con OTRA versión de torch (literal viejo)
        for stray in re.findall(r'torch\.__version__ == "([^"]+)"', text):
            assert stray == expected, f"{name}: assert torch obsoleto {stray} != {expected}"


def test_single_cpu_bootstrap_per_workflow():
    # exactamente un bootstrap `pip install torch==…+cpu` por workflow (sin un segundo pin CPU)
    for name in ("ci.yml", "freeze_and_rebuild.yml"):
        text = (WF / name).read_text()
        assert len(_BOOTSTRAP.findall(text)) == 1, f"{name}: != 1 bootstrap torch +cpu"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
