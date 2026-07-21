"""Gate AST anti-mutación-cruda (P0R.5 · Incremento 1R3 · B179/B180) + sanidad del contrato CSV del bundle. Enforce
que `tools/campaign_bundle.py` NO llame primitivas destructivas crudas (`os.unlink`/`remove`/`rename`/`replace`/
`rmdir`) — deben pasar por la cuarentena gobernada de `tools/governed_fs.py`."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

import pytest

import tools.check_raw_fs_mutations as gate


def test_bundle_layer_has_no_raw_destructive_mutations():
    assert gate.main() == 0


@pytest.mark.parametrize(
    "src",
    [
        "import os\ndef f(fd):\n    os.unlink('x', dir_fd=fd)\n",
        "import os as _o\ndef f(fd):\n    _o.rmdir('x', dir_fd=fd)\n",  # alias de módulo
        "from os import unlink\ndef f():\n    unlink('x')\n",  # from-import
        "from os import remove as rm\ndef f():\n    rm('x')\n",  # from-import con alias
        "import os\ndef f():\n    getattr(os, 'unlink')('x')\n",  # getattr
        "import shutil\ndef f():\n    shutil.rmtree('x')\n",  # shutil.rmtree
        "from pathlib import Path\ndef f(p):\n    Path(p).unlink()\n",  # Path.unlink
        "import os\ndef f():\n    os.system('rm -rf x')\n",  # os.system
        "import os\ndef f():\n    os.replace('a','b')\n",  # os.replace
    ],
)
def test_gate_catches_bypasses(src):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        assert gate._violations(path), f"el gate no detectó: {src!r}"
    finally:
        os.unlink(path)


def test_gate_allows_nondestructive_os():
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import os\ndef f(fd):\n    os.mkdir('x', dir_fd=fd)\n    os.fsync(fd)\n")
        path = fh.name
    try:
        assert not gate._violations(path), "falso positivo sobre os.mkdir/os.fsync"
    finally:
        os.unlink(path)


def test_csv_contract_is_well_formed():
    root = os.path.dirname(os.path.dirname(os.path.abspath(gate.__file__)))
    contract = json.loads(pathlib.Path(os.path.join(root, "security", "campaign_bundle_contract.json")).read_text())
    assert contract["encoding"] == "utf-8"
    assert isinstance(contract["columns"], list) and len(contract["columns"]) == 20
    assert contract["columns"][0] == "run_id" and contract["columns"][-1] == "source_run_id"
    assert set(contract["outputs"]) == {"campaign", "eval"}


@pytest.mark.parametrize(
    "src",
    [
        "import os\nf = os.unlink\ndef g():\n    f('x')\n",  # asignación de alias (B206)
        "import os\ndef g():\n    getattr(os, 'un' + 'link')('x')\n",  # getattr computado (B206)
        "import os\ndef g():\n    os.__dict__['unlink']('x')\n",  # acceso a __dict__ (B206)
        "def g():\n    __import__('os').unlink('x')\n",  # __import__ (B206)
    ],
)
def test_b206_gate_catches_indirection(src):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        assert gate._violations(path), f"el gate no detectó la indirección: {src!r}"
    finally:
        os.unlink(path)


@pytest.mark.parametrize(
    "src",
    [
        "def f():\n    open('victim', 'w')\n",  # B214: open en modo escritura
        "def f():\n    open('victim', 'a')\n",  # B214: modo append
        "import os\ndef f(d):\n    os.open('x', os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=d)\n",  # O_TRUNC
        "import os\ndef f(d, fl):\n    os.open('x', fl, dir_fd=d)\n",  # flags dinámicos
        "import os\ndef f(d):\n    os.open('x', os.O_WRONLY, dir_fd=d)\n",  # sin O_NOFOLLOW
    ],
)
def test_b214_gate_catches_destructive_open(src):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        assert gate._violations(path), f"el gate no detectó la apertura destructiva: {src!r}"
    finally:
        os.unlink(path)


def test_gate_allows_dataclasses_replace_but_not_fs_replace():
    # Incremento 2: `dataclasses.replace` (construye un frozen NUEVO) es SEGURO; `os.replace`/`Path(p).replace`
    # (renombre destructivo) siguen prohibidos.
    safe = "import dataclasses\ndef f(c):\n    return dataclasses.replace(c, x=1)\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(safe)
        path = fh.name
    try:
        assert not gate._violations(path), "falso positivo sobre dataclasses.replace"
    finally:
        os.unlink(path)
    for bad in ("import os\ndef f():\n    os.replace('a', 'b')\n", "from pathlib import Path\ndef f(p):\n    Path(p).replace('x')\n"):  # fmt: skip
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(bad)
            path = fh.name
        try:
            assert gate._violations(path), f"el gate no marcó un replace destructivo: {bad!r}"
        finally:
            os.unlink(path)


def test_b214_gate_allows_safe_reads():
    src = "import os\ndef f(d):\n    open('x', 'rb')\n    os.open('y', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=d)\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        assert not gate._violations(path), "falso positivo sobre lecturas seguras"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
