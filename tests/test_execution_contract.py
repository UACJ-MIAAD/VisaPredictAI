"""Contrato gobernado de comandos (P0R.5 · R9.1): validación estricta + interfaz run-command."""

from __future__ import annotations

import json
import pathlib
from contextlib import contextmanager

import pytest

import tools.execution_contract as ec
import tools.python_env as pe


def _write(tmp_path, doc):
    p = tmp_path / "ec.json"
    p.write_text(json.dumps(doc))
    return p


def _real():
    return json.loads((ec.ROOT / "environments" / "execution_contract.json").read_text())


# ----------------------------- validación -----------------------------


def test_contract_loads_and_validates():
    doc = ec.load_contract()
    assert doc["schema_version"] == 1 and doc["commands"]
    # cada script del contrato es gobernado; cada módulo resuelve
    for cid, c in doc["commands"].items():
        assert c["profile"] in pe.load_profiles()["profiles"], cid


def test_contract_rejects_unknown_profile(tmp_path):
    doc = _real()
    doc["commands"]["scrape_all"]["profile"] = "ghost"
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_contract_rejects_deep_without_variant(tmp_path):
    doc = _real()
    doc["commands"]["run_global_deep"]["variant"] = None  # deep EXIGE variante
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_contract_rejects_nonvariant_profile_with_variant(tmp_path):
    doc = _real()
    doc["commands"]["scrape_all"]["variant"] = "cpu"  # runtime no admite variante
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_contract_rejects_code_mode(tmp_path):
    doc = _real()
    doc["commands"]["scrape_all"]["mode"] = "code"  # -c/stdin no permitido en el contrato
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_contract_rejects_untracked_script(tmp_path):
    doc = _real()
    doc["commands"]["aggregate_seeds"]["target"] = "experiments/_nonexistent_r9.py"
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_contract_rejects_absolute_script(tmp_path):
    doc = _real()
    doc["commands"]["aggregate_seeds"]["target"] = "/tmp/evil.py"
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_contract_rejects_module_that_does_not_resolve(tmp_path):
    doc = _real()
    doc["commands"]["scrape_all"]["target"] = "pipeline.does_not_exist_r9"
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_contract_rejects_extra_command_key(tmp_path):
    doc = _real()
    doc["commands"]["scrape_all"]["evil"] = 1
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_contract_rejects_duplicate_keys(tmp_path):
    p = tmp_path / "ec.json"
    p.write_text('{"schema_version": 1, "schema_version": 1, "note": "x", "commands": {}}')
    with pytest.raises(SystemExit):
        ec.load_contract(p)


def test_contract_rejects_bad_command_id(tmp_path):
    doc = _real()
    doc["commands"]["Bad-Id"] = doc["commands"]["scrape_all"]
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


# ----------------------------- interfaz run-command -----------------------------


def test_run_command_unknown_id_rejected():
    with pytest.raises(SystemExit):
        ec.command("no_such_command_r9")


def _mock_launch(monkeypatch):
    captured: dict = {}

    @contextmanager
    def fake_open(profile, variant=None, profiles=None):
        captured["profile"], captured["variant"] = profile, variant
        yield pe._ValidEnv(-1, {}, "x", pe.ROOT, {})

    def fake_launch(env, spec, capture):
        import subprocess

        captured["spec"] = spec
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(pe, "build", lambda *a, **k: pe.ROOT)
    monkeypatch.setattr(pe, "open_or_build_valid_environment", fake_open)
    monkeypatch.setattr(pe, "_launch_fd_bound", fake_launch)
    monkeypatch.setattr(pe, "_governed_script", lambda name: name)
    return captured


def test_run_command_module_uses_contract_profile(monkeypatch):
    cap = _mock_launch(monkeypatch)
    pe.run_command("scrape_all", ["--flag"])
    assert cap["profile"] == "runtime" and cap["variant"] is None
    assert cap["spec"] == {"mode": "module", "name": "pipeline.scrape_all", "rest": ["--flag"]}


def test_run_command_deep_uses_variant(monkeypatch):
    cap = _mock_launch(monkeypatch)
    pe.run_command("run_global_deep", ["--seed", "1"])
    assert cap["profile"] == "deep" and cap["variant"] == "cpu"
    assert cap["spec"]["mode"] == "script" and cap["spec"]["name"] == "experiments/run_global_deep.py"


def test_run_command_script_goes_through_governed_script(monkeypatch):
    cap = _mock_launch(monkeypatch)  # incluye _governed_script mock (identidad)
    pe.run_command("aggregate_seeds", [])
    # modo script con el target GOBERNADO (pasó por _governed_script, aquí identidad)
    assert cap["spec"]["mode"] == "script"
    assert cap["spec"]["name"] == "experiments/aggregate_seeds.py"
    assert cap["profile"] == "model"


# ----------------------------- el sha del contrato entra en env_id -----------------------------


def test_contract_sha_in_descriptor_and_env_id(monkeypatch):
    gov = pe.descriptor("dvc-tool")["governance"]
    assert "execution_contract_sha256" in gov and gov["execution_contract_sha256"].startswith("sha256:")
    base = pe.env_id("dvc-tool")
    real = pe._sha256_path

    def fake(p):
        return ("sha256:" + "f" * 64) if p.name == "execution_contract.json" else real(p)

    monkeypatch.setattr(pe, "_sha256_path", fake)
    assert pe.env_id("dvc-tool") != base  # el contrato gobierna el env_id


# ----------------------------- R9.2R: B71 (módulo gobernado) -----------------------------


def test_b71_module_via_symlink_rejected(tmp_path):
    import tempfile

    outside = pathlib.Path(tempfile.mkdtemp()) / "evil_mod.py"
    outside.write_text("x = 1\n")
    link = ec.ROOT / "tools" / "_b71_evil_link.py"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(outside)
    try:
        with pytest.raises(SystemExit):
            ec._governed_module("tools._b71_evil_link")
    finally:
        link.unlink()


def test_b71_module_untracked_rejected():
    # un módulo cuyo fichero existe pero NO está versionado se rechaza
    p = ec.ROOT / "tools" / "_b71_untracked_mod.py"
    p.write_text("x = 1\n")
    try:
        with pytest.raises(SystemExit):
            ec._governed_module("tools._b71_untracked_mod")
    finally:
        p.unlink()


def test_b71_ambiguous_module_rejected(tmp_path, monkeypatch):
    # x.py Y x/__init__.py a la vez ⇒ resolución ambigua ⇒ rechazo
    monkeypatch.setattr(ec, "ROOT", tmp_path)
    (tmp_path / "pkg.py").write_text("x = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("x = 1\n")
    with pytest.raises(SystemExit):
        ec._governed_module("pkg")


def test_b71_nonexistent_module_rejected():
    with pytest.raises(SystemExit):
        ec._governed_module("tools.does_not_exist_b71")


def test_b71_governed_module_accepts_real():
    # un módulo real, versionado, regular, sin symlink → devuelve su ruta relativa
    assert ec._governed_module("tools.python_env") == "tools/python_env.py"


# ----------------------------- B78: installed_module (pytest/ruff/mypy) -----------------------------


def test_installed_module_present_in_real_contract():
    doc = ec.load_contract()
    inst = {cid: c for cid, c in doc["commands"].items() if c["mode"] == "installed_module"}
    assert {"test", "test_model", "lint", "typecheck"} <= set(inst)
    for cid, c in inst.items():
        assert c["target"] in ec._INSTALLED_MODULES, cid
        assert c["profile"] in ("dev", "model"), cid


def test_installed_module_rejects_non_allowlisted_target(tmp_path):
    doc = _real()
    doc["commands"]["lint"]["target"] = "os"  # módulo arbitrario del entorno, NO herramienta dev declarada
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_installed_module_rejects_wrong_profile(tmp_path):
    doc = _real()
    doc["commands"]["lint"]["profile"] = "runtime"  # installed_module EXIGE dev/model
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_installed_module_rejects_noncanonical_target(tmp_path):
    doc = _real()
    doc["commands"]["lint"]["target"] = "Not-A-Module"
    with pytest.raises(SystemExit):
        ec.load_contract(_write(tmp_path, doc))


def test_run_command_installed_module_dispatches(monkeypatch):
    cap = _mock_launch(monkeypatch)
    pe.run_command("test", ["-q", "-k", "smoke"])
    assert cap["profile"] == "dev" and cap["variant"] is None
    assert cap["spec"] == {"mode": "installed_module", "name": "pytest", "rest": ["-q", "-k", "smoke"]}


def test_run_command_installed_module_test_model_uses_model_profile(monkeypatch):
    cap = _mock_launch(monkeypatch)
    pe.run_command("test_model", ["tests/test_models.py"])
    assert cap["profile"] == "model"
    assert cap["spec"]["mode"] == "installed_module" and cap["spec"]["name"] == "pytest"


def test_installed_module_bootstrap_dispatches_like_module():
    # el bootstrap trata installed_module IGUAL que module (runpy.run_module) — un solo despacho.
    assert "installed_module" in pe._ALLOWED_MODES
    assert "mode in ('module', 'installed_module')" in pe._RUNTIME_BOOTSTRAP


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
