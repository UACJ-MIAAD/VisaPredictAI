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


def test_unknown_profile_and_bad_console_script():
    with pytest.raises(SystemExit):
        pe.descriptor("nope")
    # B67: `run` valida el console-script contra `console_scripts` del perfil (sin resolver por ruta).
    with pytest.raises(SystemExit):
        pe.run("dvc-tool", ["python"])  # `python` no es console-script declarado


# ----------------------------- ready_valid: reuso vs tamper -----------------------------

_FREEZE = ["alpha==1.0.0", "beta==2.0.0"]


_DESC = {"fake": "descriptor"}


def _fake_env(tmp_path, monkeypatch, *, digest_ok=True, env_id_ok=True, envp=None):
    if envp is None:
        envp = tmp_path / "env"
    (envp / "bin").mkdir(parents=True)
    (envp / "bin" / "python").write_text("#!/bin/sh\n")  # existe; no se ejecuta (freeze monkeypatched)
    # B58: ready_valid ahora hashea/inventaría POR el descriptor (env_fd) — se parchean las variantes `_at`.
    monkeypatch.setattr(pe, "_pip_freeze_at", lambda fd, env=None: _FREEZE)
    monkeypatch.setattr(pe, "_pip_check_at", lambda fd, env=None: True)
    monkeypatch.setattr(pe, "env_id", lambda *a, **k: "KNOWNID")
    monkeypatch.setattr(pe, "descriptor", lambda *a, **k: _DESC)
    monkeypatch.setattr(pe, "_tree_digest_at", lambda fd: "TREE")
    monkeypatch.setattr(pe, "_file_hashes_at", lambda fd, cfg: {"bin/dvc": "h"})
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
    os.chmod(envp / "READY.json", 0o600)  # B47: el sello legítimo es 0600
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
    assert set(gov) == {
        "python_env_sha256",
        "dvc_cache_guard_sha256",
        "profiles_json_sha256",
        "execution_contract_sha256",  # R9.1
    }
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
    monkeypatch.setattr(pe, "_pip_freeze_at", lambda fd, env=None: _FREEZE + ["evil==6.6.6"])
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "TAMPER" in why


def test_b4_pip_check_on_reuse(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch)
    monkeypatch.setattr(pe, "_pip_check_at", lambda fd, env=None: False)  # pip check roto en reuso
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
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "nonce").mkdir()
    (sp / "nonce" / "f").write_text("x")
    dp = tmp_path / "dp"
    dp.mkdir()
    (dp / "env").mkdir()  # target VACÍO existente
    sfd = _dir_fd(sp)
    dfd = _dir_fd(dp)
    try:
        with pytest.raises(SystemExit):
            pe._rename_noreplace(sfd, "nonce", dfd, "env")
        assert (sp / "nonce").exists() and not (dp / "env" / "f").exists()  # nada se movió
    finally:
        os.close(sfd)
        os.close(dfd)


def test_b32_rename_noreplace_creates_new(tmp_path):
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "nonce").mkdir()
    (sp / "nonce" / "f").write_text("x")
    dp = tmp_path / "dp"
    dp.mkdir()
    sfd = _dir_fd(sp)
    dfd = _dir_fd(dp)
    try:
        pe._rename_noreplace(sfd, "nonce", dfd, "env")
        assert (dp / "env" / "f").exists() and not (sp / "nonce").exists()
    finally:
        os.close(sfd)
        os.close(dfd)


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


def test_b38_openat_dir_rejects_symlink(tmp_path):
    outside = tmp_path / "out"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(SystemExit):
        pe._open_governed_chain(tmp_path, ["link"], create=False, require_mode=0o700)


def test_b38_openat_dir_rejects_wrong_mode(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    with pytest.raises(SystemExit):
        pe._open_governed_chain(tmp_path, ["d"], create=False, require_mode=0o700)


def _dir_fd(path):
    return os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def test_b39_open_lock_rejects_wrong_mode(tmp_path):
    lock = tmp_path / ".lock-x"
    lock.write_text("")
    os.chmod(lock, 0o666)
    fd = _dir_fd(tmp_path)
    try:
        with pytest.raises(SystemExit):
            pe._open_lock(fd, ".lock-x")
    finally:
        os.close(fd)


def test_b39_open_lock_rejects_symlink(tmp_path):
    target = tmp_path / "real"
    target.write_text("")
    lock = tmp_path / ".lock-x"
    lock.symlink_to(target)
    fd = _dir_fd(tmp_path)
    try:
        with pytest.raises((SystemExit, OSError)):
            pe._open_lock(fd, ".lock-x")
    finally:
        os.close(fd)


def test_b33_build_extra_aborts_before_ready(tmp_path, monkeypatch):
    # recorre build() con instalación simulada que inyecta un paquete EXTRA -> aborta y NO sella READY
    envs = tmp_path / ".vp_envs"
    envs.mkdir()
    os.chmod(envs, 0o700)  # ancla de la cadena openat (build valida ROOT→.vp_envs→perfil)
    monkeypatch.setattr(pe, "ENVS_ROOT", envs)
    monkeypatch.setattr(pe, "STAGING_ROOT", envs / ".staging")

    monkeypatch.setattr(pe, "_venv_create_at", lambda dir_fd: None)
    monkeypatch.setattr(pe, "_install_at", lambda *a, **k: None)
    monkeypatch.setattr(pe, "_pip_check_at", lambda dir_fd, env=None: True)
    exp = pe.expected_inventory("dvc-tool")
    freeze = [f"{n}=={v}" for n, v in exp.items()] + ["evil-extra==9.9"]
    monkeypatch.setattr(pe, "_pip_freeze_at", lambda dir_fd, env=None: freeze)
    monkeypatch.setattr(pe, "_tree_digest_at", lambda dir_fd: "T")
    monkeypatch.setattr(pe, "_file_hashes_at", lambda dir_fd, cfg: {"x": "h"})
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


# ----------------------------- C1: regresiones R8R6R (B46/B47/B48) -----------------------------


def test_b46_parent_symlink_rejected(tmp_path, monkeypatch):
    # el mismo entorno VÁLIDO alcanzado vía un PADRE symlink NO se reusa (la cadena openat lo caza)
    _fake_env(tmp_path / "realbase", monkeypatch)  # crea tmp/realbase/env válido 0700
    (tmp_path / "linkbase").symlink_to(tmp_path / "realbase")
    via = tmp_path / "linkbase" / "env"
    ok, why = pe.ready_valid(via, "dvc-tool")
    assert not ok and "insegura" in why


def test_b46_vp_envs_symlink_under_root_rejected(tmp_path, monkeypatch):
    # PRODUCCIÓN: env_path bajo ROOT y `.vp_envs` es symlink a un árbol externo con un env VÁLIDO ⇒ rechazo
    monkeypatch.setattr(pe, "ROOT", tmp_path)
    ext = tmp_path / "external"
    _fake_env(tmp_path, monkeypatch, envp=ext / "dvc-tool" / "KNOWNID")  # env válido externo
    (tmp_path / ".vp_envs").symlink_to(ext)
    via = tmp_path / ".vp_envs" / "dvc-tool" / "KNOWNID"
    ok, why = pe.ready_valid(via, "dvc-tool")
    assert not ok and "insegura" in why


def test_b47_ready_mode_0666_rejected(tmp_path, monkeypatch):
    envp = _fake_env(tmp_path, monkeypatch)
    os.chmod(envp / "READY.json", 0o666)  # el constructor lo sella 0600; 0666 no se acepta
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "0600" in why


def test_b48_prune_parent_symlink_blocks(tmp_path, monkeypatch):
    # un PADRE de .staging es symlink -> prune BLOQUEA (no borra el árbol externo)
    real = tmp_path / "real"
    (real / ".staging").mkdir(parents=True)
    os.chmod(real / ".staging", 0o700)
    witness = real / ".staging" / (("a" * 64) + ".tmp0000")
    witness.mkdir()  # dir por lo demás válido — NO debe borrarse
    (tmp_path / "plink").symlink_to(real)
    monkeypatch.setattr(pe, "STAGING_ROOT", tmp_path / "plink" / ".staging")
    with pytest.raises(SystemExit):
        pe.prune_staging()
    assert witness.exists()  # el borrado externo se bloqueó


def test_b48_prune_broken_symlink_blocks(tmp_path, monkeypatch):
    # .staging es un symlink ROTO -> prune BLOQUEA, no devuelve 0 en silencio
    (tmp_path / ".staging").symlink_to(tmp_path / "does_not_exist")
    monkeypatch.setattr(pe, "STAGING_ROOT", tmp_path / ".staging")
    with pytest.raises(SystemExit):
        pe.prune_staging()


def test_b43_purge_bytecode_aborts_if_residual(tmp_path, monkeypatch):
    # si algo impide borrar el bytecode, _purge_bytecode falla (no sella con .pyc)
    real_unlink = os.unlink

    def stubborn(p, *a, **k):
        if str(p).endswith(".pyc"):
            return  # simula que el borrado no surtió efecto
        return real_unlink(p, *a, **k)

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "x.pyc").write_text("junk")
    monkeypatch.setattr(os, "unlink", stubborn)
    with pytest.raises(SystemExit):
        pe._purge_bytecode(tmp_path)


# ----------------------------- C1: regresiones R8R6R2 (B51/B52) -----------------------------


def test_b51_cleanup_never_deletes_external(tmp_path, monkeypatch):
    # tras crear el staging legítimo, un atacante intercambia `.staging` por un symlink a un árbol externo
    # y fuerza un error; la limpieza (fd-relativa) NUNCA debe borrar el árbol externo.
    envs = tmp_path / ".vp_envs"
    envs.mkdir()
    os.chmod(envs, 0o700)
    monkeypatch.setattr(pe, "ENVS_ROOT", envs)
    monkeypatch.setattr(pe, "STAGING_ROOT", envs / ".staging")
    external = tmp_path / "external"
    external.mkdir()

    def malicious_venv(dir_fd):
        (external / "sub").mkdir(parents=True)  # lo que una limpieza-por-ruta (vieja) resolvería y borraría
        (external / "sub" / "DO_NOT_DELETE").write_text("x")
        (envs / ".staging").rename(envs / ".staging_real")  # swap del ancestro
        (envs / ".staging").symlink_to(external)
        raise RuntimeError("boom")

    monkeypatch.setattr(pe, "_venv_create_at", malicious_venv)
    with pytest.raises(RuntimeError):
        pe.build("dvc-tool")
    assert list(external.rglob("DO_NOT_DELETE")), "la limpieza borró el árbol externo (B51)"


def test_b51_check_ident_detects_swap(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    st = os.stat(d)
    pe._check_ident(d, (st.st_dev, st.st_ino), "x")  # ok
    other = tmp_path / "other"
    other.mkdir()
    ost = os.stat(other)
    with pytest.raises(SystemExit):
        pe._check_ident(d, (ost.st_dev, ost.st_ino), "x")  # inode distinto -> aborta


def test_b52_ready_valid_rejects_inode_swap_during_hash(tmp_path, monkeypatch):
    # un swap del ancestro ENTRE leer READY.json y hashear por ruta debe rechazarse (env_fd/ident vivos)
    envp = _fake_env(tmp_path, monkeypatch)
    other = tmp_path / "other"
    (other / "bin").mkdir(parents=True)
    (other / "bin" / "python").write_text("#!/bin/sh\n")
    os.chmod(other, 0o700)

    def swap_then_digest(fd):
        env = tmp_path / "env"
        if env.is_dir() and not env.is_symlink():
            env.rename(tmp_path / "env_real")
            env.symlink_to(other)  # env_path ahora resuelve a OTRO inode (swap NO restaurado)
        return "TREE"  # coincide con el sello monkeypatcheado -> el hash "pasa"

    monkeypatch.setattr(pe, "_tree_digest_at", swap_then_digest)
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert not ok and "inode" in why.lower()


# ----------------------------- C1: regresiones R8R6R3 (B55/B58) -----------------------------


def test_b55_real_venv_clear_cannot_delete_external(tmp_path, monkeypatch):
    # El fix elimina venv.create(..., clear=True) por RUTA ABSOLUTA. Se instala un swap del ancestro
    # `.staging` en el chequeo pre-venv (tras pasar el chequeo legítimo); la creación del venv debe ser
    # fd-relativa (fchdir al staging_fd ya abierto) y NUNCA borrar el testigo externo. En el head previo,
    # venv.create(clear=True) resolvía la ruta swappeada y wipeaba el árbol externo.
    envs = tmp_path / ".vp_envs"
    envs.mkdir()
    os.chmod(envs, 0o700)
    monkeypatch.setattr(pe, "ENVS_ROOT", envs)
    monkeypatch.setattr(pe, "STAGING_ROOT", envs / ".staging")
    external = tmp_path / "external"
    external.mkdir()

    real_check = pe._check_ident

    def swapping_check(path, ident, what):
        real_check(path, ident, what)  # el chequeo legítimo PASA
        if what == "pre-venv":
            nonce = pe.Path(path).name
            (external / nonce).mkdir(parents=True)
            (external / nonce / "DO_NOT_DELETE").write_text("precious")
            (envs / ".staging").rename(envs / ".staging_real")
            (envs / ".staging").symlink_to(external)  # swap del ancestro tras el chequeo

    monkeypatch.setattr(pe, "_check_ident", swapping_check)
    with pytest.raises(SystemExit):  # el post-venv _check_ident caza el swap y aborta
        pe.build("dvc-tool")
    assert list(external.rglob("DO_NOT_DELETE")), "la creación del venv borró el árbol externo (B55)"


def test_b55_path_swap_during_install_cannot_write_external(tmp_path):
    # Primitivo fd-bound: un subproceso de la fase de instalación debe escribir DENTRO del inode del
    # descriptor (fchdir), no a través de la ruta. Aun con la ruta swappeada a un árbol externo tras abrir
    # el fd, el subproceso escribe en el dir real, no en el externo.
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o700)
    fd = os.open(str(real), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    external = tmp_path / "external"
    external.mkdir()
    try:
        # swap de la ruta a un symlink externo DESPUÉS de abrir el fd
        real.rename(tmp_path / "real_moved")
        (tmp_path / "real").symlink_to(external)
        r = pe._run_in_dir(fd, ["sh", "-c", "echo x > install_probe.txt"], capture=True)
        assert r.returncode == 0
    finally:
        os.close(fd)
    assert (tmp_path / "real_moved" / "install_probe.txt").exists()  # escribió al inode real (fd)
    assert not (external / "install_probe.txt").exists()  # NO al árbol externo swappeado


def test_b58_ready_valid_transient_swap_is_rejected(tmp_path, monkeypatch):
    # B58: los chequeos de identidad que "encapsulan" una operación por ruta no garantizan que la
    # operación usara el mismo inode (un swap transitorio lee el árbol externo y restaura antes del
    # post-chequeo). El fix hashea/inventaría por el DESCRIPTOR gobernado (env_fd), no re-resolviendo la
    # ruta. Regresión: la versión POR RUTA de _tree_digest jamás debe ser invocada por ready_valid.
    envp = _fake_env(tmp_path, monkeypatch)
    called = {"path_tree": False}

    def path_tree_should_not_run(p):
        called["path_tree"] = True
        return "TREE"

    # swap transitorio durante la validación (tras abrir el env_fd, antes de hashear)
    other = tmp_path / "other"
    (other / "bin").mkdir(parents=True)
    (other / "bin" / "python").write_text("#!/bin/sh\n")
    os.chmod(other, 0o700)

    def transient_swap(root):
        env = tmp_path / "env"
        if env.is_dir() and not env.is_symlink():
            env.rename(tmp_path / "env_real")
            env.symlink_to(other)
            env.unlink()
            (tmp_path / "env_real").rename(env)  # restaura antes del post-chequeo
        return []

    monkeypatch.setattr(pe, "_tree_digest", path_tree_should_not_run)
    monkeypatch.setattr(pe.lc, "validate_all", transient_swap)
    ok, why = pe.ready_valid(envp, "dvc-tool")
    assert ok, why  # valida contra el fd gobernado (contenido real), inmune al swap de la ruta
    assert called["path_tree"] is False, "ready_valid usó _tree_digest POR RUTA (vulnerable a swap)"


def test_b58_tree_digest_at_is_swap_immune(tmp_path):
    # _tree_digest_at(dir_fd) hashea el inode del descriptor; un swap de la ruta a otro árbol no cambia
    # el digest (a diferencia de la versión por ruta).
    real = tmp_path / "env"
    (real / "lib").mkdir(parents=True)
    (real / "lib" / "x.py").write_text("REAL\n")
    fd = os.open(str(real), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    external = tmp_path / "external"
    (external / "lib").mkdir(parents=True)
    (external / "lib" / "x.py").write_text("EXTERNAL\n")
    try:
        before = pe._tree_digest_at(fd)
        real.rename(tmp_path / "env_moved")
        (tmp_path / "env").symlink_to(external)  # ruta ahora resuelve a EXTERNAL
        after = pe._tree_digest_at(fd)
        assert before == after  # el fd está anclado al inode real
        ext_fd = os.open(str(external), os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert pe._tree_digest_at(ext_fd) != after  # el árbol externo tiene OTRO digest (si se siguiera la ruta)
        finally:
            os.close(ext_fd)
    finally:
        os.close(fd)


# ----------------------------- C1: regresiones R8R6R4 (B60/B61/B62) -----------------------------

import socket as _socket  # noqa: E402
from contextlib import contextmanager  # noqa: E402


def _script_env(tmp_path, name, marker):
    """Crea <dir>/bin/python como un script sh que escribe `marker` (para detectar QUÉ intérprete corrió)."""
    d = tmp_path / name
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "python").write_text(f"#!/bin/sh\necho ran > {marker}\n")
    os.chmod(d / "bin" / "python", 0o755)
    os.chmod(d, 0o700)
    return d


def test_b60_run_python_ancestor_swap_never_executes_external(tmp_path, monkeypatch):
    # run_python NO puede re-ejecutar <env>/bin/python por RUTA absoluta tras la validación: un swap del
    # ancestro por symlink haría correr el intérprete EXTERNO. El fix ejecuta fd-bound (fchdir al env_fd
    # gobernado ya validado), inmune al swap.
    legit, ext = tmp_path / "LEGIT", tmp_path / "EXTERNAL"
    target = _script_env(tmp_path, "env", legit)
    external = _script_env(tmp_path, "ext", ext)

    @contextmanager
    def fake_open(profile, variant=None, profiles=None):
        fd = os.open(str(target), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        target.rename(tmp_path / "env_real")  # swap del ancestro DESPUÉS de tomar el fd
        (tmp_path / "env").symlink_to(external)
        try:
            yield pe._ValidEnv(fd, {"env_id": "x"}, "x", tmp_path / "env", {"console_scripts": ["dvc"]})
        finally:
            os.close(fd)

    monkeypatch.setattr(pe, "build", lambda *a, **k: target)
    monkeypatch.setattr(pe, "open_valid_environment", fake_open)
    pe.run_python("dvc-tool", ["-c", "pass"], capture=True)
    assert legit.exists(), "no corrió el intérprete del fd gobernado"
    assert not ext.exists(), "run_python ejecutó el intérprete EXTERNO tras el swap (B60)"


def test_b60_console_run_ancestor_swap_never_executes_external(tmp_path, monkeypatch):
    legit, ext = tmp_path / "LEGIT", tmp_path / "EXTERNAL"
    target = _script_env(tmp_path, "env", legit)
    external = _script_env(tmp_path, "ext", ext)

    @contextmanager
    def fake_open(profile, variant=None, profiles=None):
        fd = os.open(str(target), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        target.rename(tmp_path / "env_real")
        (tmp_path / "env").symlink_to(external)
        try:
            yield pe._ValidEnv(fd, {"env_id": "x"}, "x", tmp_path / "env", {"console_scripts": ["dvc"]})
        finally:
            os.close(fd)

    monkeypatch.setattr(pe, "build", lambda *a, **k: target)
    monkeypatch.setattr(pe, "open_valid_environment", fake_open)
    pe.run("dvc-tool", ["dvc", "--version"], capture=True)
    assert legit.exists() and not ext.exists(), "run ejecutó el intérprete EXTERNO tras el swap (B60)"


def test_b60_runtime_uses_same_validated_env_fd(tmp_path, monkeypatch):
    # el lanzamiento es RELATIVO al descriptor: _run_in_dir recibe el fd del handle validado y `bin/python`.
    target = tmp_path / "env"
    (target / "bin").mkdir(parents=True)
    os.chmod(target, 0o700)
    captured: dict = {}

    @contextmanager
    def fake_open(profile, variant=None, profiles=None):
        fd = os.open(str(target), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield pe._ValidEnv(fd, {}, "x", target, {})
        finally:
            os.close(fd)

    def fake_run_in_dir(dir_fd, argv, **kw):
        import subprocess

        captured["fd"], captured["argv"] = dir_fd, argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pe, "build", lambda *a, **k: target)
    monkeypatch.setattr(pe, "open_valid_environment", fake_open)
    monkeypatch.setattr(pe, "_run_in_dir", fake_run_in_dir)
    pe.run_python("dvc-tool", ["-c", "pass"])
    assert captured.get("argv", [None])[0] == "bin/python"  # intérprete relativo, no ruta absoluta
    assert isinstance(captured.get("fd"), int)  # el fd del handle validado


def test_b61_read_ready_uses_same_validated_fd(tmp_path, monkeypatch):
    # read_ready devuelve el sello de la MISMA validación (open_valid_environment), sin cerrar/reabrir.
    sentinel = {"env_id": "abc", "validated": True}

    @contextmanager
    def fake_open(profile, variant=None, profiles=None):
        yield pe._ValidEnv(-1, sentinel, "abc", tmp_path / "env", {})

    monkeypatch.setattr(pe, "open_valid_environment", fake_open)
    assert pe.read_ready("dvc-tool") == sentinel


def test_b61_read_ready_replacement_after_validation_rejected(tmp_path, monkeypatch):
    # el ENTORNO se REEMPLAZA (rename de otro dir real 0700) entre validación y lectura; read_ready no debe
    # devolver el sello no validado.
    real = tmp_path / "env"
    real.mkdir()
    os.chmod(real, 0o700)
    (real / "READY.json").write_text(json.dumps({"legit": True}))
    os.chmod(real / "READY.json", 0o600)
    external = tmp_path / "ext"
    external.mkdir()
    os.chmod(external, 0o700)
    (external / "READY.json").write_text(json.dumps({"evil": True}))
    os.chmod(external / "READY.json", 0o600)

    validated = {"env_id": "legit", "marker": "validated"}

    def fake_validate(env_fd, env_path, profile, variant, profiles, cfg):
        # "validación" pasa sobre el fd real; luego el dir se REEMPLAZA por otro dir real
        real.rename(tmp_path / "env_old")
        external.rename(tmp_path / "env")
        return True, "ok", validated

    monkeypatch.setattr(pe, "env_dir", lambda *a, **k: tmp_path / "env")
    monkeypatch.setattr(pe, "_validate_open_env", fake_validate)
    got = pe.read_ready("dvc-tool")
    assert got == validated and "evil" not in got, f"read_ready devolvió el sello REEMPLAZADO no validado: {got}"


def test_b62_fifo_is_rejected_from_environment_tree(tmp_path):
    os.mkfifo(tmp_path / "x")
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(SystemExit):
            pe._tree_digest_at(fd)
    finally:
        os.close(fd)


def _bind_short(sock, directory):
    """Bind un AF_UNIX socket con nombre corto `x` dentro de `directory` (el límite de ruta AF_UNIX es ~104
    bytes; el tmp_path de pytest lo excede) — chdir + bind relativo, restaurando el cwd."""
    old = os.getcwd()
    os.chdir(str(directory))
    try:
        sock.bind("x")
    finally:
        os.chdir(old)


def test_b62_socket_is_rejected_from_environment_tree(tmp_path):
    s = _socket.socket(_socket.AF_UNIX)
    _bind_short(s, tmp_path)
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(SystemExit):
            pe._tree_digest_at(fd)
    finally:
        os.close(fd)
        s.close()


def test_b62_special_type_replacement_changes_or_blocks_digest(tmp_path):
    # una FIFO y un socket con el MISMO nombre NO pueden colisionar en el digest — el fix los RECHAZA a ambos.
    d1 = tmp_path / "a"
    d1.mkdir()
    os.mkfifo(d1 / "x")
    d2 = tmp_path / "b"
    d2.mkdir()
    s = _socket.socket(_socket.AF_UNIX)
    _bind_short(s, d2)

    def outcome(d):
        fd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            return ("digest", pe._tree_digest_at(fd))
        except SystemExit:
            return ("blocked", None)
        finally:
            os.close(fd)

    r1, r2 = outcome(d1), outcome(d2)
    s.close()
    assert r1[0] == "blocked" and r2[0] == "blocked", f"tipo especial no rechazado: {r1}, {r2}"


# ----------------------------- R9-a: regresiones B64 (script gobernado) / B67 (helpers retirados) -----------------------------


def test_b64_governed_script_rejects_absolute(tmp_path):
    with pytest.raises(SystemExit):
        pe._parse_python_argv(["/tmp/outside.py"])


def test_b64_governed_script_rejects_parent_traversal():
    with pytest.raises(SystemExit):
        pe._parse_python_argv(["../outside.py"])
    with pytest.raises(SystemExit):
        pe._parse_python_argv(["experiments/../../outside.py"])


def test_b64_governed_script_rejects_untracked():
    with pytest.raises(SystemExit):
        pe._parse_python_argv(["experiments/_nonexistent_r9_xyz.py"])


def test_b64_governed_script_rejects_symlink():
    # un symlink DENTRO de ROOT (aunque apunte a un fichero tracked) se rechaza por componente symlink
    link = pe.ROOT / "experiments" / "_r9_evil_link.py"
    target = pe.ROOT / "experiments" / "build_key_facts.py"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    try:
        with pytest.raises(SystemExit):
            pe._parse_python_argv(["experiments/_r9_evil_link.py"])
    finally:
        link.unlink()


def test_b64_governed_script_accepts_tracked_regular():
    spec = pe._parse_python_argv(["experiments/build_key_facts.py", "--flag"])
    assert spec["mode"] == "script"
    assert spec["name"] == "experiments/build_key_facts.py"
    assert spec["rest"] == ["--flag"]


def test_b67_path_resolving_helpers_removed():
    # env_owns / resolve_console_script (resolvían por .resolve()/ruta absoluta) ya no existen como API
    assert not hasattr(pe, "env_owns"), "env_owns sigue expuesto (vector de resolución por ruta, B67)"
    assert not hasattr(pe, "resolve_console_script"), "resolve_console_script sigue expuesto (B67)"


def test_b67_no_production_callers_of_path_resolvers():
    import re as _re

    root = pe.ROOT
    offenders = []
    for sub in ("tools", "pipeline", "vp_model", "vp_data", "experiments"):
        d = root / sub
        if not d.exists():
            continue
        for p in d.rglob("*.py"):
            txt = p.read_text()
            if _re.search(r"\b(env_owns|resolve_console_script)\s*\(", txt):
                offenders.append(str(p.relative_to(root)))
    assert offenders == [], f"callers de producción de los resolvers por ruta: {offenders}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
