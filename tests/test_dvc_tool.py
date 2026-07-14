"""Adversarial del perfil dvc-tool (P0R.5 · D6): contrato de locks, aceptación acotada de
PYSEC-2026-2447 (diskcache) y guard de caché DVC. La herramienta DVC está AISLADA del producto.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

import tools.audit_python_supply_chain as m
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


# ----------------------------- guard de caché DVC -----------------------------


def _cache_root(tmp_path):
    root = tmp_path / "repo"
    (root / ".dvc/cache").mkdir(parents=True)
    (root / ".dvc/tmp").mkdir(parents=True)
    os.chmod(root / ".dvc/cache", 0o700)
    os.chmod(root / ".dvc/tmp", 0o700)
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


def test_cache_guard_config_override_blocks(tmp_path):
    root = _cache_root(tmp_path)
    (root / ".dvc/config").write_text("[cache]\n    dir = /var/shared/dvccache\n")
    assert any("override de caché" in x for x in guard.check(root))


def test_cache_guard_missing_dirs_ok(tmp_path):
    # sin .dvc/cache aún (se crea con umask 077) -> no es violación
    root = tmp_path / "repo"
    (root / ".dvc").mkdir(parents=True)
    assert guard.check(root) == []


# ----------------------------- gate: cero dvc fuera del lock (D10) -----------------------------


def _stray_root(tmp_path):
    root = tmp_path / "repo"
    (root / ".github/workflows").mkdir(parents=True)
    (root / "experiments").mkdir()
    (root / "Makefile").write_text("all:\n\techo ok\n")
    return root


def test_stray_real_repo_is_governed():
    assert stray.check(lc.ROOT) == []


def test_stray_loose_pip_install_blocks(tmp_path):
    root = _stray_root(tmp_path)
    (root / ".github/workflows/x.yml").write_text('run: |\n  pip install "dvc==3.67.1"\n')
    assert any("fuera del lock" in p for p in stray.check(root))


def test_stray_bare_dvc_invocation_blocks(tmp_path):
    root = _stray_root(tmp_path)
    (root / ".github/workflows/x.yml").write_text("run: |\n  dvc commit --force panel\n")
    assert any("sin el cache guard" in p for p in stray.check(root))


def test_stray_approved_install_and_guard_pass(tmp_path):
    root = _stray_root(tmp_path)
    (root / ".github/workflows/x.yml").write_text(
        "run: |\n"
        "  pip install --require-hashes -r locks/dvc-tool-linux-x86_64.txt --quiet\n"
        "  python -m tools.dvc_cache_guard --run dvc commit --force panel\n"
    )
    assert stray.check(root) == []


def test_stray_commented_and_dollar_dvc_pass(tmp_path):
    root = _stray_root(tmp_path)
    (root / ".github/workflows/x.yml").write_text('run: |\n  # pip install "dvc==3.67.1" legado\n  echo ok\n')
    (root / "experiments/s.sh").write_text("$DVC add models\n$(DVC) repro\ndvc.lock stale\n")
    assert stray.check(root) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
