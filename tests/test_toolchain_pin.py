"""P0R.4R3: guard anti-deriva del pin de setuptools (toolchain de build/instalación).

La versión esperada se DERIVA de `lock_contracts.TOOLCHAIN["setuptools"]` (no se re-tipea aquí) y
debe aparecer, en su forma exacta, en las 8 fuentes autoritativas + el manifiesto. Rechaza: una
versión distinta en cualquier workflow, más de una versión de setuptools, un pin flotante (>=/~=),
la ausencia del pin y la reaparición de 81.0.0. Cierra la causa de deriva entre backend de build,
toolchain, contrato y workflows (la que dejó pasar PYSEC-2026-3447 hasta el gate post-merge).
"""

from __future__ import annotations

import re
from pathlib import Path

import tools.lock_contracts as lc

ROOT = lc.ROOT
EXPECTED = lc.TOOLCHAIN["setuptools"]  # fuente única, derivada
# forma `setuptools<op>version` (pyproject/deep.in/workflows); NO casa la forma dict `"setuptools": "…"`
_PINNED = re.compile(r"setuptools\s*([=<>!~]=?)\s*([0-9][0-9A-Za-z.\-]*)")

_SOURCES = (
    "pyproject.toml",
    "requirements/deep.in",
    "tools/make_locks.sh",
    "tools/lock_contracts.py",
    ".github/workflows/ci.yml",
    ".github/workflows/freeze_and_rebuild.yml",
)


def _text(rel: str) -> str:
    return (ROOT / rel).read_text()


def _no_comments(text: str) -> str:
    # todas las fuentes usan comentarios `#` (toml/sh/in/py/yml); el guard de PINS no debe leer
    # prosa como `setuptools<82` (que explica el conflicto de torch en un comentario).
    return "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())


def test_toolchain_equals_deep_direct():
    assert lc.DEEP_DIRECT["setuptools"] == EXPECTED


def test_pyproject_build_backend_pin():
    assert f'"setuptools=={EXPECTED}"' in _text("pyproject.toml")


def test_deep_in_pin():
    assert f"setuptools=={EXPECTED}" in _text("requirements/deep.in")


def test_make_locks_pin():
    assert f'SETUPTOOLS_VERSION="{EXPECTED}"' in _text("tools/make_locks.sh")


def test_workflow_bootstrap_counts():
    # 6 bootstraps en ci.yml: lint-and-test, model-tests, deep-lock-install, dvc-tool-install,
    # environment-contract y campaign-bundle-contract (Incremento 2: el contrato del bundle pinnea el toolchain).
    assert _text(".github/workflows/ci.yml").count(f'"setuptools=={EXPECTED}"') == 6
    assert _text(".github/workflows/freeze_and_rebuild.yml").count(f'"setuptools=={EXPECTED}"') == 1


def test_manifest_generator_pin():
    m = lc.load_json_no_dupes(ROOT / lc.MANIFEST_REL)
    assert m["generator"]["setuptools"] == EXPECTED


def test_single_version_no_floating_pin():
    # todo pin `setuptools<op>ver` en las fuentes (SIN comentarios) debe ser `==EXPECTED`; ni >=/~=
    for rel in _SOURCES:
        for op, ver in _PINNED.findall(_no_comments(_text(rel))):
            if ver.startswith("$"):  # setuptools==$SETUPTOOLS_VERSION en make_locks.sh
                continue
            assert op == "==", f"{rel}: pin flotante setuptools{op}{ver}"
            assert ver == EXPECTED, f"{rel}: setuptools=={ver} != {EXPECTED}"


def test_no_stale_81_anywhere():
    for rel in _SOURCES:
        t = _text(rel)
        assert "setuptools==81.0.0" not in t, f"{rel}: reaparece setuptools==81.0.0"
        assert '"setuptools": "81.0.0"' not in t, f"{rel}: reaparece dict setuptools 81.0.0"


def test_no_stale_81_in_committed_locks():
    # los locks tras `make lock` no deben conservar el pin viejo
    for p in (ROOT / "locks").glob("*.txt"):
        assert "setuptools==81.0.0" not in p.read_text(), f"{p.name}: setuptools==81.0.0 residual"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([Path(__file__).as_posix(), "-q"]))
