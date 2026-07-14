"""Contrato del sistema de entornos content-addressed (P0R.5 · R1/R2). Unit rápido: determinismo del
env_id, ausencia de rutas/fechas en el descriptor, canonicalización PEP 503, env_owns, y la lógica de
ready_valid (reuso/tamper) con _pip_freeze monkeypatcheado. El BUILD real + smoke lo prueba el job CI
`dvc-tool-install` en Linux+macOS (evita ~1 min de red por corrida de unit tests)."""

from __future__ import annotations

import json

import pytest

import tools.python_env as pe


def test_env_id_deterministic():
    assert pe.env_id("dvc-tool") == pe.env_id("dvc-tool")
    assert len(pe.env_id("dvc-tool")) == 64


def test_descriptor_has_no_paths_or_staging():
    # el env_id debe ser reproducible ⇒ el descriptor NO puede llevar rutas absolutas, staging, tmp
    # ni el propio directorio de entornos (fechas/PID no aparecen porque no se capturan).
    blob = json.dumps(pe.descriptor("dvc-tool"))
    for bad in ("/Users/", "/home/", "/private/", ".vp_envs", ".staging", "/tmp/"):
        assert bad not in blob, f"descriptor filtra {bad!r}: {blob}"


def test_descriptor_binds_lock_and_lockset_and_config():
    d = pe.descriptor("dvc-tool")
    for k in ("lock_sha256", "lockset_sha256", "profile_config_sha256", "install_mode"):
        assert d[k] and (d[k].startswith("sha256:") or k == "install_mode")
    assert d["install_mode"] == "hash-verified"


def test_env_id_changes_if_lock_changes(monkeypatch):
    base = pe.env_id("dvc-tool")
    real = pe._sha256_path

    def fake(p):
        s = real(p)
        return s[:-1] + ("0" if s[-1] != "0" else "1") if "dvc-tool" in p.name else s

    monkeypatch.setattr(pe, "_sha256_path", fake)
    assert pe.env_id("dvc-tool") != base


def test_canon_pep503():
    assert pe._canon("flufl.lock") == "flufl-lock"
    assert pe._canon("ruamel.yaml") == "ruamel-yaml"
    assert pe._canon("zc.lockfile") == "zc-lockfile"
    assert pe._canon("DVC_S3") == "dvc-s3"


def test_env_owns_inside_and_outside():
    d = pe.env_dir("dvc-tool")
    assert pe.env_owns("dvc-tool", d / "bin" / "dvc")
    assert not pe.env_owns("dvc-tool", pe.ROOT / "ante" / "bin" / "dvc")


def test_unknown_profile_and_bad_console_script():
    with pytest.raises(SystemExit):
        pe.descriptor("nope")
    with pytest.raises(SystemExit):
        pe.resolve_console_script("dvc-tool", "python")  # no declarado como console-script


# ----------------------------- ready_valid: reuso vs tamper -----------------------------

_FREEZE = ["alpha==1.0.0", "beta==2.0.0"]


def _fake_env(tmp_path, monkeypatch, *, digest_ok=True, env_id_ok=True):
    envp = tmp_path / "env"
    (envp / "bin").mkdir(parents=True)
    (envp / "bin" / "python").write_text("#!/bin/sh\n")  # existe; no se ejecuta (freeze monkeypatched)
    monkeypatch.setattr(pe, "_pip_freeze", lambda py: _FREEZE)
    monkeypatch.setattr(pe, "env_id", lambda *a, **k: "KNOWNID")
    digest = pe._inventory_digest(_FREEZE if digest_ok else ["gamma==9.9.9"])
    (envp / "READY.json").write_text(
        json.dumps({"env_id": "KNOWNID" if env_id_ok else "OTHER", "inventory_digest": digest})
    )
    return envp


def test_ready_valid_ok(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch)
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert ok, why


def test_ready_valid_no_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "env_id", lambda *a, **k: "KNOWNID")
    ok, why = pe.ready_valid(tmp_path / "env", "dvc-tool")
    assert not ok and "READY" in why


def test_ready_valid_wrong_env_id(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch, env_id_ok=False)
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "env_id" in why


def test_ready_valid_tamper(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch, digest_ok=False)
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "TAMPER" in why


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
