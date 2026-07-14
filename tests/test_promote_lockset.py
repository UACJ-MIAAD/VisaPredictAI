"""Mecánica del promotor: rollback transaccional + detección de matriz parcial (P0R.4R).

El CONTRATO estático del staging/manifiesto vive en tools/lock_contracts.py (probado en
test_lock_contracts.py); aquí se monkeypatchea a vacío para ejercitar la MECÁNICA de promoción:
rename por lock, rollback ante fallo, manifiesto como última escritura, y señal de matriz inválida
si el rollback también falla.
"""

from __future__ import annotations

import importlib
import json

import pytest

promote_lockset = importlib.import_module("tools.promote_lockset")
lc = promote_lockset.lc

GEN = {
    "python": "3.14.2",
    "platform": "Darwin arm64",
    "pip": "26.1.2",
    "setuptools": "81.0.0",
    "wheel": "0.47.0",
    "uv": "0.11.28",
}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Aísla el promotor en un árbol temporal; contrato estático monkeypatcheado a vacío."""
    root = tmp_path / "repo"
    locks = root / "locks"
    locks.mkdir(parents=True)
    for src in lc.SOURCES:  # las 7 fuentes gobernadas deben existir (se hashean en el manifiesto)
        p = root / src
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# stub {src}\n")
    monkeypatch.setattr(promote_lockset, "ROOT", root)
    monkeypatch.setattr(promote_lockset, "LOCKS", locks)
    monkeypatch.setattr(promote_lockset, "MANIFEST", locks / "lockset.json")
    monkeypatch.setattr(promote_lockset.lc, "validate_staging", lambda staged, root=None: [])
    monkeypatch.setattr(promote_lockset.lc, "validate_all", lambda root, manifest=None: [])
    return root, locks


def _staged(tmp_path):
    """Staging con los 9 locks (pins simples, pin_map-parseables)."""
    staged = tmp_path / "staged"
    staged.mkdir()
    for i, name in enumerate(promote_lockset.LOCK_NAMES):
        (staged / name).write_text(f"# lock {name}\npkg{i}==1.2.3\nother==4.5.6\n")
    return staged


def test_happy_path_promotes_nine_and_manifest(sandbox, tmp_path):
    _root, locks = sandbox
    staged = _staged(tmp_path)
    manifest = promote_lockset.promote(staged, GEN)
    for name in promote_lockset.LOCK_NAMES:
        assert (locks / name).read_text() == (staged / name).read_text()
    assert (locks / "lockset.json").exists()
    assert manifest["generator"] == GEN
    assert len(manifest["locks"]) == 9
    assert set(manifest["sources"]) == set(lc.SOURCES) and len(manifest["sources"]) == 7
    for name in promote_lockset.LOCK_NAMES:
        assert manifest["locks"][f"locks/{name}"]["pins"] == 2


def test_invalid_generator_aborts(sandbox, tmp_path):
    # el generator se valida ANTES de tocar locks (validate_generator NO está monkeypatcheado)
    _root, locks = sandbox
    with pytest.raises(SystemExit, match="generator inválido"):
        promote_lockset.promote(_staged(tmp_path), {**GEN, "platform": "Linux x86_64"})
    assert not (locks / "lockset.json").exists()


def test_invalid_staging_aborts_without_mutation(sandbox, tmp_path, monkeypatch):
    _root, locks = sandbox
    (locks / "runtime.txt").write_text("# viejo\nold==0.0.1\n")
    monkeypatch.setattr(promote_lockset.lc, "validate_staging", lambda staged, root=None: ["staging inválido X"])
    with pytest.raises(SystemExit):
        promote_lockset.promote(_staged(tmp_path), GEN)
    assert (locks / "runtime.txt").read_text() == "# viejo\nold==0.0.1\n"
    assert not (locks / "lockset.json").exists()


def test_rollback_on_midway_failure(sandbox, tmp_path, monkeypatch):
    _root, locks = sandbox
    (locks / "runtime.txt").write_text("# viejo runtime\nold==0.0.1\n")
    (locks / "dev.txt").write_text("# viejo dev\nold==0.0.2\n")
    old_manifest = json.dumps({"schema_version": 1, "stale": True})
    (locks / "lockset.json").write_text(old_manifest)
    staged = _staged(tmp_path)

    real_replace = promote_lockset.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:  # tras promover runtime + dev
            raise RuntimeError("fallo simulado durante el rename")
        return real_replace(src, dst)

    monkeypatch.setattr(promote_lockset.os, "replace", flaky_replace)
    with pytest.raises(RuntimeError):
        promote_lockset.promote(staged, GEN)
    assert (locks / "runtime.txt").read_text() == "# viejo runtime\nold==0.0.1\n"
    assert (locks / "dev.txt").read_text() == "# viejo dev\nold==0.0.2\n"
    for name in promote_lockset.LOCK_NAMES:
        if name not in ("runtime.txt", "dev.txt"):
            assert not (locks / name).exists(), f"{name} no debió quedar tras el rollback"
    assert (locks / "lockset.json").read_text() == old_manifest
    assert not list(locks.glob(".*.tmp"))


def test_post_validation_failure_rolls_back(sandbox, tmp_path, monkeypatch):
    # si la autovalidación post-promoción falla, se hace rollback completo (nada queda promovido)
    _root, locks = sandbox
    monkeypatch.setattr(promote_lockset.lc, "validate_all", lambda root, manifest=None: ["post incoherente"])
    with pytest.raises(RuntimeError, match="post-promoción"):
        promote_lockset.promote(_staged(tmp_path), GEN)
    for name in promote_lockset.LOCK_NAMES:
        assert not (locks / name).exists()
    assert not (locks / "lockset.json").exists()


def test_rollback_failure_removes_manifest(sandbox, tmp_path, monkeypatch):
    # fallo de promoción Y de rollback -> manifiesto ELIMINADO (señal de matriz inválida) + RuntimeError
    _root, locks = sandbox
    (locks / "lockset.json").write_text(json.dumps({"stale": True}))
    real_replace = promote_lockset.os.replace
    state = {"phase": "promote", "n": 0}

    def flaky_replace(src, dst):
        state["n"] += 1
        if state["phase"] == "promote" and state["n"] == 1:
            state["phase"] = "rollback"
            raise RuntimeError("fallo de promoción")
        if state["phase"] == "rollback":
            raise RuntimeError("fallo de rollback también")
        return real_replace(src, dst)

    monkeypatch.setattr(promote_lockset.os, "replace", flaky_replace)
    with pytest.raises(RuntimeError, match="ROLLBACK FALLIDO"):
        promote_lockset.promote(_staged(tmp_path), GEN)
    assert not (locks / "lockset.json").exists()  # señal inequívoca de matriz inválida


def test_manifest_written_last_matches_bytes(sandbox, tmp_path):
    _root, locks = sandbox
    promote_lockset.promote(_staged(tmp_path), GEN)
    manifest = json.loads((locks / "lockset.json").read_text())
    for name in promote_lockset.LOCK_NAMES:
        assert manifest["locks"][f"locks/{name}"]["sha256"] == promote_lockset._sha256((locks / name).read_bytes())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
