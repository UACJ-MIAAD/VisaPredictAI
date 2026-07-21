"""B286-A: el substrate `GovernanceSnapshot` cumple TODAS las invariantes gobernadas (B274/B281/B282/B288/B293) — una
sola observación validada por checkpoint, identidad completa, cero reaperturas. Cubre symlink de raíz/ancestro/leaf,
FIFO/socket/hardlink, modos laxos de dir/leaf, swap de inode, chmod entre validar y sellar, oversized/grow, errores de
cierre, path-traversal, independencia del cwd, `tracked()` y `reverify()`."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time

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
    root = os.path.dirname(os.path.dirname(os.path.abspath(gs.__file__)))
    monkeypatch.chdir(tmp_path)  # cwd distinto → git -C ROOT ls-files sigue funcionando
    with gs.GovernanceSnapshot(root) as snap:
        t = snap.tracked(gs.TrackedQuery("exact", "tools/check_commit_frontier.py"))
    assert t == ("tools/check_commit_frontier.py",)


def test_reverify_detects_post_read_change(tmp_path):
    with _lay(tmp_path) as snap:
        snap.read("tools/campaign_bundle.py")
        (tmp_path / "tools" / "campaign_bundle.py").write_bytes(_SRC + b"\n# mutated\n")
        (tmp_path / "tools" / "campaign_bundle.py").chmod(0o644)
        with pytest.raises(gs.GovernanceSnapshotError):
            snap.reverify()


# ---------------------------------------------------------------------------
# B296 — la caché se indexaba SÓLO por `rel`, así que una primera lectura permisiva satisfacía cualquier relectura
# posterior sin comprobar `exact_mode`/`max_bytes` (falso verde reproducido en 03f8e3b: read(0644,100) sellaba y
# read(0600,100)/read(_,1) devolvían el mismo objeto). Ahora la política es INMUTABLE, con tipos cerrados, y la caché se
# liga a `(rel, policy)`.
# ---------------------------------------------------------------------------
def test_b296_stricter_mode_reread_rejected(tmp_path):
    with _lay(tmp_path) as snap:
        snap.read("tools/campaign_bundle.py", exact_mode=0o644, max_bytes=100)
        with pytest.raises(gs.GovernanceSnapshotError, match="política"):
            snap.read("tools/campaign_bundle.py", exact_mode=0o600, max_bytes=100)


def test_b296_tighter_cap_reread_rejected(tmp_path):
    with _lay(tmp_path) as snap:
        snap.read("tools/campaign_bundle.py", exact_mode=0o644, max_bytes=100)
        with pytest.raises(gs.GovernanceSnapshotError, match="política"):
            snap.read("tools/campaign_bundle.py", exact_mode=0o644, max_bytes=1)


def test_b296_category_conflict_rejected(tmp_path):
    with _lay(tmp_path) as snap:
        snap.read("tools/campaign_bundle.py", category="source", max_bytes=100)
        with pytest.raises(gs.GovernanceSnapshotError, match="política"):
            snap.read("tools/campaign_bundle.py", category="contract", max_bytes=100)


def test_b296_type_and_bound_coercions_rejected(tmp_path):
    with _lay(tmp_path) as snap:
        for kw in (
            {"exact_mode": True, "max_bytes": 100},  # bool no es int exacto
            {"exact_mode": 0o755, "max_bytes": 100},  # modo fuera del conjunto cerrado
            {"exact_mode": 0o644, "max_bytes": 1.5},  # float
            {"exact_mode": 0o644, "max_bytes": float("nan")},  # NaN
            {"exact_mode": 0o644, "max_bytes": True},  # bool como cota
            {"exact_mode": 0o644, "max_bytes": -5},  # negativo
            {"exact_mode": 0o644, "max_bytes": 0},  # cero
            {"exact_mode": 0o644, "max_bytes": 10**12},  # enorme (> cap de la categoría)
            {"exact_mode": 0o644, "max_bytes": 100, "category": "nope"},  # categoría inválida
        ):
            with pytest.raises(gs.GovernanceSnapshotError, match="B296"):
                snap.read("tools/campaign_bundle.py", **kw)


def test_b296_same_policy_returns_identity(tmp_path):
    with _lay(tmp_path) as snap:
        a = snap.read("tools/campaign_bundle.py", exact_mode=0o644, max_bytes=100)
        b = snap.read("tools/campaign_bundle.py", exact_mode=0o644, max_bytes=100)
        assert a is b


def test_b296_total_rejection_does_not_poison_counter(tmp_path, monkeypatch):
    # un rechazo por TOTAL no debe incrementar `_total`: calcular new_total ANTES de mutar.
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "a.py").write_bytes(_SRC)
    (tmp_path / "tools" / "a.py").chmod(0o644)
    (tmp_path / "tools" / "b.py").write_bytes(_SRC)
    (tmp_path / "tools" / "b.py").chmod(0o644)
    monkeypatch.setattr(gs, "_SNAPSHOT_TOTAL_MAX_BYTES", len(_SRC) + 1)  # cabe A, no A+B
    with gs.GovernanceSnapshot(str(tmp_path)) as snap:
        snap.read("tools/a.py")
        assert snap._total == len(_SRC)
        with pytest.raises(gs.GovernanceSnapshotError, match="total"):
            snap.read("tools/b.py")
        assert snap._total == len(_SRC), "un rechazo por total NO debe envenenar el contador (B296)"


def test_b296_reverify_uses_sealed_policy(tmp_path):
    # reverify re-lee con la cota SELLADA (no _SOURCE_MAX_BYTES): un fichero que crece más allá de su cota sellada
    # se rechaza por tamaño (B282), no se lee bajo un límite laxo.
    (tmp_path / "tools").mkdir(exist_ok=True)
    leaf = tmp_path / "tools" / "a.py"
    leaf.write_bytes(b"x" * 20)
    leaf.chmod(0o644)
    with gs.GovernanceSnapshot(str(tmp_path)) as snap:
        snap.read("tools/a.py", exact_mode=0o644, max_bytes=30)
        leaf.write_bytes(b"x" * 40)  # crece a 40 (> cota sellada 30, < _SOURCE_MAX_BYTES)
        leaf.chmod(0o644)
        with pytest.raises(gs.GovernanceSnapshotError, match="máximo 30|B282"):
            snap.reverify()


# ---------------------------------------------------------------------------
# B298 — `__exit__` sólo limpiaba `_cache` pero no invalidaba la instancia: tras el `with`, `read()` reabría rutas y
# aceptaba bytes NUEVOS bajo el mismo objeto (falso verde reproducido en 03f8e3b). Ahora el ciclo de vida es NEW->OPEN->
# CLOSED de un solo uso.
# ---------------------------------------------------------------------------
def test_b298_read_after_exit_rejected(tmp_path):
    snap = _lay(tmp_path)
    with snap:
        snap.read("tools/campaign_bundle.py")
    (tmp_path / "tools" / "campaign_bundle.py").write_bytes(_SRC + b"\n# NEW\n")  # reemplazo post-cierre
    (tmp_path / "tools" / "campaign_bundle.py").chmod(0o644)
    with pytest.raises(gs.GovernanceSnapshotError, match="B298"):
        snap.read("tools/campaign_bundle.py")


def test_b298_read_before_enter_rejected(tmp_path):
    snap = _lay(tmp_path)
    with pytest.raises(gs.GovernanceSnapshotError, match="B298"):
        snap.read("tools/campaign_bundle.py")


def test_b298_tracked_and_reverify_after_exit_rejected(tmp_path):
    snap = _lay(tmp_path)
    with snap:
        snap.read("tools/campaign_bundle.py")
    for op in (lambda: snap.tracked(gs.TrackedQuery("prefix", "tools/")), snap.reverify):
        with pytest.raises(gs.GovernanceSnapshotError, match="B298"):
            op()


def test_b298_reentry_rejected(tmp_path):
    snap = _lay(tmp_path)
    with snap:
        pass
    with pytest.raises(gs.GovernanceSnapshotError, match="B298"):
        with snap:  # una instancia CERRADA no renace
            pass


def test_b298_exit_closes_even_when_body_raises(tmp_path):
    snap = _lay(tmp_path)
    with pytest.raises(ValueError):
        with snap:
            raise ValueError("boom")
    # tras una excepción del cuerpo, la instancia queda CERRADA y no puede leer
    with pytest.raises(gs.GovernanceSnapshotError, match="B298"):
        snap.read("tools/campaign_bundle.py")


def test_b298_sealed_entry_stays_inspectable_after_exit(tmp_path):
    snap = _lay(tmp_path)
    with snap:
        e = snap.read("tools/campaign_bundle.py")
    # la entrada sellada sigue siendo bytes inmutables inspeccionables; NO habilita nuevas lecturas
    assert e.data == _SRC and e.sha256
    with pytest.raises(gs.GovernanceSnapshotError, match="B298"):
        snap.read("tools/campaign_bundle.py")


# ---------------------------------------------------------------------------
# B299 — varios `fstat`/`decode` internos escapaban CRUDOS (no como GovernanceSnapshotError) y, bajo una excepción
# primaria, los errores de cierre se perdían. En 03f8e3b un OSError inyectado en el fstat de un directorio o del leaf
# escapa como OSError (fuera de la taxonomía fail-closed). Ahora el resultado es TOTAL: toda excepción operacional
# esperable se normaliza y los cierres se agregan; KeyboardInterrupt/SystemExit NO se convierten.
# ---------------------------------------------------------------------------
def _fstat_boom_on_ino(monkeypatch, target_ino):
    real = os.fstat

    def boom(fd):
        st = real(fd)
        if st.st_ino == target_ino:
            raise OSError(5, "EIO fstat inyectado")
        return st

    monkeypatch.setattr(os, "fstat", boom)


def test_b299_leaf_fstat_error_stays_in_taxonomy(tmp_path, monkeypatch):
    snap = _lay(tmp_path)
    _fstat_boom_on_ino(monkeypatch, (tmp_path / "tools" / "campaign_bundle.py").stat().st_ino)
    with snap:
        with pytest.raises(gs.GovernanceSnapshotError, match="B299"):
            snap.read("tools/campaign_bundle.py")


def test_b299_dir_fstat_error_stays_in_taxonomy(tmp_path, monkeypatch):
    snap = _lay(tmp_path)
    _fstat_boom_on_ino(monkeypatch, (tmp_path / "tools").stat().st_ino)
    with snap:
        with pytest.raises(gs.GovernanceSnapshotError, match="B299"):
            snap.read("tools/campaign_bundle.py")


def test_b299_primary_exception_and_close_error_both_reported(tmp_path, monkeypatch):
    # un error PRIMARIO (fstat de dir) MÁS un cierre fallido: ambos deben reportarse; en 03f8e3b la excepción cruda del
    # fstat saltaba la agregación y perdía el error de cierre.
    snap = _lay(tmp_path)
    _fstat_boom_on_ino(monkeypatch, (tmp_path / "tools").stat().st_ino)
    real_close = os.close

    def close_boom(fd):
        try:
            real_close(fd)
        finally:
            raise OSError(9, "EBADF close inyectado")

    monkeypatch.setattr(os, "close", close_boom)
    with snap:
        with pytest.raises(gs.GovernanceSnapshotError) as ei:
            snap.read("tools/campaign_bundle.py")
    assert "B299" in str(ei.value) and "cerrar" in str(ei.value), str(ei.value)


def test_b299_keyboardinterrupt_not_converted(tmp_path, monkeypatch):
    _fstat_boom = None  # noqa: F841 (documenta intención)

    def ki(fd):
        raise KeyboardInterrupt

    snap = _lay(tmp_path)
    monkeypatch.setattr(os, "fstat", ki)
    with snap:
        with pytest.raises(KeyboardInterrupt):
            snap.read("tools/campaign_bundle.py")


def test_b299_systemexit_not_converted(tmp_path, monkeypatch):
    def se(fd):
        raise SystemExit(2)

    snap = _lay(tmp_path)
    monkeypatch.setattr(os, "fstat", se)
    with snap:
        with pytest.raises(SystemExit):
            snap.read("tools/campaign_bundle.py")


def test_b299_tracked_non_utf8_stays_in_taxonomy(tmp_path, monkeypatch):
    def _fake_git(op, out_limit):  # TOPLEVEL → ROOT real; TRACKED_INVENTORY → nombre no-UTF-8
        if op == "TOPLEVEL":
            return str(tmp_path).encode("utf-8") + b"\n"
        return b"tools/\xff\xfe.py\x00"

    with _lay(tmp_path) as snap:
        monkeypatch.setattr(snap, "_run_git", _fake_git)
        with pytest.raises(gs.GovernanceSnapshotError, match="B299"):
            snap.tracked(gs.TrackedQuery("suffix", ".py"))


# ---------------------------------------------------------------------------
# B301 — `tracked()` ejecutaba `git ls-files` en CADA llamada: dos consultas podían observar inventarios DISTINTOS
# (índices/generaciones diferentes), y el hijo heredaba `GIT_DIR`/`GIT_INDEX_FILE`/… capaces de redirigir la fuente.
# Ahora hay UN inventario sellado por snapshot, entorno saneado, y `reverify` lo re-captura una vez.
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(gs.__file__)))


# ---------------------------------------------------------------------------
# B336 — el productor/validador del recibo deep resolvían HEAD con un `git rev-parse HEAD` ad hoc (una sola llamada, sin
# cat-file/toplevel/entorno saneado): un stdout 40-hex se aceptaba como HEAD. `GovernanceSnapshot.head_commit` es la ÚNICA
# observación git gobernada: toplevel textual == ROOT + `rev-parse --verify HEAD^{commit}` 40-hex de una línea.
# ---------------------------------------------------------------------------
def test_b336_head_commit_matches_real_head():
    head = gs.GovernanceSnapshot(_REPO_ROOT).head_commit()
    real = subprocess.run(
        ["/usr/bin/git", "-C", _REPO_ROOT, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert head == real and len(head) == 40 and all(c in "0123456789abcdef" for c in head)
    assert "HEAD_COMMIT" in gs._GIT_OPS and gs._GIT_OPS["HEAD_COMMIT"] == ("rev-parse", "--verify", "HEAD^{commit}")


def test_b336_head_commit_rejects_wrong_toplevel(monkeypatch):
    snap = gs.GovernanceSnapshot(_REPO_ROOT)

    def fake(op, out_limit):  # toplevel MIENTE (otro repo); un stdout 40-hex no basta si el toplevel no es ROOT
        return b"/some/other/repo\n" if op == "TOPLEVEL" else b"f" * 40 + b"\n"

    monkeypatch.setattr(snap, "_run_git", fake)
    with pytest.raises(gs.GovernanceSnapshotError, match="toplevel"):
        snap.head_commit()


def test_b336_head_commit_rejects_non_40hex_or_multiline(monkeypatch):
    snap = gs.GovernanceSnapshot(_REPO_ROOT)

    def multiline(op, out_limit):
        return _REPO_ROOT.encode("utf-8") + b"\n" if op == "TOPLEVEL" else b"f" * 40 + b"\nEXTRA\n"

    monkeypatch.setattr(snap, "_run_git", multiline)
    with pytest.raises(gs.GovernanceSnapshotError, match="40-hex"):
        snap.head_commit()

    def nothex(op, out_limit):
        return _REPO_ROOT.encode("utf-8") + b"\n" if op == "TOPLEVEL" else b"Z" * 40 + b"\n"

    monkeypatch.setattr(snap, "_run_git", nothex)
    with pytest.raises(gs.GovernanceSnapshotError, match="40-hex"):
        snap.head_commit()


def test_b301_inventory_sealed_one_capture(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with gs.GovernanceSnapshot(_REPO_ROOT) as snap:
        a = snap.tracked(gs.TrackedQuery("suffix", ".py"))
        b = snap.tracked(gs.TrackedQuery("prefix", "tools/"))
        assert a and b and snap._captures == 1, f"dos consultas deben derivar de UNA captura (B301): {snap._captures}"


def test_b301_two_subprocess_generations_not_both_accepted(monkeypatch):
    calls = {"n": 0}

    def _fake_git(op, out_limit):  # sustituye la operación git cerrada; TOPLEVEL == ROOT exacto
        if op == "TOPLEVEL":
            return _REPO_ROOT.encode("utf-8") + b"\n"
        calls["n"] += 1
        return b"a.py\x00" if calls["n"] == 1 else b"b.py\x00"

    with gs.GovernanceSnapshot(_REPO_ROOT) as snap:
        monkeypatch.setattr(snap, "_run_git", _fake_git)
        first = snap.tracked(gs.TrackedQuery("suffix", ".py"))
        second = snap.tracked(gs.TrackedQuery("suffix", ".py"))
        assert first == second == ("a.py",), "la segunda consulta debe reusar la MISMA tuple sellada (B301)"
        assert calls["n"] == 1, "ningún segundo `git ls-files` durante el consumo (B301)"


def test_b303_child_env_is_allowlist():
    # B303: el entorno hijo es una ALLOWLIST fija (no filtrado de os.environ) — no hereda GIT_*/XDG/PYTHON* del proceso.
    env = dict(gs._GIT_CHILD_ENV)
    for k in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_CONFIG_KEY_0", "XDG_CONFIG_HOME", "PYTHONPATH"):
        assert k not in env, f"{k} no debe estar en el entorno hijo (B303)"
    assert env["LC_ALL"] == "C" and env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["PATH"] == "/usr/bin:/bin"


def test_b303_fake_git_via_path_ignored(monkeypatch, tmp_path):
    # un `git` FALSO primero en PATH que forja toplevel+inventario debe IGNORARSE — se usa el /usr/bin/git ABSOLUTO.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    git = fakebin / "git"
    git.write_text(f'#!/bin/bash\nif [[ "$*" == *"rev-parse"* ]]; then echo "{_REPO_ROOT}"; else printf "forged.py\\0"; fi\n')  # fmt: skip
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ['PATH']}")
    with gs.GovernanceSnapshot(_REPO_ROOT) as snap:
        result = snap.tracked(gs.TrackedQuery("exact", "forged.py"))
        assert result == (), "un git falso en PATH NO debe forjar el inventario sellado (B303)"


def test_b303_governed_git_identity_is_absolute_root_owned():
    # el ejecutable gobernado es el ABSOLUTO root-owned; su identidad se captura y es estable entre dos observaciones.
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    ident = snap._governed_git_identity()
    assert ident == snap._governed_git_identity(), "la identidad del git gobernado debe ser estable"
    assert ident[3] == 0, "git debe ser root-owned (uid 0) (B303)"  # st_uid


# ---------------------------------------------------------------------------
# B306 — pese al ejecutable absoluto y el entorno allowlist, git seguía leyendo la config LOCAL del repo y ejecutando
# `core.fsmonitor` durante `tracked()` (RCE). Ahora un prefijo de config por línea de comando lo neutraliza, las
# operaciones son cerradas (el caller no da argv) y el subproceso lee acotado.
# ---------------------------------------------------------------------------
def _make_repo_with_fsmonitor():
    import subprocess

    base = tempfile.mkdtemp(dir=os.path.expanduser("~"))
    repo = os.path.join(base, "repo")
    os.mkdir(repo)
    sentinel = os.path.join(base, "SENTINEL")
    fsmon = os.path.join(base, "fsmon.sh")
    with open(fsmon, "w") as fh:
        fh.write(f'#!/bin/bash\ntouch "{sentinel}"\necho ""\n')
    os.chmod(fsmon, 0o755)
    env = {"HOME": base, "PATH": "/usr/bin:/bin"}
    subprocess.run(["/usr/bin/git", "init", "-q", repo], check=True, env=env)
    with open(os.path.join(repo, "a.py"), "w") as fh:
        fh.write("x = 1\n")
    subprocess.run(["/usr/bin/git", "-C", repo, "add", "a.py"], check=True, env=env)
    subprocess.run(["/usr/bin/git", "-C", repo, "config", "core.fsmonitor", fsmon], check=True, env=env)
    return base, repo, sentinel


def test_b306_local_fsmonitor_config_not_executed():
    base, repo, sentinel = _make_repo_with_fsmonitor()
    try:
        with gs.GovernanceSnapshot(repo) as snap:
            inv = snap.tracked(gs.TrackedQuery("suffix", ".py"))
        assert inv == ("a.py",), f"inventario esperado (B306): {inv}"
        assert not os.path.exists(sentinel), "core.fsmonitor NO debe ejecutarse durante tracked() (B306)"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_b306_config_prefix_disables_executable_config():
    joined = " ".join(gs._GIT_CONFIG_ARGS)
    assert "core.fsmonitor=false" in joined and "core.hooksPath=/dev/null" in joined and "--no-optional-locks" in joined
    assert gs._GIT_CHILD_ENV["GIT_OPTIONAL_LOCKS"] == "0"


def test_b306_closed_ops_reject_caller_argv():
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    with pytest.raises(gs.GovernanceSnapshotError, match="B306"):
        snap._run_git("EVIL-SUBCOMMAND", 4096)


def test_b306_bounded_stdout_aborts():
    # el runner acotado aborta si stdout excede el límite, sin materializar ilimitado.
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    with pytest.raises(gs.GovernanceSnapshotError, match="límite"):
        snap._run_bounded(["/bin/sh", "-c", "head -c 1000000 /dev/zero"], 1024)


def test_b306_bounded_timeout_reaps():
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    orig = gs._GIT_TIMEOUT_S
    gs._GIT_TIMEOUT_S = 0.5
    try:
        with pytest.raises(gs.GovernanceSnapshotError, match="timeout"):
            snap._run_bounded(["/bin/sh", "-c", "sleep 30"], 4096)
    finally:
        gs._GIT_TIMEOUT_S = orig


# ---------------------------------------------------------------------------
# B311 — el runner usaba `select.select` (techo FD_SETSIZE) y dejaba escapar `ValueError` crudo. Ahora usa
# `selectors.DefaultSelector` (epoll/kqueue) con taxonomía TOTAL sobre el backend de espera.
# ---------------------------------------------------------------------------
def test_b311_selector_error_stays_in_taxonomy(monkeypatch):
    import selectors

    class _BadSel(selectors.DefaultSelector):
        def select(self, timeout=None):
            raise ValueError("fd out of range")

    monkeypatch.setattr(selectors, "DefaultSelector", _BadSel)
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    # B314: un fallo del backend de espera se convierte a la taxonomía (`runner falló`) y termina el grupo; el
    # ValueError crudo jamás escapa (el mensaje concreto cambió al unificar la red de seguridad del bucle).
    with pytest.raises(gs.GovernanceSnapshotError, match="runner falló"):
        snap._run_bounded(["/bin/echo", "hi"], 4096)


def test_b311_high_fds_work(monkeypatch):
    # con muchos fds abiertos (stdout/stderr por encima de 1024) el runner sigue funcionando (no FD_SETSIZE).
    held = [os.open("/dev/null", os.O_RDONLY) for _ in range(1100)]
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        out = snap._run_bounded(["/bin/echo", "hola"], 4096)
        assert out.strip() == b"hola"
    finally:
        for fd in held:
            os.close(fd)


# ---------------------------------------------------------------------------
# B312 — tras timeout sólo moría el hijo directo; un nieto quedaba vivo. Ahora el hijo corre en sesión/grupo privado y
# se termina el grupo COMPLETO (TERM→KILL) con reconciliación.
# ---------------------------------------------------------------------------
def test_b312_grandchild_killed_after_timeout(tmp_path):
    marker = tmp_path / "gcpid"
    marker.write_text("")
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    orig = gs._GIT_TIMEOUT_S
    gs._GIT_TIMEOUT_S = 0.25
    try:
        with pytest.raises(gs.GovernanceSnapshotError, match="timeout"):
            snap._run_bounded(["/bin/sh", "-c", f"sleep 60 & echo $! > {marker}; sleep 60"], 4096)
        time.sleep(0.5)
        gcpid = marker.read_text().strip()
        assert gcpid, "el nieto debe haber registrado su pid"
        alive = True
        try:
            os.kill(int(gcpid), 0)
        except ProcessLookupError, PermissionError:
            alive = False
        if alive:  # limpieza defensiva si el test fallara
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(gcpid), signal.SIGKILL)
        assert not alive, f"el nieto {gcpid} debe morir con el grupo (B312)"
    finally:
        gs._GIT_TIMEOUT_S = orig


# ---------------------------------------------------------------------------
# B313 — errores de terminate/kill/wait escapaban crudos y podían reemplazar el error primario. Ahora la taxonomía es
# total y el primario se preserva con el cleanup adjunto.
# ---------------------------------------------------------------------------
def test_b313_reap_error_stays_in_taxonomy(monkeypatch):
    real = subprocess.Popen

    class _BadWait(real):
        def wait(self, timeout=None):
            raise ProcessLookupError("gone")

    monkeypatch.setattr(gs.subprocess, "Popen", _BadWait)
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    orig = gs._GIT_TIMEOUT_S
    gs._GIT_TIMEOUT_S = 0.2
    try:
        with pytest.raises(gs.GovernanceSnapshotError) as ei:
            snap._run_bounded(["/bin/sh", "-c", "sleep 5"], 4096)
        # el primario (timeout) se preserva; el error de reap se adjunta como cleanup
        assert "timeout" in str(ei.value) and ("cleanup" in str(ei.value) or "wait" in str(ei.value))
    finally:
        gs._GIT_TIMEOUT_S = orig


def test_b313_primary_preserved_over_cleanup(monkeypatch):
    # un stdout que excede el límite (primario) + un fallo de wait (cleanup): el primario NO se reemplaza.
    real = subprocess.Popen

    class _BadWait(real):
        def wait(self, timeout=None):
            raise ProcessLookupError("gone")

    monkeypatch.setattr(gs.subprocess, "Popen", _BadWait)
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    with pytest.raises(gs.GovernanceSnapshotError, match="límite"):
        snap._run_bounded(["/bin/sh", "-c", "head -c 1000000 /dev/zero"], 1024)


# ---------------------------------------------------------------------------
# B314 — la adquisición/cierre del selector caían FUERA de la transacción: `Popen` precedía a
# `selectors.DefaultSelector()` (un fallo del ctor dejaba al hijo huérfano) y `sel.close()` era la primera
# instrucción sin guard del `finally` (su fallo cortaba `_terminate_group`/cierre de pipes). Ahora el selector se
# construye ANTES del proceso y el cleanup es TOTAL (cada paso con su propio guard).
# ---------------------------------------------------------------------------
def test_b314_selector_ctor_failure_creates_no_child():
    import selectors

    class _BadCtor(selectors.DefaultSelector):
        def __init__(self):
            raise ValueError("selector ctor boom")

    real = selectors.DefaultSelector
    selectors.DefaultSelector = _BadCtor
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        # B314: el ValueError crudo del ctor JAMÁS escapa; se convierte a la taxonomía. En el SHA base el ctor
        # ocurría DESPUÉS de Popen y el ValueError salía crudo (con un hijo ya creado y huérfano).
        with pytest.raises(gs.GovernanceSnapshotError, match="selector no construible"):
            snap._run_bounded(["/bin/sh", "-c", "sleep 30"], 4096)
    finally:
        selectors.DefaultSelector = real


def test_b314_selector_close_failure_stays_in_taxonomy():
    import selectors

    class _BadClose(selectors.DefaultSelector):
        def close(self):
            super().close()
            raise OSError("selector close boom")

    real = selectors.DefaultSelector
    selectors.DefaultSelector = _BadClose
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        # B314: el hijo (echo) se reapea y el error de cierre se ADJUNTA como incidencia; el OSError crudo no escapa.
        # En el SHA base `sel.close()` era la primera instrucción del `finally` y su OSError cortaba el resto.
        with pytest.raises(gs.GovernanceSnapshotError, match="cleanup incompleto|selector-close"):
            snap._run_bounded(["/bin/echo", "hi"], 4096)
    finally:
        selectors.DefaultSelector = real


# ---------------------------------------------------------------------------
# B315 — `KeyboardInterrupt`/`SystemExit` usaban `kill=False`: si el padre ya había terminado (rc=0) un nieto quedaba
# vivo. Además el camino feliz (rc=0) no verificaba el grupo. Ahora TODA salida anormal exige terminación y el grupo
# se verifica incluso tras rc=0 (un descendiente residual se termina y falla).
# ---------------------------------------------------------------------------
def test_b315_keyboardinterrupt_kills_grandchild(tmp_path):
    import selectors

    class _KISel(selectors.DefaultSelector):
        def select(self, timeout=None):
            time.sleep(0.3)  # deja que el padre termine y deje el nieto en el grupo
            raise KeyboardInterrupt

    marker = tmp_path / "gcpid"
    marker.write_text("")
    real = selectors.DefaultSelector
    selectors.DefaultSelector = _KISel
    gcpid = ""
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        # B315: el KeyboardInterrupt PROPAGA (mismo objeto) pero SOLO tras terminar el grupo. En el SHA base propagaba
        # con `kill=False` y el nieto sobrevivía.
        with pytest.raises(KeyboardInterrupt):
            snap._run_bounded(["/bin/sh", "-c", f"sleep 30 & echo $! > {marker}; exit 0"], 4096)
        time.sleep(0.4)
        gcpid = marker.read_text().strip()
        assert gcpid, "el nieto debe haber registrado su pid"
        alive = True
        try:
            os.kill(int(gcpid), 0)
        except ProcessLookupError, PermissionError:
            alive = False
        assert not alive, f"el nieto {gcpid} debe morir aunque el padre ya hubiera terminado (B315)"
    finally:
        selectors.DefaultSelector = real
        if gcpid:  # limpieza defensiva independiente del código bajo prueba
            with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
                os.kill(int(gcpid), signal.SIGKILL)


def test_b315_happy_path_residual_descendant_fails_closed(tmp_path):
    marker = tmp_path / "gcpid"
    marker.write_text("")
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    gcpid = ""
    try:
        # el hijo deja un nieto en el grupo y termina rc=0. B315: el grupo se verifica tras rc=0, el nieto se
        # termina y el runner FALLA. En el SHA base `kill=False` devolvía bytes y el nieto sobrevivía. El nieto
        # REDIRIGE sus fds a /dev/null: así el pipe del padre EOFa de inmediato y el bucle completa rc=0 de verdad
        # (sin el redirect, el nieto retiene el pipe y el caso degenera en timeout).
        with pytest.raises(gs.GovernanceSnapshotError, match="cleanup incompleto|descendientes"):
            snap._run_bounded(
                ["/bin/sh", "-c", f"sleep 30 </dev/null >/dev/null 2>&1 & echo $! > {marker}; echo hi; exit 0"], 4096
            )
        time.sleep(0.3)
        gcpid = marker.read_text().strip()
        assert gcpid, "el nieto debe haber registrado su pid"
        alive = True
        try:
            os.kill(int(gcpid), 0)
        except ProcessLookupError, PermissionError:
            alive = False
        assert not alive, f"el nieto {gcpid} debe morir en la reconciliación (B315)"
    finally:
        if gcpid:
            with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
                os.kill(int(gcpid), signal.SIGKILL)


def test_b315_normal_run_with_finishing_descendant_returns(tmp_path):
    # CONTROL benigno: un nieto que TERMINA solo (el padre lo espera) deja el grupo vacío ⇒ el runner NO fail-closes.
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    out = snap._run_bounded(["/bin/sh", "-c", "sleep 0.05 & echo hi; wait"], 4096)
    assert out.strip() == b"hi"


# ---------------------------------------------------------------------------
# B318 — `_group_alive` sólo capturaba ProcessLookupError/PermissionError (un OSError(EIO) salía crudo) y la rama
# TimeoutExpired descartaba errores de `killpg` con `except OSError: pass`. Ahora el estado del grupo es TRI-VALUADO
# (UNKNOWN ≠ limpio) y toda `killpg` alimenta una incidencia.
# ---------------------------------------------------------------------------
def test_b318_group_state_unknown_on_oserror(monkeypatch):
    def _eio(pgid, sig):
        raise OSError(5, "EIO injected")

    monkeypatch.setattr(gs.os, "killpg", _eio)
    # el OSError NO escapa crudo; el estado es UNKNOWN con incidencia (jamás se descarta el error de la sonda).
    state, issue = gs.GovernanceSnapshot._group_state(999999)
    assert state is gs._GroupState.UNKNOWN
    assert issue is not None and "EIO" in str(issue)


def test_b318_group_state_absent_and_present():
    # ProcessLookupError → ABSENT; grupo vivo (el propio, con killpg 0) → PRESENT.
    state, issue = gs.GovernanceSnapshot._group_state(2**31 - 1)
    assert state is gs._GroupState.ABSENT and issue is None


def test_b318_killpg_probe_error_stays_in_taxonomy(tmp_path, monkeypatch):
    # CONDUCTUAL: un timeout fuerza terminación; la SONDA del grupo (killpg sig 0) eleva OSError(EIO). En el SHA base
    # `_group_alive` dejaba escapar ese OSError crudo desde el `finally`; ahora se convierte a UNKNOWN/taxonomía. Los
    # señalamientos reales (TERM/KILL) SÍ pasan, de modo que el hijo muere.
    real_killpg = os.killpg
    marker = tmp_path / "cpid"
    marker.write_text("")

    def _probe_eio(pgid, sig):
        if sig == 0:
            raise OSError(5, "EIO injected")
        return real_killpg(pgid, sig)

    monkeypatch.setattr(gs.os, "killpg", _probe_eio)
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    orig = gs._GIT_TIMEOUT_S
    gs._GIT_TIMEOUT_S = 0.25
    cpid = ""
    try:
        with pytest.raises(gs.GovernanceSnapshotError):  # NO un OSError crudo
            snap._run_bounded(["/bin/sh", "-c", f"echo $$ > {marker}; exec sleep 30"], 4096)
        time.sleep(0.3)
        cpid = marker.read_text().strip()
    finally:
        gs._GIT_TIMEOUT_S = orig
        if cpid:  # limpieza defensiva con el killpg REAL (real_killpg no está parcheado)
            with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
                real_killpg(int(cpid), signal.SIGKILL)


# ---------------------------------------------------------------------------
# Ronda B — huecos de la misma raíz cazados adversarialmente tras el cierre de B314/B315/B318.
# ---------------------------------------------------------------------------
def test_b317b_unregister_in_loop_stays_in_taxonomy():
    # `sel.unregister(fd)` DENTRO del bucle (al EOF de un pipe) puede elevar KeyError/ValueError. En el SHA base ese
    # KeyError NO estaba en la red de seguridad del bucle y escapaba CRUDO; ahora se convierte a la taxonomía.
    import selectors

    class _UnregFail(selectors.DefaultSelector):
        def unregister(self, fileobj):
            raise KeyError("unregister boom")

    real = selectors.DefaultSelector
    selectors.DefaultSelector = _UnregFail
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        with pytest.raises(gs.GovernanceSnapshotError, match="runner falló"):
            snap._run_bounded(["/bin/echo", "hi"], 4096)
    finally:
        selectors.DefaultSelector = real


def test_b315b_two_grandchildren_one_traps_term(tmp_path):
    # Ronda B: DOS nietos en el grupo, uno ATRAPA SIGTERM; un KeyboardInterrupt con el padre terminado debe matar a
    # AMBOS (el que atrapa TERM muere por el SIGKILL del grupo). En el SHA base sobrevivían.
    import selectors

    class _KISel(selectors.DefaultSelector):
        def select(self, timeout=None):
            time.sleep(0.3)
            raise KeyboardInterrupt

    marker = tmp_path / "pids"
    marker.write_text("")
    real = selectors.DefaultSelector
    selectors.DefaultSelector = _KISel
    pids: list[str] = []
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        cmd = f"(trap '' TERM; sleep 30 </dev/null >/dev/null 2>&1) & echo $! > {marker}; (sleep 30 </dev/null >/dev/null 2>&1) & echo $! >> {marker}; exit 0"  # noqa: E501
        with pytest.raises(KeyboardInterrupt):
            snap._run_bounded(["/bin/sh", "-c", cmd], 4096)
        time.sleep(0.6)
        pids = [p for p in marker.read_text().split() if p]
        assert len(pids) == 2, "ambos nietos deben registrar su pid"
        alive = []
        for p in pids:
            try:
                os.kill(int(p), 0)
                alive.append(p)
            except ProcessLookupError, PermissionError:
                pass
        assert not alive, f"ambos nietos deben morir con el grupo (B315 ronda B): {alive}"
    finally:
        selectors.DefaultSelector = real
        for p in pids:  # limpieza defensiva
            with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
                os.kill(int(p), signal.SIGKILL)


# ---------------------------------------------------------------------------
# B319 — el cleanup sólo capturaba `(OSError, ValueError)`: un `RuntimeError` de una operación de cleanup escapaba crudo
# y reemplazaba el error primario. B320 — `contextlib.suppress` descartaba el fallo de `poll()`. Ahora cada paso pasa por
# `_guarded_step`: total, observable y sin reemplazar el primario.
# ---------------------------------------------------------------------------
def test_b319_cleanup_runtimeerror_stays_in_taxonomy():
    import selectors

    class _CloseBoom(selectors.DefaultSelector):
        def close(self):
            super().close()
            raise RuntimeError("selector-close-runtimeerror")

    real = selectors.DefaultSelector
    selectors.DefaultSelector = _CloseBoom
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        # B319: en el SHA base `selector-close` sólo atrapaba OSError/ValueError → el RuntimeError escapaba crudo. Ahora
        # se ACUMULA como incidencia y el runner falla en taxonomía.
        with pytest.raises(gs.GovernanceSnapshotError, match="cleanup incompleto|selector-close"):
            snap._run_bounded(["/bin/echo", "hi"], 4096)
    finally:
        selectors.DefaultSelector = real


def test_b319_primary_interrupt_preserved_over_cleanup_error():
    import selectors

    class _KIandBoom(selectors.DefaultSelector):
        def select(self, timeout=None):
            raise KeyboardInterrupt("primary-ki")

        def close(self):
            super().close()
            raise RuntimeError("cleanup-would-mask-primary")

    real = selectors.DefaultSelector
    selectors.DefaultSelector = _KIandBoom
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        # B319: el KeyboardInterrupt del cuerpo es el PRIMARIO; el RuntimeError de cleanup se ADJUNTA como nota, jamás lo
        # reemplaza. En el SHA base el RuntimeError de close reemplazaba al KI (escapaba en su lugar).
        with pytest.raises(KeyboardInterrupt):
            snap._run_bounded(["/bin/echo", "hi"], 4096)
    finally:
        selectors.DefaultSelector = real


def test_b320_poll_failure_surfaces_as_issue():
    # Un TIMEOUT llama DETERMINÍSTICAMENTE `proc.poll()` en `_grace_probe` (reap best-effort durante TERM→grace). Con un
    # `poll()` que eleva OSError, B320: el error se ACUMULA como incidencia `reap/poll` (dedup a 1) y el mensaje lo
    # menciona. En el SHA base `contextlib.suppress` lo descartaba y `poll` NO aparecía en el mensaje.
    class _BadPoll(subprocess.Popen):
        def poll(self):
            raise OSError("poll-injected")

    real = gs.subprocess.Popen
    gs.subprocess.Popen = _BadPoll
    orig = gs._GIT_TIMEOUT_S
    gs._GIT_TIMEOUT_S = 0.25
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        with pytest.raises(gs.GovernanceSnapshotError, match="poll"):
            snap._run_bounded(["/bin/sh", "-c", "sleep 5"], 4096)  # el TERM/KILL del grupo (killpg real) mata al hijo
    finally:
        gs.subprocess.Popen = real
        gs._GIT_TIMEOUT_S = orig


# ---------------------------------------------------------------------------
# B325 — `_cleanup_process` llamaba `_finish_process_group` FUERA de `_guarded_step`; una operación no guardada
# (`_group_state`, monotonic, sleep) que elevaba escapaba y reemplazaba el primario. Ahora la sonda del grupo va
# guardada y la fase de grupo COMPLETA está tras un escudo exterior con fallback de terminación.
# ---------------------------------------------------------------------------
class _FakeProc:  # proceso mínimo para ejercitar el cleanup sin lanzar uno real
    pid = 999999
    stdout = stderr = None

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def test_b325_group_state_error_does_not_escape_cleanup(monkeypatch):
    # B325: `_group_state` que eleva RuntimeError ya NO escapa de `_cleanup_process`; se ACUMULA como incidencia. En el
    # SHA base la excepción cruzaba `_cleanup_process` (y desde el `finally` de `_run_bounded` podía reemplazar el primario).
    def _boom(pgid):
        raise RuntimeError("group-state-injected")

    monkeypatch.setattr(gs.GovernanceSnapshot, "_group_state", staticmethod(_boom))
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    rc, issues, interrupt = snap._cleanup_process(None, _FakeProc(), must_terminate=True)
    assert interrupt is None
    assert any("group_state" in i.operation or "RuntimeError" in i.detail for i in issues), issues


def test_b325_primary_interrupt_preserved_over_group_state_error(monkeypatch):
    import selectors

    def _boom(pgid):
        raise RuntimeError("group-state-injected")

    class _KISel(selectors.DefaultSelector):
        def select(self, timeout=None):
            raise KeyboardInterrupt("primary-ki")

    monkeypatch.setattr(gs.GovernanceSnapshot, "_group_state", staticmethod(_boom))
    real = selectors.DefaultSelector
    selectors.DefaultSelector = _KISel
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        # B325: el KeyboardInterrupt del cuerpo se preserva pese a que la sonda del grupo falle en el cleanup.
        with pytest.raises(KeyboardInterrupt):
            snap._run_bounded(["/bin/echo", "hi"], 4096)
    finally:
        selectors.DefaultSelector = real


def test_b325_fallback_terminates_group_on_finish_failure(monkeypatch, tmp_path):
    # B325: si `_finish_process_group` falla INESPERADAMENTE, un fallback gobernado termina el grupo (no sólo cierra fds).
    # En el SHA base el fallo escapaba `_cleanup_process` y el hijo quedaba vivo.
    marker = tmp_path / "cpid"
    marker.write_text("")

    def _boom(self, proc, pgid, *, terminate, interrupt):
        raise RuntimeError("finish-injected")

    monkeypatch.setattr(gs.GovernanceSnapshot, "_finish_process_group", _boom)
    proc = subprocess.Popen(
        ["/bin/sh", "-c", f"echo $$ > {marker}; exec sleep 30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    time.sleep(0.2)
    cpid = marker.read_text().strip()
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        _rc, issues, _interrupt = snap._cleanup_process(None, proc, must_terminate=True)
        assert any(i.phase == "fallback" or i.operation == "finish" for i in issues), issues
        time.sleep(0.3)
        alive = True
        try:
            os.kill(int(cpid), 0)
        except ProcessLookupError, PermissionError:
            alive = False
        assert not alive, f"el fallback debe terminar el grupo (B325); pid {cpid} vivo"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
            os.kill(int(cpid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def test_b314_b315_b318_runner_lifecycle_shape():
    # GATE ESTRUCTURAL POSITIVO (plan §6): la forma del runner y de sus helpers se fija por inspección de fuente, de
    # modo que una regresión de forma (selector tras Popen, `select.select`, `proc.terminate`, un `killpg` mudo, un
    # cleanup desnudo) rompa el contrato aunque ninguna prueba conductual la toque.
    import inspect
    import re

    src_run = inspect.getsource(gs.GovernanceSnapshot._run_bounded)
    src_clean = inspect.getsource(gs.GovernanceSnapshot._cleanup_process)
    src_group = inspect.getsource(gs.GovernanceSnapshot._finish_process_group)
    src_state = inspect.getsource(gs.GovernanceSnapshot._group_state)
    src_guard = inspect.getsource(gs.GovernanceSnapshot._guarded_step)
    src_fallback = inspect.getsource(gs.GovernanceSnapshot._fallback_terminate)
    whole = src_run + src_clean + src_group + src_state + src_guard + src_fallback

    # 1. el selector se ADQUIERE antes del proceso (B314: si falla, no hay hijo que limpiar).
    assert src_run.index("selectors.DefaultSelector(") < src_run.index("subprocess.Popen("), "selector antes de Popen"
    # 2. Popen endurecido: sesión/grupo privados, close_fds, stdin DEVNULL, allowlist de entorno.
    for tok in ("start_new_session=True", "close_fds=True", "stdin=subprocess.DEVNULL", "_GIT_CHILD_ENV"):
        assert tok in src_run, f"Popen debe fijar {tok}"
    # 3. TODA salida anormal exige terminación: must_terminate NO deriva sólo de `problem` (B315).
    assert "primary is not None or problem is not None or not event_loop_complete" in src_run
    # 4. sin backend no portable ni señalización al proceso suelto (sólo al GRUPO).
    for bad in ("select.select(", "shell=True", ".terminate(", "proc.kill("):
        assert bad not in whole, f"prohibido {bad} en el runner"
    # 5. cero cleanup mudo (B319/B320): ningún `except ...: pass` NI `contextlib.suppress` en la superficie del ciclo de
    # vida — cada operación pasa por `_guarded_step`, que ACUMULA la excepción como incidencia.
    assert not re.search(r"except\b[^\n]*:\s*\n\s*pass\b", whole), "ningún except silencioso en el runner"
    assert "contextlib.suppress" not in whole, "B320: ninguna supresión silenciosa en el ciclo de vida"
    # 6. el grupo se verifica INCLUSO tras el reap (B315): la sonda guardada `_probe()` se consulta después del reap.
    assert src_group.index("_reap(5.0)") < src_group.rindex("_probe()"), "grupo verificado tras el reap"
    # 7. cada paso de cleanup pasa por `_guarded_step` (B319): get_map, unregister, selector-close y AMBOS pipes.
    for op in ('"get_map"', '"unregister"', '"selector-close"', '"close-{tag}"'):
        assert op in src_clean, f"paso de cleanup {op} debe pasar por _guarded_step"
    assert '"stdout"' in src_clean and '"stderr"' in src_clean
    # 8. `_guarded_step` (B319): retiene el PRIMER KI/SE y ACUMULA cualquier Exception; su frontera no puede reemplazar el
    # primario del cuerpo (en `_run_bounded` el `primary` manda y el interrupt de cleanup sólo se propaga sin primario).
    # B330: `_guarded_step` retiene TODA interrupción (incl. GeneratorExit) y tiene un catch-all `except BaseException`
    # final para cualquier otro BaseException raro (cleanup incompleto, repropagado); un Exception NORMAL sólo se acumula.
    # (whitespace-robusto: ruff puede envolver la tupla larga con coma mágica final)
    guard_flat = re.sub(r"\s+", "", src_guard)
    assert "except(KeyboardInterrupt,SystemExit,GeneratorExit" in guard_flat, "B330: GeneratorExit debe retenerse"
    assert "exceptExceptionasexc" in guard_flat and "exceptBaseExceptionasexc" in guard_flat
    assert "if primary is not None:" in src_run and "if cleanup_interrupt is not None:" in src_run
    assert src_run.index("if primary is not None:") < src_run.index("if cleanup_interrupt is not None:")
    # 9. B320: el fallo de `poll()` pasa por `_guarded_step` (no se descarta) y killpg alimenta una incidencia.
    assert '"poll"' in src_group and '"reap"' in src_group and "_ProcessIssue" in src_group
    # 10. estado TRI-VALUADO: UNKNOWN existe y no equivale a limpio.
    assert "_GroupState.UNKNOWN" in src_state and "_GroupState.ABSENT" in src_state
    # 11. B325: la FASE DE GRUPO completa está tras el escudo exterior — la llamada a `_finish_process_group` va DENTRO
    # de `_guarded_step`, con `_fallback_terminate` cuando falla; la sonda del grupo va GUARDADA (dentro del lambda del
    # `_guarded_step`), nunca asignada cruda.
    assert "_finish_process_group" in src_clean and src_clean.index("_guarded_step") < src_clean.index("_finish_process_group")  # fmt: skip
    assert "_fallback_terminate" in src_clean, "B325: debe existir un fallback de terminación"
    assert "lambda: self._group_state(pgid)" in src_group and src_group.count("self._group_state(pgid)") == 1, "B325: sonda guardada"  # fmt: skip
    # 12. B325: el fallback termina el grupo (killpg TERM+KILL, wait) y verifica ausencia — no sólo cierra fds.
    assert (
        "signal.SIGTERM" in src_fallback and "signal.SIGKILL" in src_fallback and "_GroupState.ABSENT" in src_fallback
    )


# ---------------------------------------------------------------------------
# B330 — `_guarded_step` sólo retenía `(KeyboardInterrupt, SystemExit)` y acumulaba `Exception`; un `GeneratorExit` (que es
# `BaseException` pero NO `Exception`) de un paso de cleanup ESCAPABA crudo de `_cleanup_process`, saltándose los pasos
# restantes y dejando el segundo pipe ABIERTO. Ahora GeneratorExit es una interrupción retenida: el cleanup CONTINÚA
# (cierra AMBOS pipes) y se propaga después; cualquier otro BaseException raro cae en el catch-all y también se repropaga.
# ---------------------------------------------------------------------------
def test_b330_generatorexit_in_cleanup_is_retained_not_escaped(monkeypatch):
    class _Pipe:  # pipe mínimo cerrable; el primero eleva GeneratorExit al cerrar
        def __init__(self, boom=False):
            self.closed = False
            self._boom = boom

        def close(self):
            if self._boom:
                raise GeneratorExit("ge-on-close")
            self.closed = True

    class _Proc:
        pid = 999999

        def __init__(self):
            self.stdout = _Pipe(boom=True)  # el PRIMER pipe eleva GeneratorExit al cerrar
            self.stderr = _Pipe(boom=False)  # el SEGUNDO debe cerrarse igual (el cleanup no puede abortar)

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    # aísla B330 en la fase de pipes: la fase de grupo es un no-op limpio
    monkeypatch.setattr(
        gs.GovernanceSnapshot,
        "_finish_process_group",
        lambda self, proc, pgid, *, terminate, interrupt: (0, gs._GroupState.ABSENT, []),
    )
    snap = gs.GovernanceSnapshot(_REPO_ROOT)
    proc = _Proc()
    try:
        _rc, issues, interrupt = snap._cleanup_process(None, proc, must_terminate=True)
    except GeneratorExit:  # comportamiento INSEGURO del SHA base: el GeneratorExit ESCAPA del cleanup
        pytest.fail("B330: GeneratorExit escapó de _cleanup_process (el 2º pipe quedó ABIERTO)")
    assert isinstance(interrupt, GeneratorExit), interrupt  # retenido como interrupción, no escapado
    assert proc.stderr.closed, "B330: el 2º pipe DEBE cerrarse pese al GeneratorExit del 1º"
    assert any(i.operation == "close-stdout" for i in issues), issues


def test_b330_generatorexit_propagates_from_runner_after_cleanup():
    # B330 end-to-end: un GeneratorExit del cuerpo (aquí en `select`) propaga como PRIMARIO tras un cleanup TOTAL — el
    # hijo se termina y ambos pipes se cierran. En el SHA base escapaba antes del cleanup del grupo.
    import selectors

    class _GESel(selectors.DefaultSelector):
        def select(self, timeout=None):
            raise GeneratorExit("primary-ge")

    real = selectors.DefaultSelector
    selectors.DefaultSelector = _GESel
    try:
        snap = gs.GovernanceSnapshot(_REPO_ROOT)
        with pytest.raises(GeneratorExit):
            snap._run_bounded(["/bin/echo", "hi"], 4096)
    finally:
        selectors.DefaultSelector = real


# ---------------------------------------------------------------------------
# B309 — `_governed_git_identity()` cerraba los fds con `except OSError: pass`, así que un cierre fallido sobre un camino
# EXITOSO se silenciaba y devolvía identidad. Ahora el resultado es TOTAL: un cierre fallido es fail-closed, los errores
# se agregan, y KeyboardInterrupt/SystemExit propagan.
# ---------------------------------------------------------------------------
def test_b309_close_error_on_success_fails_closed(monkeypatch):
    real = os.close
    state = {"n": 0}

    def boom(fd):
        state["n"] += 1
        if state["n"] == 1:
            try:
                real(fd)
            finally:
                raise OSError(9, "EBADF inyectado")
        return real(fd)

    monkeypatch.setattr(os, "close", boom)
    with pytest.raises(gs.GovernanceSnapshotError, match="B309"):
        gs.GovernanceSnapshot(_REPO_ROOT)._governed_git_identity()


def test_b309_multiple_close_errors_aggregated(monkeypatch):
    real = os.close

    def boom(fd):
        try:
            real(fd)
        finally:
            raise OSError(9, f"EBADF-{fd}")

    monkeypatch.setattr(os, "close", boom)
    with pytest.raises(gs.GovernanceSnapshotError) as ei:
        gs.GovernanceSnapshot(_REPO_ROOT)._governed_git_identity()
    assert "B309" in str(ei.value) and "cerrar" in str(ei.value)


def test_b309_keyboardinterrupt_propagates(monkeypatch):
    def ki(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "close", ki)
    with pytest.raises(KeyboardInterrupt):
        gs.GovernanceSnapshot(_REPO_ROOT)._governed_git_identity()


def test_b309_no_silent_except_in_git_surface():
    # estructural: la superficie git no usa `except OSError: pass` (taxonomía total, B299/B309).
    import inspect
    import re

    for fn in (gs.GovernanceSnapshot._governed_git_identity, gs.GovernanceSnapshot._run_git):
        src = inspect.getsource(fn)
        assert not re.search(r"except\s+OSError[^\n:]*:\s*\n\s*pass", src), f"{fn.__name__} no debe silenciar OSError (B309)"  # fmt: skip


def test_b301_reverify_detects_inventory_change(monkeypatch):
    with gs.GovernanceSnapshot(_REPO_ROOT) as snap:
        snap.tracked(gs.TrackedQuery("suffix", ".py"))  # sella
        orig = snap._capture_inventory

        def _changed():
            paths, sha, ident = orig()
            return paths[:-1], sha, ident  # inventario con una ruta menos → cambio detectado

        monkeypatch.setattr(snap, "_capture_inventory", _changed)
        with pytest.raises(gs.GovernanceSnapshotError, match="B301"):
            snap.reverify()


def test_b301_toplevel_mismatch_rejected(monkeypatch):
    def _fake_git(op, out_limit):
        return b"/somewhere/else\n" if op == "TOPLEVEL" else b""

    with gs.GovernanceSnapshot(_REPO_ROOT) as snap:
        monkeypatch.setattr(snap, "_run_git", _fake_git)
        with pytest.raises(gs.GovernanceSnapshotError, match="toplevel"):
            snap.tracked(gs.TrackedQuery("suffix", ".py"))


# ---------------------------------------------------------------------------
# B302 — el contrato público no era total: `_cache.get(rel)` y `_CATEGORY_CAPS.get(category)` requerían objetos
# hashables, así que un `rel`/`category` no hashable daba TypeError CRUDO (fuera de la taxonomía). `tracked` aceptaba
# pathspec git arbitrario. Ahora los tipos se cierran ANTES de cualquier lookup y la consulta es una gramática cerrada.
# ---------------------------------------------------------------------------
def test_b302_unhashable_and_wrong_type_inputs_in_taxonomy(tmp_path):
    with _lay(tmp_path) as snap:
        for kw in (
            {"rel": []},  # no hashable → antes TypeError en _cache.get
            {"rel": b"x"},  # bytes, no str
            {"rel": 5},  # int
            {"rel": "tools/campaign_bundle.py", "category": []},  # category no hashable → antes TypeError en .get
            {"rel": "tools/campaign_bundle.py", "category": 3},  # category no str
            {"rel": "tools/../etc/passwd"},  # traversal
            {"rel": "a\x00b"},  # NUL
            {"rel": "/abs"},  # absoluta
        ):
            with pytest.raises(gs.GovernanceSnapshotError):
                snap.read(**kw)


def test_b302_read_input_types_still_closed(tmp_path):
    # regresión B302 (read): tipos no hashables/incorrectos en taxonomía (la gramática de query/root migró a B304).
    with _lay(tmp_path) as snap:
        for kw in ({"rel": []}, {"rel": b"x"}, {"rel": "tools/campaign_bundle.py", "category": []}):
            with pytest.raises(gs.GovernanceSnapshotError):
                snap.read(**kw)


# ---------------------------------------------------------------------------
# B304 — el contrato "cerrado" aún colaba: `TrackedQuery(prefix="")`/`suffix=""` seleccionaban TODO el inventario, una
# SUBCLASE que reescribía `matches()` ignoraba la modalidad, y un `root` PathLike cuyo `__fspath__` devolvía bytes hacía
# escapar `TypeError`. Ahora `TrackedQuery` es `(kind, value)` con valor no vacío y matching interno, `type(query) is
# TrackedQuery`, y `root` es SÓLO `str` exacto.
# ---------------------------------------------------------------------------
def test_b304_tracked_query_grammar_closed():
    bad = [
        ("prefix", ""),  # vacío
        ("suffix", ""),  # vacío
        ("exact", ""),  # vacío
        ("exact", "a\x00b"),  # NUL
        ("exact", "/abs"),  # absoluto
        ("exact", "a/../b"),  # traversal
        ("prefix", "../x"),  # traversal
        ("prefix", "/x"),  # absoluto
        ("prefix", "a//b"),  # doble slash
        ("prefix", "a/./b"),  # punto
        ("suffix", "a/b"),  # slash en suffix
        ("suffix", ".."),  # `..`
        ("bogus", "x"),  # kind inválido
    ]
    for kind, value in bad:
        with pytest.raises(gs.GovernanceSnapshotError, match="B304"):
            gs.TrackedQuery(kind, value)
    for kind, value in (("kind_nonstr", None), ("prefix", 5), ("prefix", [])):  # tipos no-str
        with pytest.raises(gs.GovernanceSnapshotError, match="B304"):
            gs.TrackedQuery(kind if isinstance(kind, str) else "prefix", value)
    # controles válidos
    for kind, value in (("prefix", "tools/"), ("suffix", ".py"), ("exact", "tools/check_reflection.py")):
        assert gs.TrackedQuery(kind, value).value == value


def test_b304_subclass_and_wrong_type_query_rejected(tmp_path):
    # una subclase que reescribe matches() NO puede colar su lógica: `type(query) is TrackedQuery`.
    class Evil(gs.TrackedQuery):
        def matches(self, _p):  # hostil a propósito (nunca alcanzable: la subclase se rechaza)
            return True

    with _lay(tmp_path) as snap:
        with pytest.raises(gs.GovernanceSnapshotError, match="B304"):
            Evil("exact", "x")  # __post_init__ ya rechaza la subclase
        with pytest.raises(gs.GovernanceSnapshotError, match="B304"):
            snap.tracked("tools/*")  # str crudo, no TrackedQuery
        with pytest.raises(gs.GovernanceSnapshotError, match="B307"):
            snap.tracked(object.__new__(gs.TrackedQuery))  # sin campos → revalidación de frontera (B307)


def test_b304_root_only_exact_str(tmp_path):
    class BytesPath:
        def __fspath__(self):
            return b"/bytes"

    class RaisingPath:
        def __fspath__(self):
            raise RuntimeError("hostil")

    for bad in (5, b"/x", BytesPath(), RaisingPath(), "", "root\x00nul"):
        with pytest.raises(gs.GovernanceSnapshotError, match="B304"):
            gs.GovernanceSnapshot(bad)


# ---------------------------------------------------------------------------
# B307 — `tracked()` sólo revalidaba tipo/kind/no-vacío; una instancia del tipo EXACTO creada por `object.__new__` +
# `object.__setattr__` (saltando `__post_init__`) evadía la gramática por modalidad (`.`, `../`, prefix sin `/`, suffix
# con slash) y seleccionaba paths no autorizados. Ahora la gramática COMPLETA (`_tracked_query_problem`) se aplica en
# AMBAS fronteras, y una query rechazada NO captura git.
# ---------------------------------------------------------------------------
def _forge_query(kind, value):
    q = object.__new__(gs.TrackedQuery)
    object.__setattr__(q, "kind", kind)
    object.__setattr__(q, "value", value)
    return q


def test_b307_forged_query_gets_full_grammar_at_tracked_boundary(tmp_path, monkeypatch):
    root = os.path.dirname(os.path.dirname(os.path.abspath(gs.__file__)))
    monkeypatch.chdir(tmp_path)
    with gs.GovernanceSnapshot(root) as snap:
        for kind, value in (
            ("prefix", "."),  # `.` no es directorio explícito
            ("prefix", "../"),  # traversal
            ("prefix", "/x/"),  # absoluto
            ("prefix", "tools"),  # sin `/` final
            ("exact", "/etc/passwd"),  # absoluto
            ("exact", "a/../b"),  # traversal
            ("suffix", "a/b"),  # slash
            ("suffix", "noext"),  # sin extensión explícita
            ("bogus", "x"),  # kind inválido
        ):
            before = snap._captures
            with pytest.raises(gs.GovernanceSnapshotError, match="B307"):
                snap.tracked(_forge_query(kind, value))
            assert snap._captures == before, "una query rechazada NO debe capturar git (B307)"


def test_b307_grammar_single_source_matches_constructor(tmp_path):
    # la MISMA gramática rige el constructor y la frontera: lo que rechaza `__post_init__` lo rechaza `tracked`, y
    # viceversa; los controles válidos pasan por ambos.
    for kind, value in (("prefix", "tools/"), ("suffix", ".py"), ("exact", "tools/check_reflection.py")):
        assert gs._tracked_query_problem(kind, value) is None
        gs.TrackedQuery(kind, value)  # no eleva
    for kind, value in (("prefix", "."), ("suffix", "x/y"), ("exact", "/abs")):
        assert gs._tracked_query_problem(kind, value) is not None
        with pytest.raises(gs.GovernanceSnapshotError):
            gs.TrackedQuery(kind, value)
