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


_DESC = {"fake": "descriptor"}


def _fake_env(tmp_path, monkeypatch, *, digest_ok=True, env_id_ok=True):
    envp = tmp_path / "env"
    (envp / "bin").mkdir(parents=True)
    (envp / "bin" / "python").write_text("#!/bin/sh\n")  # existe; no se ejecuta (freeze monkeypatched)
    monkeypatch.setattr(pe, "_pip_freeze", lambda py: _FREEZE)
    monkeypatch.setattr(pe, "_pip_check", lambda py: True)
    monkeypatch.setattr(pe, "env_id", lambda *a, **k: "KNOWNID")
    monkeypatch.setattr(pe, "descriptor", lambda *a, **k: _DESC)
    monkeypatch.setattr(pe, "_tree_digest", lambda p: "TREE")
    monkeypatch.setattr(pe.lc, "validate_all", lambda root: [])
    sealed = _FREEZE if digest_ok else ["gamma==9.9.9"]
    meta = {
        "schema_version": 1,
        "env_id": "KNOWNID" if env_id_ok else "OTHER",
        "descriptor": _DESC,
        "inventory": sealed,
        "inventory_digest": pe._inventory_digest(sealed),
        "file_hashes": {},
        "tree_digest": "TREE",
        "pip_check": "ok",
        "n_packages": len(sealed),
    }
    (envp / "READY.json").write_text(json.dumps(meta))
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


# ----------------------------- C1: regresiones de los falsos verdes cerrados -----------------------------


def test_b3_cache_guarded_changes_env_id():
    prof = pe.load_profiles()
    base = pe.env_id("dvc-tool", None, prof)
    prof["profiles"]["dvc-tool"]["cache_guarded"] = False
    import hashlib

    alt = hashlib.sha256(json.dumps(pe.descriptor("dvc-tool", None, prof), sort_keys=True).encode()).hexdigest()
    assert base != alt


def test_b3_governance_hashes_in_descriptor():
    gov = pe.descriptor("dvc-tool")["governance"]
    assert set(gov) == {"python_env_sha256", "dvc_cache_guard_sha256", "profiles_json_sha256"}
    assert all(v.startswith("sha256:") for v in gov.values())


def test_b3_variant_in_descriptor_and_deep_requires_variant():
    assert pe.descriptor("deep", "cpu")["variant"] == "cpu"
    with pytest.raises(SystemExit):
        pe.env_id("deep")  # nunca CUDA/CPU en silencio


def test_auto_recipe_resolves_by_hashes(tmp_path, monkeypatch):
    # un lock con --hash= => hash-verified; sin => version-locked
    hashed = pe.ROOT / "locks/dvc-tool-macos-arm64.txt"
    plain = pe.ROOT / "locks/runtime.txt"
    cfg = {"install_mode": "auto"}
    assert pe._resolved_recipe(cfg, str(hashed.relative_to(pe.ROOT))) == "hash-verified"
    assert pe._resolved_recipe(cfg, str(plain.relative_to(pe.ROOT))) == "version-locked"


def test_all_profiles_compute_env_id():
    combos = [("runtime", None), ("dev", None), ("model", None), ("deep", "cpu"), ("dvc-tool", None)]
    ids = {pe.env_id(p, v) for p, v in combos}
    assert len(ids) == len(combos)  # todos distintos y computables


def test_b4_extra_package_blocks(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch)
    # inventario vivo con un paquete EXTRA no sellado
    monkeypatch.setattr(pe, "_pip_freeze", lambda py: _FREEZE + ["evil==6.6.6"])
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "TAMPER" in why


def test_b4_pip_check_on_reuse(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch)
    monkeypatch.setattr(pe, "_pip_check", lambda py: False)  # pip check roto en reuso
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "pip check" in why


def test_no_force_flag_in_cli():
    # `build` ya no acepta --force (no se puede reconstruir un entorno sellado)
    with pytest.raises(SystemExit):
        pe.main(["python_env", "build", "--profile", "dvc-tool", "--force"])


def test_prune_only_touches_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "STAGING_ROOT", tmp_path / ".staging")
    (tmp_path / ".staging" / "x").mkdir(parents=True)
    n = pe.prune_staging()
    assert n == 1 and not (tmp_path / ".staging" / "x").exists()


def test_provenance_distinguishes_head_checkout(monkeypatch):
    monkeypatch.setenv("GITHUB_PR_HEAD_SHA", "aaaa")
    monkeypatch.setenv("GITHUB_BASE_SHA", "bbbb")
    prov = pe.provenance()
    assert prov["source_head_sha"] == "aaaa" and prov["base_sha"] == "bbbb"
    assert "checkout_sha" in prov and "git_dirty" in prov


# ----------------------------- C1: regresiones R8R2 (B12-B16) -----------------------------


def test_b12_cpu_torch_and_index_change_env_id():
    import hashlib

    prof = pe.load_profiles()
    base = pe.env_id("model", None, prof)
    for field in ("cpu_torch", "cpu_index"):
        p2 = json.loads(json.dumps(prof))
        p2["profiles"]["model"][field] = "sentinel"
        alt = hashlib.sha256(json.dumps(pe.descriptor("model", None, p2), sort_keys=True).encode()).hexdigest()
        assert base != alt, field


def test_b13_tree_digest_detects_file_change(tmp_path):
    d = tmp_path / "env"
    (d / "lib").mkdir(parents=True)
    (d / "lib" / "x.py").write_text("a = 1\n")
    h1 = pe._tree_digest(d)
    (d / "lib" / "x.py").write_text("a = 2\n")  # misma "versión", distinto contenido
    assert pe._tree_digest(d) != h1
    # bytecode NO cuenta (mutable)
    h2 = pe._tree_digest(d)
    (d / "lib" / "__pycache__").mkdir()
    (d / "lib" / "__pycache__" / "x.pyc").write_text("junk")
    assert pe._tree_digest(d) == h2


def test_b14_invalid_lockset_blocks_reuse(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch)
    monkeypatch.setattr(pe.lc, "validate_all", lambda root: ["lockset roto"])
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "lockset/contrato inválido" in why


def test_b13_ready_schema_exact(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch)
    meta = json.loads((envp / "READY.json").read_text())
    meta["extra_key"] = 1  # clave de más
    (envp / "READY.json").write_text(json.dumps(meta))
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "esquema inexacto" in why


def _write_profiles(tmp_path, monkeypatch, mutate):
    prof = json.loads((pe.ROOT / "environments" / "python_profiles.json").read_text())
    mutate(prof)
    p = tmp_path / "pp.json"
    p.write_text(json.dumps(prof))
    monkeypatch.setattr(pe, "PROFILES_JSON", p)


def test_b15_unknown_profile_key_rejected(tmp_path, monkeypatch):
    _write_profiles(tmp_path, monkeypatch, lambda prof: prof["profiles"]["dvc-tool"].update(evil=1))
    with pytest.raises(SystemExit):
        pe.load_profiles()


def test_b15_missing_profile_rejected(tmp_path, monkeypatch):
    _write_profiles(tmp_path, monkeypatch, lambda prof: prof["profiles"].pop("model"))
    with pytest.raises(SystemExit):
        pe.load_profiles()


def test_b15_deep_variant_matrix_enforced(tmp_path, monkeypatch):
    # cu126 en macOS NO debe declararse (solo Linux)
    _write_profiles(
        tmp_path,
        monkeypatch,
        lambda prof: prof["profiles"]["deep"]["variants"]["cu126"].update(
            {"Darwin-arm64": "locks/deep-macos-arm64.txt"}
        ),
    )
    with pytest.raises(SystemExit):
        pe.load_profiles()


def test_b15_model_requires_cpu_torch(tmp_path, monkeypatch):
    _write_profiles(tmp_path, monkeypatch, lambda prof: prof["profiles"]["model"].pop("cpu_torch"))
    with pytest.raises(SystemExit):
        pe.load_profiles()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
