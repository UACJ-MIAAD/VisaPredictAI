"""Contrato del sistema de entornos content-addressed (P0R.5 · R1/R2). Unit rápido: determinismo del
env_id, ausencia de rutas/fechas en el descriptor, canonicalización PEP 503, env_owns, y la lógica de
ready_valid (reuso/tamper) con _pip_freeze monkeypatcheado. El BUILD real + smoke lo prueba el job CI
`dvc-tool-install` en Linux+macOS (evita ~1 min de red por corrida de unit tests)."""

from __future__ import annotations

import json
import os

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
    monkeypatch.setattr(pe, "_file_hashes", lambda p, cfg: {"bin/dvc": "h"})
    monkeypatch.setattr(pe, "_inventory_problems", lambda obs, prof, var, profiles: [])
    monkeypatch.setattr(pe.lc, "validate_all", lambda root: [])
    sealed = _FREEZE if digest_ok else ["gamma==9.9.9"]
    meta = {
        "schema_version": 1,
        "env_id": "KNOWNID" if env_id_ok else "OTHER",
        "descriptor": _DESC,
        "inventory": sealed,
        "inventory_digest": pe._inventory_digest(sealed),
        "file_hashes": {"bin/dvc": "h"},
        "tree_digest": "TREE",
        "pip_check": "ok",
        "n_packages": len(sealed),
    }
    (envp / "READY.json").write_text(json.dumps(meta))
    os.chmod(envp, 0o700)  # B40/B41: un entorno legítimo es 0700 del UID actual
    return envp


def test_ready_valid_ok(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch)
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert ok, why


def test_ready_valid_no_ready(tmp_path, monkeypatch):
    # dir de entorno existente y 0700 pero SIN READY.json -> no se reusa
    envp = tmp_path / "env"
    envp.mkdir()
    os.chmod(envp, 0o700)
    monkeypatch.setattr(pe, "env_id", lambda *a, **k: "KNOWNID")
    ok, why = pe.ready_valid(envp, "dvc-tool")
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
    staging = tmp_path / ".staging"
    staging.mkdir()
    os.chmod(staging, 0o700)  # B42: el padre debe ser 0700 del UID actual
    monkeypatch.setattr(pe, "STAGING_ROOT", staging)
    victim = staging / (("a" * 64) + ".tmp7890")  # nombre válido <env_id>.<sufijo mkdtemp>
    victim.mkdir()
    n = pe.prune_staging()
    assert n == 1 and not victim.exists()


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
    # B43: el bytecode SÍ cuenta ahora — un .pyc plantado (código sin tocar la fuente) altera el árbol
    h2 = pe._tree_digest(d)
    (d / "lib" / "__pycache__").mkdir()
    (d / "lib" / "__pycache__" / "x.pyc").write_text("junk")
    assert pe._tree_digest(d) != h2


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


# ----------------------------- C1: regresiones R8R3 (B20/B21) -----------------------------


@pytest.mark.parametrize(
    "mut,frag",
    [
        ({"schema_version": 999}, "schema_version"),
        ({"pip_check": "BROKEN"}, "pip_check"),
        ({"n_packages": 999}, "n_packages"),
        ({"inventory_digest": "sha256:garbage"}, "inventory_digest"),
        ({"file_hashes": {}}, "file_hashes"),
    ],
)
def test_b20_ready_semantic_rejects(tmp_path, monkeypatch, mut, frag):
    envp = _fake_env(tmp_path, monkeypatch)
    meta = json.loads((envp / "READY.json").read_text())
    meta.update(mut)
    (envp / "READY.json").write_text(json.dumps(meta))
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and frag in why


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["profiles"]["dvc-tool"].update(cache_guarded=1),
        lambda p: p["profiles"]["dvc-tool"].update(console_scripts="dvc"),
        lambda p: p["toolchain"].update(pip=26),
        lambda p: p["profiles"]["model"].update(cpu_index="https://evil/whl"),
        lambda p: p["profiles"]["model"].update(cpu_torch="2.13.0"),
        lambda p: p.update(evil=1),
    ],
)
def test_b21_strict_types_reject(tmp_path, monkeypatch, mutate):
    _write_profiles(tmp_path, monkeypatch, mutate)
    with pytest.raises(SystemExit):
        pe.load_profiles()


# ----------------------------- C1: regresiones R8R4 (B28/B29/B32) -----------------------------


@pytest.mark.parametrize("field", ["schema_version", "n_packages"])
def test_b28_bool_not_accepted_as_int(tmp_path, monkeypatch, field):
    envp = _fake_env(tmp_path, monkeypatch)
    meta = json.loads((envp / "READY.json").read_text())
    meta[field] = True  # True == 1 pero NO es int por identidad de tipo
    (envp / "READY.json").write_text(json.dumps(meta))
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "no es int" in why


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["profiles"]["model"]["install_mode"].update({"Windows-x86_64": "constraint-model"}),
        lambda p: p["profiles"]["model"]["install_mode"].pop("Linux-x86_64"),
        lambda p: p["profiles"]["runtime"].update(project_source="none"),
        lambda p: p["profiles"]["dvc-tool"].update(install_mode="version-locked"),
    ],
)
def test_b29_exact_install_mode_and_project_source(tmp_path, monkeypatch, mutate):
    _write_profiles(tmp_path, monkeypatch, mutate)
    with pytest.raises(SystemExit):
        pe.load_profiles()


def test_b32_rename_noreplace_rejects_existing(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")
    tgt = tmp_path / "tgt"
    tgt.mkdir()  # target VACÍO existente
    with pytest.raises(SystemExit):
        pe._rename_noreplace(src, tgt)
    assert src.exists() and not (tgt / "f").exists()  # nada se movió


def test_b32_rename_noreplace_creates_new(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")
    dst = tmp_path / "dst"
    pe._rename_noreplace(src, dst)
    assert (dst / "f").exists() and not src.exists()


# ----------------------------- C1: regresiones R8R5 (B33-B39) -----------------------------


def test_b34_inventory_rejects_extra_and_wrong_toolchain():
    exp = pe.expected_inventory("dvc-tool")
    obs = dict(exp)
    obs["evil-extra"] = "9.9"
    obs["pip"] = "0.0.0"
    probs = pe._inventory_problems(obs, "dvc-tool", None, pe.load_profiles())
    assert any("EXTRA" in p for p in probs) and any("incorrectos" in p for p in probs)


def test_b34_inventory_exact_closure_passes():
    # el cierre esperado exacto NO da problemas
    exp = pe.expected_inventory("dvc-tool")
    assert pe._inventory_problems(dict(exp), "dvc-tool", None, pe.load_profiles()) == []


def test_b35_pin_fullmatch_rejects_trailing():
    assert pe._PIN.match("alpha==1 TRAILING")  # match parcial (el bug)
    assert not pe._PIN.fullmatch("alpha==1 TRAILING")  # fullmatch lo rechaza (el fix)


def test_b36_schema_version_bool_rejected(tmp_path, monkeypatch):
    _write_profiles(tmp_path, monkeypatch, lambda p: p.__setitem__("schema_version", True))
    with pytest.raises(SystemExit):
        pe.load_profiles()


def test_b38_ensure_governed_dir_rejects_symlink(tmp_path):
    outside = tmp_path / "out"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(SystemExit):
        pe._ensure_governed_dir(link, create=True, require_mode=0o700)


def test_b38_ensure_governed_dir_rejects_wrong_mode(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    with pytest.raises(SystemExit):
        pe._ensure_governed_dir(d, create=False, require_mode=0o700)


def test_b39_open_lock_rejects_wrong_mode(tmp_path):
    lock = tmp_path / ".lock-x"
    lock.write_text("")
    os.chmod(lock, 0o666)
    with pytest.raises(SystemExit):
        pe._open_lock(lock)


def test_b39_open_lock_rejects_symlink(tmp_path):
    target = tmp_path / "real"
    target.write_text("")
    lock = tmp_path / ".lock-x"
    lock.symlink_to(target)
    with pytest.raises((SystemExit, OSError)):
        pe._open_lock(lock)


def test_b33_build_extra_aborts_before_ready(tmp_path, monkeypatch):
    # recorre build() con instalación simulada que inyecta un paquete EXTRA -> aborta y NO sella READY
    monkeypatch.setattr(pe, "ENVS_ROOT", tmp_path / ".vp_envs")
    monkeypatch.setattr(pe, "STAGING_ROOT", tmp_path / ".vp_envs" / ".staging")

    def fake_create(path, **kw):
        (path / "bin").mkdir(parents=True)
        (path / "bin" / "python").write_text("#!/bin/sh\n")
        (path / "pyvenv.cfg").write_text("x")

    monkeypatch.setattr(pe.venv, "create", fake_create)
    monkeypatch.setattr(pe, "_install", lambda *a, **k: None)
    monkeypatch.setattr(pe, "_pip_check", lambda py: True)
    exp = pe.expected_inventory("dvc-tool")
    freeze = [f"{n}=={v}" for n, v in exp.items()] + ["evil-extra==9.9"]
    monkeypatch.setattr(pe, "_pip_freeze", lambda py: freeze)
    monkeypatch.setattr(pe, "_tree_digest", lambda p: "T")
    monkeypatch.setattr(pe, "_file_hashes", lambda p, cfg: {"x": "h"})
    with pytest.raises(SystemExit):
        pe.build("dvc-tool")
    target = pe.env_dir("dvc-tool")
    assert not target.exists()  # nada sellado
    assert not list((tmp_path / ".vp_envs" / ".staging").glob("*"))  # staging limpiado


# ----------------------------- C1: regresiones R8R6 (B40-B43) -----------------------------


def test_b40_ready_valid_rejects_env_mode_0777(tmp_path, monkeypatch):
    # un entorno por lo demás válido pero con el DIR en 0777 no se reusa (permisos de grupo/otros)
    envp = _fake_env(tmp_path, monkeypatch)
    os.chmod(envp, 0o777)
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "modo" in why


def test_b40_ready_valid_rejects_ready_hardlink(tmp_path, monkeypatch):
    # READY.json con hardlink (nlink>1) — otro path podría reescribir el sello — se rechaza
    envp = _fake_env(tmp_path, monkeypatch)
    os.link(envp / "READY.json", envp / "READY.alias")  # nlink==2
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "hardlink" in why


def test_b41_ready_valid_rejects_env_symlink(tmp_path, monkeypatch):
    # el DIR del entorno es un symlink a un entorno real 0700 -> prohibido (podría apuntar fuera del repo)
    real = _fake_env(tmp_path, monkeypatch)
    link = tmp_path / "link_env"
    link.symlink_to(real)
    ok, why = pe.ready_valid(link, "dvc-tool")
    assert not ok and "symlink" in why


def test_b41_ready_valid_rejects_ready_symlink(tmp_path, monkeypatch):
    # READY.json es un symlink a un sello externo -> se rechaza antes de leerlo
    envp = _fake_env(tmp_path, monkeypatch)
    real_ready = tmp_path / "ready_real.json"
    (envp / "READY.json").rename(real_ready)
    (envp / "READY.json").symlink_to(real_ready)
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "symlink" in why


def _staging(tmp_path, monkeypatch):
    s = tmp_path / ".staging"
    s.mkdir()
    os.chmod(s, 0o700)
    monkeypatch.setattr(pe, "STAGING_ROOT", s)
    return s


def test_b42_prune_aborts_on_bad_name(tmp_path, monkeypatch):
    # una entrada con nombre que NO casa <env_id>.<sufijo> aborta el prune con CERO borrados
    s = _staging(tmp_path, monkeypatch)
    good = s / (("b" * 64) + ".tmp0000")
    good.mkdir()
    (s / "unexpected").mkdir()
    with pytest.raises(SystemExit):
        pe.prune_staging()
    assert good.exists() and (s / "unexpected").exists()  # nada se borró


def test_b42_prune_aborts_on_symlink_child(tmp_path, monkeypatch):
    s = _staging(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (s / (("c" * 64) + ".tmp0000")).symlink_to(outside)  # nombre válido pero es symlink
    with pytest.raises(SystemExit):
        pe.prune_staging()
    assert outside.exists()


def test_b42_prune_aborts_on_wrong_mode_root(tmp_path, monkeypatch):
    s = _staging(tmp_path, monkeypatch)
    os.chmod(s, 0o755)  # padre no es 0700
    with pytest.raises(SystemExit):
        pe.prune_staging()


def test_b43_env_no_pyc_sets_flag():
    assert pe._env_no_pyc({"A": "1"})["PYTHONDONTWRITEBYTECODE"] == "1"
    assert pe._env_no_pyc({"A": "1"})["A"] == "1"  # preserva el resto del env base


def test_b43_purge_bytecode_removes_pyc(tmp_path):
    (tmp_path / "lib" / "__pycache__").mkdir(parents=True)
    (tmp_path / "lib" / "__pycache__" / "x.pyc").write_text("junk")
    (tmp_path / "lib" / "y.pyo").write_text("junk")
    (tmp_path / "lib" / "keep.py").write_text("a = 1\n")
    pe._purge_bytecode(tmp_path)
    assert not (tmp_path / "lib" / "__pycache__").exists()
    assert not (tmp_path / "lib" / "y.pyo").exists()
    assert (tmp_path / "lib" / "keep.py").exists()  # la fuente permanece


def test_b43_tree_excludes_only_ready():
    # el ÚNICO fichero excluido del sello es READY.json; el bytecode ya NO se excluye
    assert pe._TREE_EXCLUDE_NAMES == {"READY.json"}
    assert pe._TREE_EXCLUDE_SUFFIX == () and pe._TREE_EXCLUDE_DIRS == set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
