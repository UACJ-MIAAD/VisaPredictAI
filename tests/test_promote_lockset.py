"""Contrato del promotor atómico de la matriz de 9 locks (tools/promote_lockset.py, P0R.4).

Verifica: (1) happy path promueve los 9 + manifest coherente; (2) staging inválido (faltante /
vacío / con ruta temporal) aborta SIN mutar locks/; (3) un fallo a media promoción hace ROLLBACK
total (restaura previos, borra los que no existían, deja el manifest viejo); (4) el manifest se
escribe AL FINAL y sus hashes coinciden con los bytes de los locks.
"""

from __future__ import annotations

import importlib
import json

import pytest

promote_lockset = importlib.import_module("tools.promote_lockset")

GEN = {"python": "3.14", "pip": "26.1.2", "setuptools": "81.0.0", "wheel": "0.47.0", "uv": "0.11.28"}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Aísla ROOT/LOCKS/MANIFEST/SOURCES del módulo en un árbol temporal."""
    root = tmp_path / "repo"
    locks = root / "locks"
    reqs = root / "requirements"
    locks.mkdir(parents=True)
    reqs.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    for w in ("deep.in", "deep-linux-cpu.in", "deep-linux-cu126.in"):
        (reqs / w).write_text(f"# {w}\ntorch==2.12.1\n")
    monkeypatch.setattr(promote_lockset, "ROOT", root)
    monkeypatch.setattr(promote_lockset, "LOCKS", locks)
    monkeypatch.setattr(promote_lockset, "MANIFEST", locks / "lockset.json")
    monkeypatch.setattr(
        promote_lockset,
        "SOURCES",
        (
            "pyproject.toml",
            "requirements/deep.in",
            "requirements/deep-linux-cpu.in",
            "requirements/deep-linux-cu126.in",
        ),
    )
    return root, locks


def _staged(tmp_path, mutate=None):
    """Directorio de staging con los 9 locks válidos; `mutate(name)->text|None` para variar uno."""
    staged = tmp_path / "staged"
    staged.mkdir()
    for i, name in enumerate(promote_lockset.LOCK_NAMES):
        text = f"# lock {name}\npkg{i}==1.2.3\nother==4.5.6\n"
        if mutate:
            override = mutate(name)
            if override is not None:
                text = override
        (staged / name).write_text(text)
    return staged


def test_happy_path_promotes_nine_and_manifest(sandbox, tmp_path):
    _root, locks = sandbox
    staged = _staged(tmp_path)
    manifest = promote_lockset.promote(staged, GEN)

    # los 9 locks existen con el contenido staged
    for name in promote_lockset.LOCK_NAMES:
        assert (locks / name).read_text() == (staged / name).read_text()
    # manifest coherente: 9 locks, generator, hashes de fuentes, y sha == bytes del lock
    assert (locks / "lockset.json").exists()
    assert manifest["generator"] == GEN
    assert len(manifest["locks"]) == 9
    assert set(manifest["sources"]) == set(promote_lockset.SOURCES)
    for name in promote_lockset.LOCK_NAMES:
        assert manifest["locks"][f"locks/{name}"]["sha256"] == promote_lockset._sha256((locks / name).read_bytes())
        assert manifest["locks"][f"locks/{name}"]["pins"] == 2


def test_rejects_missing_lock_without_mutation(sandbox, tmp_path):
    _root, locks = sandbox
    (locks / "runtime.txt").write_text("# viejo\nold==0.0.1\n")
    staged = _staged(tmp_path)
    (staged / "deep-linux-x86_64-cu126.txt").unlink()  # falta uno -> 8
    with pytest.raises(SystemExit):
        promote_lockset.promote(staged, GEN)
    # nada mutó: runtime.txt sigue viejo, no hay manifest
    assert (locks / "runtime.txt").read_text() == "# viejo\nold==0.0.1\n"
    assert not (locks / "lockset.json").exists()


def test_rejects_empty_lock(sandbox, tmp_path):
    _root, _locks = sandbox
    staged = _staged(tmp_path, mutate=lambda n: "# sin pins\n" if n == "dev.txt" else None)
    with pytest.raises(SystemExit):
        promote_lockset.promote(staged, GEN)


def test_rejects_temp_path_leak(sandbox, tmp_path):
    _root, _locks = sandbox
    staged = _staged(
        tmp_path, mutate=lambda n: "# x\npkg==1.0.0  # -c /var/folders/xx\n" if n == "runtime.txt" else None
    )
    with pytest.raises(SystemExit):
        promote_lockset.promote(staged, GEN)


def test_rollback_on_midway_failure(sandbox, tmp_path, monkeypatch):
    _root, locks = sandbox
    # estado previo: 2 locks + manifest viejo; los otros 7 no existen
    (locks / "runtime.txt").write_text("# viejo runtime\nold==0.0.1\n")
    (locks / "dev.txt").write_text("# viejo dev\nold==0.0.2\n")
    old_manifest = json.dumps({"schema_version": 1, "stale": True})
    (locks / "lockset.json").write_text(old_manifest)
    staged = _staged(tmp_path)

    # fuerza fallo en el 3er os.replace (tras promover runtime + dev)
    real_replace = promote_lockset.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("kill -9 simulado a media promoción")
        return real_replace(src, dst)

    monkeypatch.setattr(promote_lockset.os, "replace", flaky_replace)
    with pytest.raises(RuntimeError):
        promote_lockset.promote(staged, GEN)

    # rollback total: previos restaurados, no-existentes borrados, manifest viejo intacto
    assert (locks / "runtime.txt").read_text() == "# viejo runtime\nold==0.0.1\n"
    assert (locks / "dev.txt").read_text() == "# viejo dev\nold==0.0.2\n"
    for name in promote_lockset.LOCK_NAMES:
        if name not in ("runtime.txt", "dev.txt"):
            assert not (locks / name).exists(), f"{name} no debió quedar tras el rollback"
    assert (locks / "lockset.json").read_text() == old_manifest
    # sin huérfanos .tmp
    assert not list(locks.glob(".*.tmp"))


def test_manifest_written_last_matches_bytes(sandbox, tmp_path):
    _root, locks = sandbox
    staged = _staged(tmp_path)
    promote_lockset.promote(staged, GEN)
    manifest = json.loads((locks / "lockset.json").read_text())
    # una mutación posterior del lock rompería la coherencia -> el auditor la detecta
    for name in promote_lockset.LOCK_NAMES:
        assert manifest["locks"][f"locks/{name}"]["sha256"] == promote_lockset._sha256((locks / name).read_bytes())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
