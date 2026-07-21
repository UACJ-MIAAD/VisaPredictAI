"""Primitivas de rename atómico (P0R.5 · R9.2R10 · B121/B122/B123). Se ejercitan en Linux Y macOS (CI corre
`python -m tools.atomic_fs --selftest` en ambos runners; estas pruebas corren en el runner de pytest)."""

from __future__ import annotations

import os

import pytest

import tools.atomic_fs as afs


def _dfd(tmp_path):
    return os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)


def _write(dfd, name, data):
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dfd)
    os.write(fd, data)
    os.close(fd)


def _read(dfd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    try:
        return os.read(fd, 4096)
    finally:
        os.close(fd)


def test_supported_on_this_platform():
    assert afs.supported(), "la plataforma de CI/dev debe ofrecer renameat2/renameatx_np"


def test_selftest_exit_zero():
    assert afs._selftest() == 0


def test_noreplace_moves_to_absent(tmp_path):
    dfd = _dfd(tmp_path)
    try:
        _write(dfd, "a", b"AAA")
        afs.rename_noreplace(dfd, "a", dfd, "b")
        assert _read(dfd, "b") == b"AAA"
        assert not os.path.exists(tmp_path / "a")
    finally:
        os.close(dfd)


def test_noreplace_never_overwrites_collision(tmp_path):
    # B121: una colisión en el destino NO se sobrescribe — FileExistsError y AMBOS inodes intactos.
    dfd = _dfd(tmp_path)
    try:
        _write(dfd, "src", b"SRC")
        _write(dfd, "dst", b"DST-SURVIVES")
        with pytest.raises(FileExistsError):
            afs.rename_noreplace(dfd, "src", dfd, "dst")
        assert _read(dfd, "dst") == b"DST-SURVIVES", "rename_noreplace destruyó el destino (B121)"
        assert _read(dfd, "src") == b"SRC", "rename_noreplace tocó el origen en la colisión"
    finally:
        os.close(dfd)


def test_noreplace_missing_src_is_filenotfound(tmp_path):
    dfd = _dfd(tmp_path)
    try:
        with pytest.raises(FileNotFoundError):
            afs.rename_noreplace(dfd, "nope", dfd, "dst")
    finally:
        os.close(dfd)


def test_exchange_swaps_inodes(tmp_path):
    # B122/B123: el intercambio es atómico y NUNCA destruye — ambos inodes sobreviven, con contenidos cruzados.
    dfd = _dfd(tmp_path)
    try:
        _write(dfd, "a", b"AAA")
        _write(dfd, "b", b"BBB")
        afs.rename_exchange(dfd, "a", dfd, "b")
        assert _read(dfd, "a") == b"BBB" and _read(dfd, "b") == b"AAA"
    finally:
        os.close(dfd)


def test_exchange_missing_end_is_filenotfound(tmp_path):
    dfd = _dfd(tmp_path)
    try:
        _write(dfd, "a", b"AAA")
        with pytest.raises(FileNotFoundError):
            afs.rename_exchange(dfd, "a", dfd, "nope")
        assert _read(dfd, "a") == b"AAA", "un exchange fallido no debe tocar el origen"
    finally:
        os.close(dfd)


def test_cross_directory_noreplace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "dst").mkdir()
    sfd = os.open(str(tmp_path / "src"), os.O_RDONLY | os.O_DIRECTORY)
    dfd = os.open(str(tmp_path / "dst"), os.O_RDONLY | os.O_DIRECTORY)
    try:
        _write(sfd, "x", b"CROSS")
        afs.rename_noreplace(sfd, "x", dfd, "y")
        assert _read(dfd, "y") == b"CROSS"
        assert not (tmp_path / "src" / "x").exists()
    finally:
        os.close(sfd)
        os.close(dfd)
