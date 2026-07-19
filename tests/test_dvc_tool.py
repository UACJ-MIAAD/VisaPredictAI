"""Adversarial del perfil dvc-tool (P0R.5 · D6): contrato de locks, aceptación acotada de
PYSEC-2026-2447 (diskcache) y guard de caché DVC. La herramienta DVC está AISLADA del producto.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess

import pytest

import tools.audit_python_supply_chain as m
import tools.check_no_legacy_envs as legacy
import tools.check_no_stray_dvc as stray
import tools.dvc_cache_guard as guard
import tools.lock_contracts as lc
import tools.validate_dvc_receipt as vr

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
    monkeypatch.setattr(guard, "_external_config_layers", lambda env: [ext])
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


# ----------------------------- C1: regresiones R8R3 (B19/B22/B23/B26) -----------------------------


def _valid_receipt(plat):
    contract = {**lc.DVC_TOOL_DIRECT, "diskcache": lc.DVC_TOOL_DISKCACHE}
    z = "sha256:" + "0" * 64
    return {
        "schema_version": 2,
        "profile": "dvc-tool",
        "source_head_sha": "a",
        "checkout_sha": "b",
        "checkout_tree_sha": "c",
        "base_sha": "d",
        "git_dirty": False,
        "github_run_id": "1",
        "github_run_attempt": "1",
        "env_id": "e",
        "python": {},
        "platform": plat,
        "lock": vr._LOCKS_BY_PLATFORM[f"{plat['system']}-{plat['machine']}"],
        "lock_sha256": z,
        "lockset_sha256": z,
        "dvc_in_sha256": z,
        "expected": contract,
        "observed": contract,
        "version_ok": True,
        "pip_check": "ok",
        "cache_guard": "ok",
        "site_cache_dir": "repo/x",
        "site_cache_confined": True,
        "dag_returncode": 0,
        "dag_hash": z,
        "dvc_status_returncode": 0,
        "inventory_digest": z,
        "n_packages": 0,
        "sbom_component_count": 0,
        "sbom_sha256": z,
        "smoke_ok": True,
    }


def test_b19_forged_receipt_rejected(tmp_path, monkeypatch):
    # workspace con los ficheros de referencia; el recibo forjado con pip_check/contract malos cae
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    (tmp_path / "locks").mkdir()
    (tmp_path / "requirements").mkdir()
    (tmp_path / "locks/dvc-tool-linux-x86_64.txt").write_text("x")
    (tmp_path / lc.MANIFEST_REL).write_text("y")
    (tmp_path / "requirements/dvc.in").write_text("z")
    r = _valid_receipt({"system": "Linux", "machine": "x86_64"})
    r.update({"pip_check": "BROKEN", "expected": {"dvc": "9"}})
    (tmp_path / "r.json").write_text(json.dumps(r))
    (tmp_path / "s.json").write_text('{"bomFormat":"CycloneDX","components":[]}')
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    assert any("pip_check" in p for p in probs) and any("contrato" in p for p in probs)


def test_b19_receipt_outside_workspace_rejected(tmp_path):
    (tmp_path / "r.json").write_text("{}")
    (tmp_path / "s.json").write_text("{}")
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    assert any("fuera del workspace" in p for p in probs)


def test_b22_child_env_no_parent_mutation(tmp_path):
    root = _cache_root(tmp_path)
    before = os.environ.get("DVC_SITE_CACHE_DIR")
    guard.child_env(root)
    assert os.environ.get("DVC_SITE_CACHE_DIR") == before  # padre intacto


def test_b22_site_cache_tree_group_writable_blocks(tmp_path):
    root = _cache_root(tmp_path)
    guard.prepare(root)
    (root / ".dvc/site-cache/repo/token").mkdir(parents=True)
    os.chmod(root / ".dvc/site-cache/repo/token", 0o777)
    assert any("site-cache/repo/token" in x and "escribible" in x for x in guard.check(root))


def test_b23_legacy_gate_syntax_error_fail_closed(tmp_path):
    root = _git_repo(tmp_path, {"m.py": "def broken(:\n  pass\n"})
    with pytest.raises(SystemExit):
        legacy.current_counts(root)


def test_b23_legacy_gate_variable_argv(tmp_path):
    root = _git_repo(
        tmp_path, {"m.py": 'import subprocess\ncmd = ["ante_nf/bin/python", "x.py"]\nsubprocess.run(cmd)\n'}
    )
    assert legacy.current_counts(root).get("m.py") == 1


def test_b23_legacy_gate_from_import_alias(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'from subprocess import run as r\nr(["ante/bin/python", "x"])\n'})
    assert legacy.current_counts(root).get("m.py") == 1


def test_b26_advisory_rationale_mentions_site_cache():
    e = next(x for x in m.load_advisories(m.ADVISORIES) if x["id"] == "PYSEC-2026-2447")
    assert "site_cache_dir" in e["rationale"] and "0700" in e["rationale"]


# ----------------------------- C1: regresiones R8R4 (B27/B30/B31) -----------------------------


def test_b27_forged_provenance_and_empty_sbom_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    (tmp_path / "locks").mkdir()
    (tmp_path / "requirements").mkdir()
    (tmp_path / "locks/dvc-tool-linux-x86_64.txt").write_text("x")
    (tmp_path / lc.MANIFEST_REL).write_text("y")
    (tmp_path / "requirements/dvc.in").write_text("z")
    r = _valid_receipt({"system": "Linux", "machine": "x86_64"})
    g = "a" * 40
    r.update(
        {
            "source_head_sha": g,
            "checkout_sha": g,
            "checkout_tree_sha": g,
            "base_sha": g,
            "site_cache_dir": "../../outside",
        }
    )
    (tmp_path / "r.json").write_text(json.dumps(r))
    (tmp_path / "s.json").write_text('{"bomFormat":"CycloneDX","components":[]}')
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    # procedencia: tmp NO es un repo git ⇒ fail-closed ("no se pudo resolver el checkout")
    assert any("checkout" in p for p in probs)
    assert any("site_cache_dir" in p for p in probs)  # cache fuera
    assert any("SBOM" in p for p in probs)  # SBOM vacío != cierre esperado


def test_b27_site_cache_pattern_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    (tmp_path / "r.json").write_text("{}")  # inválido pero dentro del workspace
    probs = vr.validate(tmp_path / "r.json", tmp_path / "r.json")
    assert probs  # esquema exacto ya lo rechaza; no revienta


def test_b30_check_never_writes_os_environ(monkeypatch):
    writes = []
    real = os.environ

    class Tripwire(dict):
        def __setitem__(self, k, v):
            writes.append(k)
            super().__setitem__(k, v)

        def pop(self, k, *a):
            writes.append(("pop", k))
            return super().pop(k, *a)

    monkeypatch.setattr(os, "environ", Tripwire(real))
    guard.check(guard.ROOT, env={"DVC_SITE_CACHE_DIR": str(guard.site_cache_dir(guard.ROOT))})
    assert writes == []


def test_b31_legacy_from_os_alias(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'from os import system as s\ns("ante/bin/python x")\n'})
    assert legacy.current_counts(root).get("m.py") == 1


def test_b31_legacy_string_var_in_list(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'import subprocess\nPY = "ante_nf/bin/python"\nsubprocess.run([PY, "x"])\n'})
    assert legacy.current_counts(root).get("m.py") == 1


# ----------------------------- C1: regresiones R8R5 (B37/B34-SBOM) -----------------------------


def test_b37_legacy_constant_concat(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'import subprocess\nsubprocess.run(["ante" + "/bin/python"])\n'})
    assert legacy.current_counts(root).get("m.py") == 1


def test_b37_legacy_os_exec_family(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'import os\nos.execv("ante_nf/bin/python", ["x"])\n'})
    assert legacy.current_counts(root).get("m.py") == 1


def test_b37_legacy_executable_kwarg(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'import subprocess\nsubprocess.run(["x"], executable="ante/bin/python")\n'})
    assert legacy.current_counts(root).get("m.py") == 1


def _sbom(components):
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": components}


def _comp(name, ver):
    return {"type": "library", "name": name, "version": ver, "purl": f"pkg:pypi/{name.lower()}@{ver}"}


def test_b34_receipt_sbom_rejects_extra_component(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    (tmp_path / "locks").mkdir()
    (tmp_path / "requirements").mkdir()
    lockp = tmp_path / "locks/dvc-tool-linux-x86_64.txt"
    # lock con los pins reales del repo (para que expected_inventory case)
    import shutil

    shutil.copy(lc.ROOT / "locks/dvc-tool-linux-x86_64.txt", lockp)
    (tmp_path / lc.MANIFEST_REL).write_text("y")
    (tmp_path / "requirements/dvc.in").write_text("z")
    # SBOM = cierre esperado + un componente EXTRA
    import tools.python_env as pe

    exp = pe.expected_inventory("dvc-tool")
    comps = [_comp(n, v) for n, v in exp.items()] + [_comp("evil-extra", "9.9")]
    r = _valid_receipt({"system": "Linux", "machine": "x86_64"})
    (tmp_path / "s.json").write_text(json.dumps(_sbom(comps)))
    r.update({"sbom_component_count": len(comps), "n_packages": len(comps)})
    (tmp_path / "r.json").write_text(json.dumps(r))
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    assert any("SBOM" in p and ("EXTRA" in p or "evil" in p) for p in probs)


# ----------------------------- C1: regresiones R8R6 (B44/B45 + Node 20) -----------------------------


def test_b44_legacy_two_var_propagation(tmp_path):
    # PY(str ante) -> cmd(list con PY) -> subprocess.run(cmd): propagación de 2 variables
    root = _git_repo(
        tmp_path,
        {"m.py": 'import subprocess\nPY = "ante/bin/python"\ncmd = [PY, "x.py"]\nsubprocess.run(cmd)\n'},
    )
    assert legacy.current_counts(root).get("m.py") == 1


def test_b44_legacy_os_posix_spawn(tmp_path):
    root = _git_repo(tmp_path, {"m.py": 'import os\nos.posix_spawn("ante_nf/bin/python", ["x"], {})\n'})
    assert legacy.current_counts(root).get("m.py") == 1


def test_b44_legacy_args_kwarg(tmp_path):
    # el argv pasado por el kwarg `args=` (no posicional) también se detecta
    root = _git_repo(tmp_path, {"m.py": 'import subprocess\nsubprocess.run(args=["ante/bin/python", "x"])\n'})
    assert legacy.current_counts(root).get("m.py") == 1


@pytest.mark.parametrize(
    "field,val,frag",
    [
        ("schema_version", 2.0, "schema_version no es int"),
        ("dag_returncode", 0.0, "dag_returncode no es int"),
        ("dvc_status_returncode", 0.0, "dvc_status_returncode no es int"),
        ("n_packages", True, "n_packages no es int"),
        ("sbom_component_count", 1.0, "sbom_component_count no es int"),
    ],
)
def test_b45_receipt_type_identity(tmp_path, monkeypatch, field, val, frag):
    # un recibo con un campo numérico como float/bool (JSON no distingue 2 de 2.0) se rechaza por identidad
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    (tmp_path / "locks").mkdir()
    (tmp_path / "requirements").mkdir()
    (tmp_path / "locks/dvc-tool-linux-x86_64.txt").write_text("x")
    (tmp_path / lc.MANIFEST_REL).write_text("y")
    (tmp_path / "requirements/dvc.in").write_text("z")
    r = _valid_receipt({"system": "Linux", "machine": "x86_64"})
    r[field] = val
    (tmp_path / "r.json").write_text(json.dumps(r))
    (tmp_path / "s.json").write_text('{"bomFormat":"CycloneDX","components":[]}')
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    assert any(frag in p for p in probs)


def test_action_pins_repo_clean():
    import tools.check_action_pins as pins

    assert pins.check() == []  # el repo real: todo por SHA, ninguna Node 20


def test_action_pins_catches_node20(tmp_path):
    import tools.check_action_pins as pins

    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    sha = next(iter(pins._NODE20_SHAS))
    (wf / "x.yml").write_text(f"jobs:\n  a:\n    steps:\n      - uses: actions/upload-artifact@{sha}\n")
    probs = pins.check(tmp_path)
    assert any("Node 20" in p for p in probs)


def test_action_pins_catches_floating_tag(tmp_path):
    import tools.check_action_pins as pins

    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "x.yml").write_text("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
    probs = pins.check(tmp_path)
    assert any("SHA" in p for p in probs)


def test_no_node20_sha_anywhere_in_github():
    import tools.check_action_pins as pins

    gh = lc.ROOT / ".github"
    hits = [
        f"{p}:{sha}"
        for p in gh.rglob("*")
        if p.is_file()
        for sha in pins._NODE20_SHAS
        if sha in p.read_text(errors="ignore")
    ]
    assert hits == [], f"SHA Node 20 presente en .github: {hits}"


# ----------------------------- C1: regresiones R8R6R (B49 registro Actions / B50 esquema recibo) --------


def _wf(tmp_path, name, body):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(body)
    return tmp_path


def test_b49_unknown_action_valid_sha(tmp_path):
    import tools.check_action_pins as pins

    sha = "a" * 40  # SHA de 40 hex bien formado pero de una acción NO autorizada
    root = _wf(tmp_path, "x.yml", f"jobs:\n  a:\n    steps:\n      - uses: evil/backdoor@{sha}\n")
    probs = pins.check(root)
    assert any("NO autorizada" in p for p in probs)


def test_b49_false_version_comment(tmp_path):
    import tools.check_action_pins as pins

    reg = pins.load_registry()["actions/checkout"]
    root = _wf(tmp_path, "x.yml", f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{reg['sha']}  # v99\n")
    probs = pins.check(root)
    assert any("comentario" in p for p in probs)


def test_b49_node20_sha_in_yaml_extension(tmp_path):
    import tools.check_action_pins as pins

    sha = next(iter(pins._NODE20_SHAS))  # en un fichero .yaml (no .yml) que el gate viejo no escaneaba
    root = _wf(tmp_path, "x.yaml", f"jobs:\n  a:\n    steps:\n      - uses: actions/upload-artifact@{sha}\n")
    probs = pins.check(root)
    assert any("Node 20" in p for p in probs)


def test_b49_floating_tag_rejected(tmp_path):
    import tools.check_action_pins as pins

    root = _wf(tmp_path, "x.yml", "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v5\n")
    probs = pins.check(root)
    assert any("40 hex" in p for p in probs)


def _receipt_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    (tmp_path / "locks").mkdir()
    (tmp_path / "requirements").mkdir()
    (tmp_path / "locks/dvc-tool-linux-x86_64.txt").write_text("x")
    (tmp_path / lc.MANIFEST_REL).write_text("y")
    (tmp_path / "requirements/dvc.in").write_text("z")
    (tmp_path / "s.json").write_text('{"bomFormat":"CycloneDX","components":[]}')


def _good_python():
    return {"implementation": "cpython", "version": "3.14.0", "cache_tag": "cpython-314", "abi": "cpython-314-darwin"}


def _good_platform():
    return {"system": "Linux", "machine": "x86_64", "libc_or_macos": "glibc-2.39"}


@pytest.mark.parametrize(
    "mutate,frag",
    [
        (lambda r: r.__setitem__("python", {}), "python con claves"),
        (lambda r: r.__setitem__("platform", {**_good_platform(), "evil": 1}), "platform con claves"),
        (lambda r: r.__setitem__("github_run_id", 5), "github_run_id no es string decimal"),
        (lambda r: r.__setitem__("github_run_attempt", "0"), "github_run_attempt no es string decimal >= 1"),
        (lambda r: r.__setitem__("env_id", "deadbeef"), "env_id no es 64 hex"),
    ],
)
def test_b50_receipt_schema_exact(tmp_path, monkeypatch, mutate, frag):
    # recibo por lo demás bien formado (python/platform/env_id válidos) con UNA mutación de esquema
    _receipt_workspace(tmp_path, monkeypatch)
    r = _valid_receipt(_good_platform())
    r.update({"python": _good_python(), "env_id": "e" * 64, "github_run_id": "1", "github_run_attempt": "1"})
    mutate(r)
    (tmp_path / "r.json").write_text(json.dumps(r))
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    assert any(frag in p for p in probs)


# ----------------------------- C1: regresiones R8R6R2 (B53 registro / B54 recibo) -----------------------------

import tools.check_action_pins as pins  # noqa: E402


def _reg(tmp_path, text):
    p = tmp_path / "reg.json"
    p.write_text(text)
    return p


def test_b53_registry_rejects_bool_schema_version(tmp_path):
    p = _reg(tmp_path, '{"schema_version": true, "note": "x", "actions": {}}')
    with pytest.raises(SystemExit):
        pins.load_registry(p)


def test_b53_registry_rejects_duplicate_keys(tmp_path):
    p = _reg(tmp_path, '{"schema_version": 1, "schema_version": 1, "note": "x", "actions": {}}')
    with pytest.raises(SystemExit):
        pins.load_registry(p)


def test_b53_registry_rejects_extra_top_level_key(tmp_path):
    p = _reg(tmp_path, '{"schema_version": 1, "note": "x", "actions": {}, "evil": 1}')
    with pytest.raises(SystemExit):
        pins.load_registry(p)


# B287 — gramática CERRADA del registro de Actions. RED_BASE_SHA = b781d68: load_registry no validaba `note`
# (aceptaba bool/null/whitespace), aceptaba versión whitespace (isinstance+truthy) y no validaba el NOMBRE de la
# acción (aceptaba "", whitespace, @, .., slash). Estas pruebas corren load_registry (existe en ambos SHAs).
def _one_action_reg(**over):
    action = {"sha": "0" * 40, "version": "v5", "runtime": "node24"}
    action.update(over.pop("action", {}))
    doc = {"schema_version": 1, "note": "x", "actions": {over.pop("name", "actions/checkout"): action}}
    doc.update(over)
    return json.dumps(doc)


def test_b287_note_must_be_nonempty_bounded_string(tmp_path):
    for bad in (True, None, [], "   ", "", "x" * 3000):
        with pytest.raises(SystemExit):
            pins.load_registry(_reg(tmp_path, _one_action_reg(note=bad)))


def test_b287_version_grammar_rejects(tmp_path):
    for bad in ("   ", "5", "v", "v5.", "va", "v5-beta", "latest", "v5.0.0.0"):
        with pytest.raises(SystemExit):
            pins.load_registry(_reg(tmp_path, _one_action_reg(action={"version": bad})))
    for good in ("v5", "v5.0", "v5.0.3"):  # gramática válida no debe fallar
        pins.load_registry(_reg(tmp_path, _one_action_reg(action={"version": good})))


def test_b287_action_name_grammar_rejects(tmp_path):
    for bad in ("", "   ", "checkout", "actions/", "/actions/checkout", "actions//checkout", "actions/../x", "actions/check out", "actions/check@out"):  # fmt: skip
        with pytest.raises(SystemExit):
            pins.load_registry(_reg(tmp_path, _one_action_reg(name=bad)))
    pins.load_registry(_reg(tmp_path, _one_action_reg(name="owner/repo/sub")))  # owner/repo/path válido


def test_b287_runtime_must_be_node24(tmp_path):  # regresión (b781d68 ya rechazaba runtime != node24)
    for bad in (True, "node20", "", None):
        with pytest.raises(SystemExit):
            pins.load_registry(_reg(tmp_path, _one_action_reg(action={"runtime": bad})))


def test_b53_check_flags_orphan_registered_action(tmp_path):
    # un root cuyos workflows usan SOLO checkout deja las otras 5 acciones registradas como huérfanas
    sha = pins.load_registry()["actions/checkout"]["sha"]
    root = _wf(tmp_path, "x.yml", f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{sha}  # v5\n")
    probs = pins.check(root)
    assert any("huérfano" in p for p in probs)


def test_b53_check_flags_unparseable_uses(tmp_path):
    root = _wf(tmp_path, "x.yml", "jobs:\n  a:\n    steps:\n      - uses: docker://evil/image:latest\n")
    probs = pins.check(root)
    assert any("no parseable" in p for p in probs)


def test_b53_verify_remote_catches_bad_sha(tmp_path, monkeypatch):
    # sin red real: _gh mockeado devuelve un SHA de tag que NO coincide con el registrado
    p = _reg(
        tmp_path,
        '{"schema_version": 1, "note": "x", "actions": {"actions/checkout": '
        '{"sha": "' + ("a" * 40) + '", "version": "v5", "runtime": "node24"}}}',
    )

    def fake_gh(*args):
        q = args[0]
        if "commits/" in q:
            return '"' + "a" * 40 + '"'
        if "git/ref/tags/" in q:
            return "b" * 40  # el tag apunta a OTRO sha
        if "contents/action.yml" in q:
            import base64

            return base64.b64encode(b"runs:\n  using: node24\n").decode()
        return None

    monkeypatch.setattr(pins, "_gh", fake_gh)
    probs = pins.verify_remote(p)
    assert any("apunta a" in x for x in probs)


def test_b54_receipt_rejects_python_version_suffix(tmp_path, monkeypatch):
    _receipt_workspace(tmp_path, monkeypatch)
    r = _valid_receipt(_good_platform())
    bad_py = {**_good_python(), "version": "3.14.6evil"}  # sufijo basura tras la versión
    r.update({"python": bad_py, "env_id": "e" * 64, "github_run_id": "1", "github_run_attempt": "1"})
    (tmp_path / "r.json").write_text(json.dumps(r))
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    assert any("python.version no casa" in p for p in probs)


def test_b54_receipt_python_identity_matches_descriptor(tmp_path, monkeypatch):
    # recibo de ESTA plataforma (plat==actual) con python cache_tag falso -> debe diferir del descriptor real
    import tools.python_env as pe

    _receipt_workspace(tmp_path, monkeypatch)
    (tmp_path / ("locks/" + pe.lock_rel_for("dvc-tool").split("/")[-1])).write_text("x")
    desc = pe.descriptor("dvc-tool")
    plat = desc["platform"]
    r = _valid_receipt(plat)  # plat == actual (Darwin-arm64/Linux-x86_64)
    bad_py = {**desc["python"], "cache_tag": "cpython-999-fake"}  # no-vacío pero != descriptor
    r.update({"python": bad_py, "env_id": "e" * 64, "github_run_id": "1", "github_run_attempt": "1"})
    (tmp_path / "r.json").write_text(json.dumps(r))
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    assert any("python != descriptor" in p for p in probs)


def test_b54_receipt_platform_identity_matches_descriptor(tmp_path, monkeypatch):
    import tools.python_env as pe

    _receipt_workspace(tmp_path, monkeypatch)
    (tmp_path / ("locks/" + pe.lock_rel_for("dvc-tool").split("/")[-1])).write_text("x")
    desc = pe.descriptor("dvc-tool")
    bad_plat = {**desc["platform"], "libc_or_macos": "totally-wrong"}
    r = _valid_receipt(bad_plat)
    r.update({"python": desc["python"], "env_id": "e" * 64, "github_run_id": "1", "github_run_attempt": "1"})
    (tmp_path / "r.json").write_text(json.dumps(r))
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json")
    assert any("platform != descriptor" in p for p in probs)


# ----------------------------- C1: regresiones R8R6R3 (B56/B57/B59) -----------------------------


def test_b56_yaml_spaced_uses_is_detected(tmp_path):
    # `uses :` (espacio antes del `:`) es YAML válido pero el gate por texto no lo veía. El parseo
    # estructural DEBE ver la acción no autorizada.
    sha = "a" * 40
    root = _wf(tmp_path, "x.yml", f"jobs:\n  a:\n    steps:\n      - uses : evil/backdoor@{sha}\n")
    probs = pins.check(root)
    assert any("evil/backdoor" in p and "NO autorizada" in p for p in probs)


def test_b56_yaml_duplicate_uses_key_is_rejected(tmp_path):
    # dos claves `uses:` en un mismo step son ambiguas (una podría enmascarar a la otra). El loader YAML
    # con rechazo de claves duplicadas debe marcarlo como problema (fail-closed).
    reg = pins.load_registry()
    a = reg["actions/checkout"]
    b = reg["actions/setup-python"]
    body = (
        "jobs:\n  a:\n    steps:\n      - name: s\n"
        f"        uses: actions/checkout@{a['sha']}  # {a['version']}\n"
        f"        uses: actions/setup-python@{b['sha']}  # {b['version']}\n"
    )
    root = _wf(tmp_path, "x.yml", body)
    probs = pins.check(root)
    assert any("duplicad" in p.lower() for p in probs)


def test_b57_runtime_must_come_from_runs_using(tmp_path, monkeypatch):
    # verify_remote debe derivar node24 de la estructura runs.using, no de una búsqueda textual. Un
    # action.yml Node 20 con "using: node24" en la descripción NO debe pasar.
    p = _reg(
        tmp_path,
        '{"schema_version": 1, "note": "x", "actions": {"actions/checkout": '
        '{"sha": "' + ("a" * 40) + '", "version": "v5", "runtime": "node24"}}}',
    )

    def fake_gh(*args):
        q = args[0]
        if "commits/" in q:
            return '"' + "a" * 40 + '"'
        if "git/ref/tags/" in q:
            return "a" * 40  # el tag apunta al sha registrado (esa parte OK)
        if "contents/action" in q:
            import base64

            manifest = 'name: x\ndescription: "using: node24"\nruns:\n  using: node20\n  main: index.js\n'
            return '"' + base64.b64encode(manifest.encode()).decode() + '"'
        return None

    monkeypatch.setattr(pins, "_gh", fake_gh)
    probs = pins.verify_remote(p)
    assert any("node24" in x for x in probs)


def test_b57_node24_in_comment_or_description_does_not_count(tmp_path, monkeypatch):
    # node24 solo dentro de un comentario del manifiesto no cuenta: runs.using es node20.
    p = _reg(
        tmp_path,
        '{"schema_version": 1, "note": "x", "actions": {"actions/checkout": '
        '{"sha": "' + ("b" * 40) + '", "version": "v5", "runtime": "node24"}}}',
    )

    def fake_gh(*args):
        q = args[0]
        if "commits/" in q:
            return '"' + "b" * 40 + '"'
        if "git/ref/tags/" in q:
            return "b" * 40
        if "contents/action" in q:
            import base64

            manifest = "runs:\n  using: node20  # using: node24 (lie)\n  main: index.js\n"
            return '"' + base64.b64encode(manifest.encode()).decode() + '"'
        return None

    monkeypatch.setattr(pins, "_gh", fake_gh)
    probs = pins.verify_remote(p)
    assert any("node24" in x for x in probs)


def test_b59_receipt_cannot_choose_its_expected_platform(tmp_path, monkeypatch):
    # Un recibo que declara la plataforma CONTRARIA a la esperada (por la matriz) debe rechazarse; no puede
    # elegir su propia plataforma para saltarse la comparación de identidad ligada al runner.
    import tools.python_env as pe

    _receipt_workspace(tmp_path, monkeypatch)
    (tmp_path / ("locks/" + pe.lock_rel_for("dvc-tool").split("/")[-1])).write_text("x")
    cur = pe.platform_key()
    opposite = "Linux-x86_64" if cur == "Darwin-arm64" else "Darwin-arm64"
    sysd, machd = opposite.split("-")
    r = _valid_receipt({"system": sysd, "machine": machd, "libc_or_macos": "fake"})
    r.update({"python": _good_python(), "env_id": "e" * 64, "github_run_id": "1", "github_run_attempt": "1"})
    (tmp_path / "r.json").write_text(json.dumps(r))
    probs = vr.validate(tmp_path / "r.json", tmp_path / "s.json", expected_platform=cur)
    assert any("esperada" in p or "runner" in p for p in probs)


def test_b59_cross_requires_linux_and_macos_exactly_once():
    # el conjunto de plataformas de un par --cross debe ser EXACTAMENTE {Linux-x86_64, Darwin-arm64}
    lnx = {"system": "Linux", "machine": "x86_64", "libc_or_macos": "glibc-2.39"}
    mac = {"system": "Darwin", "machine": "arm64", "libc_or_macos": "macos-15"}
    assert vr._cross_set_problems({"platform": lnx}, {"platform": mac}) == []


def test_b59_two_linux_receipts_cannot_pass_as_cross_platform():
    lnx = {"system": "Linux", "machine": "x86_64", "libc_or_macos": "glibc-2.39"}
    probs = vr._cross_set_problems({"platform": lnx}, {"platform": dict(lnx)})
    assert probs and any("plataforma" in p for p in probs)


# ----------------------------- C1: regresiones R8R6R4 (B63) -----------------------------


def _all_actions_correct_lines():
    """Una línea `uses:` por cada acción registrada con su comentario correcto (para cerrar la biyección)."""
    reg = pins.load_registry()
    return [f"      - uses: {name}@{e['sha']}  # {e['version']}" for name, e in reg.items()]


def test_b63_wrong_first_comment_cannot_be_masked_by_later_use(tmp_path):
    # dos ocurrencias del MISMO action@sha: la 1ª con comentario FALSO, la 2ª correcta. El comentario por
    # ocurrencia (por línea) debe marcar la 1ª; el dict global `action@sha` last-wins la enmascaraba.
    reg = pins.load_registry()
    sha = reg["actions/checkout"]["sha"]
    body = "jobs:\n  a:\n    steps:\n"
    body += f"      - uses: actions/checkout@{sha}  # vFAKE\n"  # 1ª ocurrencia, comentario FALSO
    body += "\n".join(_all_actions_correct_lines()) + "\n"  # 2ª (checkout # v5) + resto correctas
    root = _wf(tmp_path, "x.yml", body)
    probs = pins.check(root)
    comment_probs = [p for p in probs if "comentario" in p]
    assert len(comment_probs) == 1, f"esperaba 1 comentario falso marcado, hubo {comment_probs}"


def test_b63_each_action_occurrence_checks_its_own_comment(tmp_path):
    # 1ª ocurrencia CORRECTA, 2ª FALSA: solo la 2ª debe marcarse (el dict global last-wins marcaba AMBAS).
    reg = pins.load_registry()
    sha = reg["actions/checkout"]["sha"]
    ver = reg["actions/checkout"]["version"]
    body = "jobs:\n  a:\n    steps:\n"
    body += f"      - uses: actions/checkout@{sha}  # {ver}\n"  # correcta
    body += f"      - uses: actions/checkout@{sha}  # vFAKE\n"  # FALSA
    # resto de acciones registradas una vez (biyección)
    body += "\n".join(line for line in _all_actions_correct_lines() if "actions/checkout@" not in line) + "\n"
    root = _wf(tmp_path, "x.yml", body)
    probs = pins.check(root)
    comment_probs = [p for p in probs if "comentario" in p]
    assert len(comment_probs) == 1, f"esperaba solo la 2ª (falsa) marcada, hubo {comment_probs}"


# ----------------------------- R9-a: regresiones B68 (desglose trinquete) / B70 (cobertura fail-closed) -----------------------------


def test_b68_ratchet_reports_executable_vs_documentary(tmp_path):
    # el trinquete debe SEPARAR refs ejecutables de documentales (comentarios) por fichero
    sh = "PY=ante/bin/python\n# comentario: no usar ante_nf/bin/python nunca\n$PY -m pipeline.x\n"
    root = _git_repo(tmp_path, {"run.sh": sh})
    bd = legacy.current_breakdown(root)
    assert bd["run.sh"]["executable"] == 1, bd  # solo la asignación PY=ante/bin/python
    assert bd["run.sh"]["documentary"] == 1, bd  # el ref en el comentario


def test_b70_ci_coverage_artifact_fails_closed():
    ci = (lc.ROOT / ".github" / "workflows" / "ci.yml").read_text()
    # el bloque de subida de coverage-by-layer no puede terminar verde sin conservar el recibo
    assert "coverage-by-layer" in ci
    block = ci.split("coverage-by-layer", 1)[1].split("retention-days", 1)[0]
    assert "if-no-files-found: error" in block, "coverage-by-layer sigue en if-no-files-found: ignore (B70)"


# ----------------------- R9.2R3 · B84: registro machine-readable de warnings upstream -----------------------


def _warn_doc(**overrides):
    import tools.check_action_pins as pins

    reg = pins.load_registry()
    action = "actions/download-artifact"
    w = {
        "id": "DEP0005-download-artifact",
        "action": action,
        "sha": reg[action]["sha"],
        "detail": "warning upstream de prueba",
        "review": "2099-01-01",
    }
    w.update(overrides)
    return {"schema_version": 1, "note": "test", "warnings": [w]}


def _write_warn(tmp_path, doc):
    p = tmp_path / "upstream_warnings.json"
    p.write_text(json.dumps(doc))
    return p


def test_b84_real_upstream_warnings_file_valid():
    import tools.check_action_pins as pins

    # el archivo REAL del repo valida limpio contra el registro real y la fecha de hoy
    assert pins.validate_upstream_warnings(pins.load_registry()) == []


def test_b84_expired_review_fails(tmp_path):
    import tools.check_action_pins as pins

    p = _write_warn(tmp_path, _warn_doc(review="2020-01-01"))
    probs = pins.validate_upstream_warnings(pins.load_registry(), p)
    assert any("VENCIDA" in x for x in probs), probs


def test_b84_review_expires_at_boundary(tmp_path):
    import tools.check_action_pins as pins

    p = _write_warn(tmp_path, _warn_doc(review="2026-01-01"))
    ok = pins.validate_upstream_warnings(pins.load_registry(), p, today=dt.date(2026, 1, 1))
    assert ok == []  # el día exacto de review aún vale
    expired = pins.validate_upstream_warnings(pins.load_registry(), p, today=dt.date(2026, 1, 2))
    assert any("VENCIDA" in x for x in expired)


def test_b84_sha_mismatch_invalidates_acceptance(tmp_path):
    import tools.check_action_pins as pins

    p = _write_warn(tmp_path, _warn_doc(sha="f" * 40))  # la acción bumpeó → la aceptación caduca
    probs = pins.validate_upstream_warnings(pins.load_registry(), p)
    assert any("re-evaluar" in x for x in probs), probs


def test_b84_unknown_action_fails(tmp_path):
    import tools.check_action_pins as pins

    p = _write_warn(tmp_path, _warn_doc(action="evil/unregistered"))
    probs = pins.validate_upstream_warnings(pins.load_registry(), p)
    assert any("NO está en el registro" in x for x in probs), probs


def test_b84_bad_schema_fails(tmp_path):
    import tools.check_action_pins as pins

    doc = _warn_doc()
    doc["warnings"][0]["extra"] = 1  # clave desconocida
    p = _write_warn(tmp_path, doc)
    probs = pins.validate_upstream_warnings(pins.load_registry(), p)
    assert probs, "esquema con clave extra aceptado"


def test_b84_missing_file_means_zero_accepted(tmp_path):
    import tools.check_action_pins as pins

    assert pins.validate_upstream_warnings(pins.load_registry(), tmp_path / "nope.json") == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
