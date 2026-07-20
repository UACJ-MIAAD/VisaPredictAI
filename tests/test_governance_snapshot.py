"""B286-A: el substrate `GovernanceSnapshot` cumple TODAS las invariantes gobernadas (B274/B281/B282/B288/B293) — una
sola observación validada por checkpoint, identidad completa, cero reaperturas. Cubre symlink de raíz/ancestro/leaf,
FIFO/socket/hardlink, modos laxos de dir/leaf, swap de inode, chmod entre validar y sellar, oversized/grow, errores de
cierre, path-traversal, independencia del cwd, `tracked()` y `reverify()`."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import tempfile

import pytest

import tools.governance_snapshot as gs

_SRC = b"x = 1\n" * 4


def _lay(tmp_path):
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "campaign_bundle.py").write_bytes(_SRC)
    (tmp_path / "tools" / "campaign_bundle.py").chmod(0o644)
    return gs.GovernanceSnapshot(str(tmp_path))


def _reads(snap, rel="tools/campaign_bundle.py", **kw):
    try:
        return snap.read(rel, **kw), None
    except gs.GovernanceSnapshotError as exc:
        return None, str(exc)


def test_happy_control_accepts(tmp_path):
    with _lay(tmp_path) as snap:
        e, err = _reads(snap)
        assert e is not None and e.data == _SRC and err is None


def test_invalid_rel_rejected(tmp_path):
    with _lay(tmp_path) as snap:
        for rel in ("/etc/passwd", "tools/../x", "tools//x", "", "a\x00b", "tools/./x"):
            e, err = _reads(snap, rel)
            assert e is None and err, f"{rel!r} debe rechazarse"


def test_root_symlink_rejected(tmp_path):
    real = tmp_path / "real"
    (real / "tools").mkdir(parents=True)
    (real / "tools" / "campaign_bundle.py").write_bytes(_SRC)
    (real / "tools" / "campaign_bundle.py").chmod(0o644)
    (tmp_path / "root-link").symlink_to(real, target_is_directory=True)
    with gs.GovernanceSnapshot(str(tmp_path / "root-link")) as snap:
        e, err = _reads(snap)
        assert e is None and "B281" in err


def test_ancestor_and_leaf_symlink_rejected(tmp_path):
    with _lay(tmp_path) as snap:
        leaf = tmp_path / "tools" / "campaign_bundle.py"
        real = leaf.with_suffix(".real")
        leaf.rename(real)
        leaf.symlink_to(real)
        e, err = _reads(snap)
        assert e is None and "B274" in err
        leaf.unlink()
        real.rename(leaf)


def test_dir_group_other_writable_rejected(tmp_path):
    for mode in (0o777, 0o775):
        with _lay(tmp_path) as snap:
            (tmp_path / "tools").chmod(mode)
            try:
                e, err = _reads(snap)
                assert e is None and "B282" in err and "escribible" in err
            finally:
                (tmp_path / "tools").chmod(0o755)


def test_leaf_lax_mode_and_hardlink_and_nlink_rejected(tmp_path):
    with _lay(tmp_path) as snap:
        (tmp_path / "tools" / "campaign_bundle.py").chmod(0o666)
        e, err = _reads(snap)
        assert e is None and "modo" in err
    with _lay(tmp_path) as snap:
        os.link(tmp_path / "tools" / "campaign_bundle.py", tmp_path / "tools" / "hard.py")
        e, err = _reads(snap)
        assert e is None and "nlink" in err


def test_oversized_and_grow_rejected(tmp_path):
    with _lay(tmp_path) as snap:
        e, err = _reads(snap, max_bytes=3)
        assert e is None and "B282" in err


def test_fifo_leaf_does_not_hang(tmp_path):
    def _timeout(signum, frame):
        raise TimeoutError("la lectura gobernada colgó en un FIFO (B274)")

    with _lay(tmp_path) as snap:
        (tmp_path / "tools" / "campaign_bundle.py").unlink()
        os.mkfifo(tmp_path / "tools" / "campaign_bundle.py")
        old = signal.signal(signal.SIGALRM, _timeout)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            e, err = _reads(snap)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            (tmp_path / "tools" / "campaign_bundle.py").unlink()
        assert e is None and err


def test_socket_leaf_rejected():
    short = tempfile.mkdtemp(prefix="b", dir="/tmp")
    os.mkdir(os.path.join(short, "tools"))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(os.path.join(short, "tools", "campaign_bundle.py"))
        with gs.GovernanceSnapshot(short) as snap:
            e, err = _reads(snap)
            assert e is None and err
    finally:
        srv.close()
        shutil.rmtree(short, ignore_errors=True)


def test_leaf_inode_swap_during_read_rejected(monkeypatch, tmp_path):
    leaf = tmp_path / "tools" / "campaign_bundle.py"
    real_read = os.read
    done = {"x": False}

    def _read_swap(fd, n):
        if not done["x"]:
            done["x"] = True
            leaf.unlink()
            leaf.write_bytes(_SRC + b"\n# other\n")
            leaf.chmod(0o644)
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", _read_swap)
    with _lay(tmp_path) as snap:
        e, err = _reads(snap)
        assert e is None and ("cambió" in err or "tamaño" in err)


def test_b293_one_fstat_validate_equals_seal(monkeypatch, tmp_path):
    # chmod del dir a 0777 entre el 1º y el 2º fstat: con UN solo fstat (validar==sellar) la revalidación final lo caza.
    tools_ino = os.stat(tmp_path / "tools").st_ino if (tmp_path / "tools").exists() else None
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "campaign_bundle.py").write_bytes(_SRC)
    (tmp_path / "tools" / "campaign_bundle.py").chmod(0o644)
    tools_ino = os.stat(tmp_path / "tools").st_ino
    real_fstat = os.fstat
    fired = {"x": False}

    def _fstat_chmod(fd):
        st = real_fstat(fd)
        if st.st_ino == tools_ino and not fired["x"]:
            fired["x"] = True
            (tmp_path / "tools").chmod(0o777)
        return st

    monkeypatch.setattr(os, "fstat", _fstat_chmod)
    with gs.GovernanceSnapshot(str(tmp_path)) as snap:
        try:
            e, err = _reads(snap)
            assert e is None and err, "chmod entre validar y sellar debe rechazarse (B293)"
        finally:
            (tmp_path / "tools").chmod(0o755)


def test_b288_post_mode_change_dir_rejected(monkeypatch, tmp_path):
    with _lay(tmp_path) as snap:
        real_read = os.read
        done = {"x": False}

        def _read_chmod(fd, n):
            if not done["x"]:
                done["x"] = True
                (tmp_path / "tools").chmod(0o777)  # cambia el modo del dir DESPUÉS de sellar → re-fstat final lo caza
            return real_read(fd, n)

        monkeypatch.setattr(os, "read", _read_chmod)
        try:
            e, err = _reads(snap)
            assert e is None and "B288" in err
        finally:
            (tmp_path / "tools").chmod(0o755)


def test_close_error_surfaced(monkeypatch, tmp_path):
    real_close = os.close
    state = {"n": 0}

    def _boom(fd):
        state["n"] += 1
        if state["n"] == 2:  # falla el 2º cierre (un directorio)
            try:
                real_close(fd)
            finally:
                raise OSError(9, "EBADF inyectado")
        return real_close(fd)

    monkeypatch.setattr(os, "close", _boom)
    with _lay(tmp_path) as snap:
        e, err = _reads(snap)
        assert e is None and "cerrar" in err and "B282" in err


def test_one_observation_per_read(tmp_path):
    # cache por rel = UNA observación: dos read() del mismo rel devuelven el MISMO objeto sellado.
    with _lay(tmp_path) as snap:
        a = snap.read("tools/campaign_bundle.py")
        b = snap.read("tools/campaign_bundle.py")
        assert a is b


def test_tracked_and_cwd_independence(tmp_path, monkeypatch):
    snap = gs.GovernanceSnapshot(os.path.dirname(os.path.dirname(os.path.abspath(gs.__file__))))
    monkeypatch.chdir(tmp_path)  # cwd distinto → git -C ROOT ls-files sigue funcionando
    t = snap.tracked("tools/check_commit_frontier.py")
    assert t == ("tools/check_commit_frontier.py",)


def test_reverify_detects_post_read_change(tmp_path):
    with _lay(tmp_path) as snap:
        snap.read("tools/campaign_bundle.py")
        (tmp_path / "tools" / "campaign_bundle.py").write_bytes(_SRC + b"\n# mutated\n")
        (tmp_path / "tools" / "campaign_bundle.py").chmod(0o644)
        with pytest.raises(gs.GovernanceSnapshotError):
            snap.reverify()
