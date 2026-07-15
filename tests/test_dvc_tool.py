"""Adversarial del perfil dvc-tool (P0R.5 · D6): contrato de locks, aceptación acotada de
PYSEC-2026-2447 (diskcache) y guard de caché DVC. La herramienta DVC está AISLADA del producto.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess

import pytest

import tools.audit_python_supply_chain as m
import tools.check_no_legacy_envs as legacy
import tools.check_no_stray_dvc as stray
import tools.dvc_cache_guard as guard
import tools.lock_contracts as lc

_H = "sha256:" + "a" * 64
MAC = "locks/dvc-tool-macos-arm64.txt"
LNX = "locks/dvc-tool-linux-x86_64.txt"


def _dvc_lock(pins: dict) -> str:
    return "\n".join(f"{k}=={v} \\\n    --hash={_H}" for k, v in pins.items()) + "\n"


def _valid_pins(**over) -> dict:
    p = {"dvc": "3.67.1", "dvc-s3": "3.3.0", "diskcache": "5.6.3"}
    p.update(over)
    return p


def _dvc_root(tmp_path, mac=None, lnx=None, dvc_in=None):
    root = tmp_path / "repo"
    (root / "locks").mkdir(parents=True)
    (root / "requirements").mkdir(parents=True)
    (root / "requirements/dvc.in").write_text(
        dvc_in if dvc_in is not None else "\n".join(f"{k}=={v}" for k, v in lc.DVC_TOOL_DIRECT.items()) + "\n"
    )
    (root / MAC).write_text(_dvc_lock(mac or _valid_pins()))
    (root / LNX).write_text(_dvc_lock(lnx or _valid_pins()))
    return root


# ----------------------------- contrato dvc-tool -----------------------------


def test_valid_dvc_tool_passes(tmp_path):
    assert lc.validate_dvc_tool(_dvc_root(tmp_path)) == []


def test_diskcache_wrong_version_blocks(tmp_path):
    root = _dvc_root(tmp_path, mac=_valid_pins(diskcache="5.6.4"))
    assert any("diskcache" in x for x in lc.validate_dvc_tool(root))


def test_diskcache_absent_blocks(tmp_path):
    root = _dvc_root(tmp_path, mac={"dvc": "3.67.1", "dvc-s3": "3.3.0"})
    assert any("diskcache" in x for x in lc.validate_dvc_tool(root))


def test_wrong_dvc_version_blocks(tmp_path):
    root = _dvc_root(tmp_path, mac=_valid_pins(dvc="3.99.0"))
    assert any("dvc:" in x for x in lc.validate_dvc_tool(root))


def test_dvc_in_extra_pin_blocks(tmp_path):
    root = _dvc_root(tmp_path, dvc_in="dvc[s3]==3.67.1\ndvc-s3==3.3.0\nextra==1.0.0\n")
    assert any("requirements/dvc.in" in x for x in lc.validate_dvc_tool(root))


def test_cross_platform_divergence_blocks(tmp_path):
    root = _dvc_root(tmp_path, lnx=_valid_pins(dvc="3.66.0"))  # dvc distinto en linux
    assert any("divergencia de versión pública de dvc" in x for x in lc.validate_dvc_tool(root))


def test_no_dvc_in_product_locks(tmp_path):
    # un lock de producto con diskcache -> bloquea
    root = tmp_path / "repo"
    (root / "locks").mkdir(parents=True)
    (root / "locks/runtime.txt").write_text("alpha==1.0.0\ndiskcache==5.6.3\n")
    probs = lc.validate_no_dvc_in_product(root)
    assert any("diskcache" in x and "runtime" in x for x in probs)


def test_real_dvc_tool_contract_holds():
    assert lc.validate_dvc_tool(lc.ROOT) == []
    assert lc.validate_no_dvc_in_product(lc.ROOT) == []


# ----------------------------- aceptación acotada del advisory -----------------------------

TODAY = dt.date(2026, 7, 20)
DISK_ENTRY = {
    "id": "PYSEC-2026-2447",
    "aliases": ["CVE-2025-69872", "GHSA-w8v5-vhqr-4h9v"],
    "package": "diskcache",
    "versions": ["5.6.3"],
    "profiles": ["dvc-tool"],
    "locks": [MAC, LNX],
    "decision": "accept",
    "severity": "moderate",
    "scope": "local",
    "owner": "Javier",
    "expires_at": "2026-08-12",
    "rationale": "fixture",
}
OBS = {"package": "diskcache", "version": "5.6.3", "id": "PYSEC-2026-2447", "aliases": ["CVE-2025-69872"]}


def test_disk_advisory_accepted_in_dvc_lock():
    assert m.reconcile_lock([OBS], [DISK_ENTRY], profile="dvc-tool", lock=MAC, today=TODAY) == []


def test_disk_advisory_in_product_lock_blocks():
    # el mismo aviso observado en un lock de producto (model) -> NO permitido
    probs = m.reconcile_lock([OBS], [DISK_ENTRY], profile="model", lock="locks/model-cpu.txt", today=TODAY)
    assert any("NO permitido" in p for p in probs)


def test_disk_advisory_absent_orphan_blocks():
    # permitido en MAC pero no observado -> huérfana
    probs = m.reconcile_lock([], [DISK_ENTRY], profile="dvc-tool", lock=MAC, today=TODAY)
    assert any("HUÉRFANA" in p for p in probs)


def test_disk_advisory_wrong_version_blocks():
    bad = {**OBS, "version": "5.6.4"}
    probs = m.reconcile_lock([bad], [DISK_ENTRY], profile="dvc-tool", lock=MAC, today=TODAY)
    assert any("versión" in p for p in probs)


def test_real_advisories_have_exactly_two():
    entries = m.load_advisories(m.ADVISORIES)
    assert m.validate_advisory_schema(entries) == []
    ids = {e["id"] for e in entries}
    assert ids == {"PYSEC-2026-3043", "PYSEC-2026-2447"}


# ----------------------------- guard de caché DVC (R4 endurecido) -----------------------------


def _cache_root(tmp_path):
    root = tmp_path / "repo"
    (root / ".dvc/cache").mkdir(parents=True)
    (root / ".dvc/tmp").mkdir(parents=True)
    for d in (".dvc", ".dvc/cache", ".dvc/tmp"):
        os.chmod(root / d, 0o700)
    return root


def test_cache_guard_valid_passes(tmp_path):
    assert guard.check(_cache_root(tmp_path)) == []


def test_cache_guard_symlink_blocks(tmp_path):
    root = _cache_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".dvc/cache").rmdir()
    (root / ".dvc/cache").symlink_to(outside)
    assert any("symlink" in x for x in guard.check(root))


def test_cache_guard_world_writable_blocks(tmp_path):
    root = _cache_root(tmp_path)
    os.chmod(root / ".dvc/cache", 0o777)
    assert any("escribible por grupo/otros" in x for x in guard.check(root))


def test_cache_guard_group_writable_blocks(tmp_path):
    root = _cache_root(tmp_path)
    os.chmod(root / ".dvc/tmp", 0o770)
    assert any(".dvc/tmp" in x and "escribible" in x for x in guard.check(root))


def test_cache_guard_parent_dvc_unsafe_blocks(tmp_path):
    # R4: el padre .dvc inseguro (escribible por grupo/otros) DEBE bloquear aunque cache/tmp sean 0700
    root = _cache_root(tmp_path)
    os.chmod(root / ".dvc", 0o777)
    assert any(x.startswith(".dvc ") and "escribible" in x for x in guard.check(root))


def test_cache_guard_config_override_blocks(tmp_path):
    root = _cache_root(tmp_path)
    (root / ".dvc/config").write_text("[cache]\n    dir = /var/shared/dvccache\n")
    assert any("override de caché" in x for x in guard.check(root))


def test_cache_guard_config_local_override_blocks(tmp_path):
    # R4: config.local también se inspecciona (antes solo config -> falso verde)
    root = _cache_root(tmp_path)
    (root / ".dvc/config.local").write_text("[cache]\n    dir = /tmp/evil\n")
    assert any("config.local" in x and "override" in x for x in guard.check(root))


def test_cache_guard_missing_dirs_with_safe_parent_ok(tmp_path):
    root = tmp_path / "repo"
    (root / ".dvc").mkdir(parents=True)
    os.chmod(root / ".dvc", 0o700)
    assert guard.check(root) == []


def test_cache_guard_missing_dirs_with_unsafe_parent_blocks(tmp_path):
    # R4: sin cache/tmp pero con .dvc escribible por otros -> NO es seguro
    root = tmp_path / "repo"
    (root / ".dvc").mkdir(parents=True)
    os.chmod(root / ".dvc", 0o707)
    assert any(x.startswith(".dvc ") for x in guard.check(root))


def test_cache_guard_prepare_creates_0700(tmp_path):
    root = tmp_path / "repo"
    (root / ".dvc").mkdir(parents=True)
    os.chmod(root / ".dvc", 0o700)
    guard.prepare(root)
    for name in ("cache", "tmp"):
        assert (root / ".dvc" / name).exists()
        assert (os.stat(root / ".dvc" / name).st_mode & 0o777) == 0o700


# ----------------------------- gate anti-DVC-suelto (R4, git ls-files) -----------------------------


def _git_repo(tmp_path, files: dict):
    root = tmp_path / "repo"
    root.mkdir()
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def test_stray_real_repo_is_governed():
    assert stray.check(lc.ROOT) == []


def test_stray_bare_dvc_blocks(tmp_path):
    root = _git_repo(tmp_path, {".github/workflows/x.yml": "run: |\n  dvc commit --force panel\n"})
    assert any("sin el wrapper" in p for p in stray.check(root))


def test_stray_dvc_version_flag_blocks(tmp_path):
    # R4/B3: `dvc --version` (flag, no verbo) también debe caer
    root = _git_repo(tmp_path, {"x.sh": "dvc --version\n"})
    assert any("sin el wrapper" in p for p in stray.check(root))


def test_stray_yaml_extension_scanned(tmp_path):
    # R4/B3: .yaml (no solo .yml)
    root = _git_repo(tmp_path, {"a.yaml": "run: dvc status\n"})
    assert any("sin el wrapper" in p for p in stray.check(root))


def test_stray_python_subprocess_blocks(tmp_path):
    # R4/B3: invocación por subprocess en .py
    root = _git_repo(tmp_path, {"m.py": 'import subprocess\nsubprocess.run(["dvc", "status"])\n'})
    assert any("subprocess fuera del wrapper" in p for p in stray.check(root))


def test_stray_python_os_system_blocks(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'import os\nos.system("dvc push")\n'})
    assert any("os.system fuera del wrapper" in p for p in stray.check(root))


def test_stray_dvc_bin_and_legacy_block(tmp_path):
    root = _git_repo(tmp_path, {"Makefile": "DVC_BIN ?= ante/bin/dvc\n"})
    probs = stray.check(root)
    assert any("DVC_BIN" in p for p in probs)
    assert any("legacy" in p for p in probs)


def test_stray_dollar_dvc_without_wrapper_def_blocks(tmp_path):
    root = _git_repo(tmp_path, {"s.sh": "$DVC add models\n"})
    assert any("sin definir DVC como el wrapper" in p for p in stray.check(root))


def test_stray_wrapper_form_passes(tmp_path):
    root = _git_repo(
        tmp_path,
        {
            "x.yml": "run: python -m tools.python_env exec --profile dvc-tool -- dvc status\n",
            "Makefile": "DVC = python -m tools.python_env exec --profile dvc-tool -- dvc\nrepro:\n\t$(DVC) repro\n",
        },
    )
    assert stray.check(root) == []


def test_stray_fixture_string_not_flagged(tmp_path):
    # una cadena de datos con 'dvc' en un .py (fixture write_text) NO es invocación real
    root = _git_repo(tmp_path, {"t.py": '(p).write_text("run: |\\n  dvc commit\\n")\n'})
    assert stray.check(root) == []


# ----------------------------- C1: regresiones guard/gate (B7/B8) -----------------------------


def test_b7_broken_dvc_symlink_blocks(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".dvc").symlink_to(root / "nope")  # symlink roto
    assert any("symlink" in x for x in guard.check(root))


def test_b7_dvc_not_directory_blocks(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".dvc").write_text("x")
    assert any("no es un directorio" in x for x in guard.check(root))


def test_b7_external_global_config_blocks(tmp_path, monkeypatch):
    root = _cache_root(tmp_path)
    ext = tmp_path / "gconf"
    ext.write_text("[cache]\n    dir = /var/evil\n")
    monkeypatch.setattr(guard, "_external_config_layers", lambda: [ext])
    assert any("config externa" in x for x in guard.check(root))


def test_b8_gate_alias_and_tuple_and_module(tmp_path):
    root = _git_repo(
        tmp_path,
        {
            "a.py": "import subprocess as sp\nsp.run(['dvc','status'])\n",
            "b.py": "import subprocess\nsubprocess.run(('dvc','status'))\n",
            "c.sh": "python -m dvc status\n",
            "e.py": "from subprocess import run as r\nr(['dvc','push'])\n",
            "f.py": "import subprocess\nsubprocess.run(['python','-m','dvc','status'])\n",
        },
    )
    probs = stray.check(root)
    for f in ("a.py", "b.py", "c.sh", "e.py", "f.py"):
        assert any(f in p for p in probs), f"no cazó {f}"


def test_b8_gate_fail_closed_on_git_failure(tmp_path):
    with pytest.raises(SystemExit):
        stray.check(tmp_path / "not-a-git-repo")


def test_b8_gate_pipx_uv_install_dvc_block(tmp_path):
    root = _git_repo(tmp_path, {"x.sh": "pipx install dvc\nuv tool install dvc\n"})
    probs = stray.check(root)
    assert sum("fuera de tools/python_env.py" in p for p in probs) >= 2


# ----------------------------- C1: regresiones R8R2 (B11/B16) -----------------------------


def test_b11_prepare_creates_site_cache_0700(tmp_path):
    root = tmp_path / "repo"
    (root / ".dvc").mkdir(parents=True)
    os.chmod(root / ".dvc", 0o700)
    guard.prepare(root)
    for rel in ("site-cache", "site-cache/repo"):
        assert (os.stat(root / ".dvc" / rel).st_mode & 0o777) == 0o700, rel


def test_b11_external_site_cache_dir_blocks(tmp_path, monkeypatch):
    root = _cache_root(tmp_path)
    monkeypatch.setenv("DVC_SITE_CACHE_DIR", "/var/evil")
    assert any("DVC_SITE_CACHE_DIR" in x for x in guard.check(root))


def test_b11_confined_site_cache_dir_ok(tmp_path, monkeypatch):
    root = _cache_root(tmp_path)
    guard.prepare(root)
    monkeypatch.setenv("DVC_SITE_CACHE_DIR", str(guard.site_cache_dir(root)))
    assert guard.check(root) == []


def test_b11_site_cache_group_writable_blocks(tmp_path):
    root = _cache_root(tmp_path)
    guard.prepare(root)
    os.chmod(root / ".dvc/site-cache/repo", 0o770)
    assert any("site-cache/repo" in x and "escribible" in x for x in guard.check(root))


def test_b16_legacy_gate_detects_py_subprocess(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'import subprocess\nsubprocess.run(["ante_nf/bin/python", "x.py"])\n'})
    # sin baseline en el repo temporal -> current_counts lo cuenta
    assert legacy.current_counts(root).get("m.py") == 1


def test_b16_legacy_gate_ignores_py_comment(tmp_path):
    root = _git_repo(tmp_path, {"m.py": "# ante/bin/python es legacy, migrar\nx = 1\n"})
    assert "m.py" not in legacy.current_counts(root)


def test_b16_baseline_total_must_match():
    import json as _json

    doc = _json.loads((legacy.BASELINE).read_text())
    assert doc["total"] == sum(doc["max_per_file"].values())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
