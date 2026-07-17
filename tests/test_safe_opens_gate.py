"""Gate AST de APERTURAS SEGURAS (P0R.5 · Incremento 1R7R · B218/B219). Enforce que TODA `os.open(...)` de la ruta
online del bundle no pueda COLGAR sobre un objeto especial (FIFO/socket/dispositivo) sustituido por nombre: las
lecturas/RW sobre nombres existentes exigen `O_NOFOLLOW | O_NONBLOCK`, las de dir exigen `O_DIRECTORY | O_NOFOLLOW`,
y las de creación exigen `O_CREAT | O_EXCL | O_NOFOLLOW`. Flags dinámicos o indirecciones → fail-closed."""

from __future__ import annotations

import os
import tempfile

import pytest

import tools.check_safe_opens as gate


def test_online_path_has_no_hangable_opens():
    assert gate.main() == 0


@pytest.mark.parametrize(
    "src",
    [
        "import os\ndef f(d):\n    os.open('x', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=d)\n",  # lectura sin O_NONBLOCK
        "import os\ndef f(d):\n    os.open('x', os.O_RDWR | os.O_NOFOLLOW, dir_fd=d)\n",  # RW sin O_NONBLOCK
        "import os\ndef f(d):\n    os.open('x', os.O_RDONLY | os.O_NONBLOCK, dir_fd=d)\n",  # sin O_NOFOLLOW
        "import os\ndef f(d):\n    os.open('x', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=d)\n",  # create sin O_EXCL
        "import os\ndef f(d):\n    os.open('x', os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)\n",  # O_TRUNC
        "import os\ndef f(d, fl):\n    os.open('x', fl, dir_fd=d)\n",  # flags dinámicos
        "import os\ndef f(d):\n    os.open('x')\n",  # sin flags
    ],
)
def test_gate_flags_hangable_opens(src):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        assert gate._violations(path), f"el gate no detectó la apertura insegura: {src!r}"
    finally:
        os.unlink(path)


@pytest.mark.parametrize(
    "src",
    [
        "import os\nf = os.open\ndef g(d):\n    f('x', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=d)\n",  # alias de os.open
        "import os\ndef g():\n    getattr(os, 'open')('x', 0)\n",  # getattr(os, 'open')
        "import os\ndef g():\n    os.__dict__['open']('x', 0)\n",  # os.__dict__
        "def g():\n    __import__('os').open('x', 0)\n",  # __import__
    ],
)
def test_gate_catches_bypasses(src):
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
        "import os\ndef f(d):\n    os.open('x', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)\n",  # lectura segura
        "import os\n_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW\ndef f(d):\n    os.open('x', _DIR, dir_fd=d)\n",  # dir por constante
        "import os\ndef f(d):\n    os.open('x', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=d)\n",  # create-only
        "import os\ndef f(d, c):\n    os.open('x', os.O_RDONLY | (os.O_NOFOLLOW | os.O_NONBLOCK), dir_fd=d)\n",  # BinOp anidado
        "import os\ndef f(d, p):\n    os.open('x', _D) if p else os.open('x', _D, dir_fd=d)\n",  # IfExp de dos os.open (cada uno visitado)
    ],
)
def test_gate_allows_safe_opens(src):
    prelude = "_D = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW\n" if "_D)" in src or "_D," in src else ""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src.replace("import os\n", "import os\n" + prelude, 1))
        path = fh.name
    try:
        assert not gate._violations(path), f"falso positivo sobre apertura segura: {src!r}"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
