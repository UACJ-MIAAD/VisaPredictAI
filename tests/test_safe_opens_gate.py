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
        "from tools.governed_fs import GovernedQuarantine\n",  # nuevo módulo que usa la cuarentena online
        "import tools.campaign_bundle as cb\n",  # importa la maquinaria del bundle
        "import tools.merge_campaign_pools\n",  # importa el merge
    ],
)
def test_inventory_flags_new_online_module(src):
    import ast as _ast

    problems = gate._online_import_problems("tools/nuevo_online.py", _ast.parse(src))
    assert problems, f"el inventario no marcó un módulo online nuevo: {src!r}"


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
        "def f():\n    open('c.json', 'rb').read()\n",  # builtin open (lectura por ruta sin gobernar)
        "o = open\ndef f():\n    o('c', 'rb')\n",  # alias del builtin open
        "import io\ndef f():\n    io.open('c', 'rb')\n",  # io.open
        "from io import open\ndef f():\n    open('c')\n",  # from io import open
        "from pathlib import Path\ndef f(p):\n    Path(p).read_bytes()\n",  # Path(...).read_bytes()
        "def f(p):\n    p.read_text()\n",  # <x>.read_text()
        "import os\ndef f(d):\n    os.open('x', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)\n",  # lectura O_RDONLY fuera de la fuente única
    ],
)
def test_gate_flags_ungoverned_reads(src):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        assert gate._violations(path), f"el gate no detectó la lectura no gobernada: {src!r}"
    finally:
        os.unlink(path)


def test_gate_allows_read_only_inside_source_helper():
    # una os.open de LECTURA es válida SÓLO dentro de la fuente única real (por (fichero, función)).
    src = "import os\ndef opened_regular_noblock_at(d, n):\n    os.open(n, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)\n"
    import ast as _ast

    sc = gate._Scanner("tools/governed_read.py")  # el path del registro positivo
    tree = _ast.parse(src)
    sc.prescan(tree)
    sc.visit(tree)
    assert not sc.problems, f"falso positivo dentro de la fuente única: {sc.problems}"
    sc2 = gate._Scanner("tools/merge_campaign_pools.py")  # MISMO código en otro fichero → prohibido
    sc2.prescan(tree)
    sc2.visit(tree)
    assert sc2.problems, "una lectura O_RDONLY en un fichero que no es la fuente única debe fallar"


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
        "import os\n_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW\ndef f(d):\n    os.open('x', _DIR, dir_fd=d)\n",  # dir por constante
        "import os\ndef f(d):\n    os.open('x', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=d)\n",  # create-only
        "import os\ndef f(d):\n    os.open('l', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)\n",  # lock RW inline (no lee contenido)
        "import os\n_D = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW\ndef f(d, p):\n    os.open('x', _D) if p else os.open('x', _D, dir_fd=d)\n",  # IfExp de dos os.open de dir
    ],
)
def test_gate_allows_safe_opens(src):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        assert not gate._violations(path), f"falso positivo sobre apertura segura: {src!r}"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
