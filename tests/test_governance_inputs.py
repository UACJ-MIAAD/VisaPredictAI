"""B286-C REDs: gate POSITIVO de inputs de gobernanza (`tools/check_governance_inputs.py`) + migración de consumers.

Construye un repo git SINTÉTICO bajo `tmp_path` (dirs 0700/0755 → pasa B282) con un `governance_inputs.json` mínimo y sus
inputs, monkeypatchea `_ROOT` a su realpath, y ejercita cada RED de §8.2/§8.3: input nuevo no registrado, registro
huérfano/movido, consumer divergente, modo != 0644, JSON duplicado del registro, y la lectura gobernada (reverify) del
input. Complementa la biyección real (`test_real_registry_is_exact`)."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

import tools.check_governance_inputs as gi


def _git(d, *args):
    subprocess.run(["git", "-C", d, *args], check=True, capture_output=True)


def _entry(category="contract", consumers=None, max_bytes=262144):
    return {
        "category": category,
        "format": "json",
        "parser": "json",
        "consumers": consumers or [],
        "operations": ["read"],
        "max_bytes": max_bytes,
        "reason": "r",
        "local_mode": "0o644",
    }


def _repo(tmp_path, inputs, files, *, registry_raw=None):
    """Monta un repo git con `files` (rel→texto), un `governance_inputs.json` (de `inputs` o `registry_raw` crudo), git
    init+add, y apunta `gi._ROOT` a su realpath. Los .json de inputs deben existir en `files`."""
    d = os.path.realpath(str(tmp_path))
    subprocess.run(["git", "init", "-q", d], check=True, capture_output=True)
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    for rel, txt in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(txt)
        os.chmod(p, 0o644)
    reg = (
        registry_raw
        if registry_raw is not None
        else json.dumps({"schema_version": 1, "note": "x", "required_mode": "0644", "inputs": inputs})
    )
    with open(os.path.join(d, "security", "governance_inputs.json"), "w", encoding="utf-8") as fh:
        fh.write(reg)
    os.chmod(os.path.join(d, "security", "governance_inputs.json"), 0o644)
    _git(d, "add", "-A")
    return d


def _run(tmp_path, monkeypatch, inputs, files, *, registry_raw=None):
    d = _repo(tmp_path, inputs, files, registry_raw=registry_raw)
    monkeypatch.setattr(gi, "_ROOT", d)
    return gi.problems()


def test_real_registry_is_exact():
    assert gi.problems() == [], gi.problems()


def test_clean_synthetic_repo_passes(tmp_path, monkeypatch):
    files = {"security/a.json": "{}\n", "tools/reader.py": 'P = "security/a.json"\n'}
    probs = _run(tmp_path, monkeypatch, {"security/a.json": _entry(consumers=["tools/reader.py"])}, files)
    assert probs == [], probs


def test_new_unregistered_input_fails(tmp_path, monkeypatch):
    # security/b.json existe pero NO está en el registro
    files = {"security/a.json": "{}\n", "security/b.json": "{}\n", "tools/reader.py": 'P = "security/a.json"\n'}
    probs = _run(tmp_path, monkeypatch, {"security/a.json": _entry(consumers=["tools/reader.py"])}, files)
    assert any("NO REGISTRADO" in p and "security/b.json" in p for p in probs), probs


def test_orphan_registry_entry_fails(tmp_path, monkeypatch):
    # el registro declara security/ghost.json que no existe
    files = {"security/a.json": "{}\n", "tools/reader.py": 'P = "security/a.json"\n'}
    inputs = {
        "security/a.json": _entry(consumers=["tools/reader.py"]),
        "security/ghost.json": _entry(consumers=["tools/reader.py"]),
    }
    probs = _run(tmp_path, monkeypatch, inputs, files)
    assert any("OBSOLETO/MOVIDO" in p and "ghost" in p for p in probs), probs


def test_undeclared_consumer_fails(tmp_path, monkeypatch):
    # dos tools referencian a.json pero el registro sólo declara uno
    files = {
        "security/a.json": "{}\n",
        "tools/reader.py": 'P = "security/a.json"\n',
        "tools/sneaky.py": 'Q = "security/a.json"\n',
    }
    probs = _run(tmp_path, monkeypatch, {"security/a.json": _entry(consumers=["tools/reader.py"])}, files)
    assert any("sneaky" in p and "NO está en consumers" in p for p in probs), probs


def test_obsolete_consumer_fails(tmp_path, monkeypatch):
    # el registro declara un consumer que ya NO referencia el input
    files = {"security/a.json": "{}\n", "tools/reader.py": "X = 1\n"}
    probs = _run(tmp_path, monkeypatch, {"security/a.json": _entry(consumers=["tools/reader.py"])}, files)
    assert any("NO referencia el input" in p for p in probs), probs


def test_non_0644_input_fails(tmp_path, monkeypatch):
    files = {"security/a.json": "{}\n", "tools/reader.py": 'P = "security/a.json"\n'}
    d = _repo(tmp_path, {"security/a.json": _entry(consumers=["tools/reader.py"])}, files)
    os.chmod(os.path.join(d, "security", "a.json"), 0o600)  # modo != 0644
    monkeypatch.setattr(gi, "_ROOT", d)
    assert any("no legible por GovernanceSnapshot" in p for p in gi.problems()), gi.problems()


def test_oversize_input_fails(tmp_path, monkeypatch):
    files = {"security/a.json": "{}\n" + " " * 500, "tools/reader.py": 'P = "security/a.json"\n'}
    probs = _run(tmp_path, monkeypatch, {"security/a.json": _entry(consumers=["tools/reader.py"], max_bytes=8)}, files)
    assert any("no legible por GovernanceSnapshot" in p for p in probs), probs


def test_registry_duplicate_json_key_fails(tmp_path, monkeypatch):
    files = {"security/a.json": "{}\n", "tools/reader.py": 'P = "security/a.json"\n'}
    raw = '{"schema_version": 1, "note": "x", "note": "dup", "required_mode": "0644", "inputs": {}}'
    probs = _run(tmp_path, monkeypatch, {}, files, registry_raw=raw)
    assert any("duplicad" in p.lower() for p in probs), probs


def test_bad_required_mode_fails(tmp_path, monkeypatch):
    files = {"security/a.json": "{}\n", "tools/reader.py": 'P = "security/a.json"\n'}
    raw = json.dumps({"schema_version": 1, "note": "x", "required_mode": "0777", "inputs": {}})
    probs = _run(tmp_path, monkeypatch, {}, files, registry_raw=raw)
    assert any("required_mode" in p for p in probs), probs


# --------------------------------------------------------------------------- §8.2 consumer migrations


def test_p0r5_and_action_pins_and_consistency_are_registered_consumers():
    # las migraciones de §8.2 declaran estos tres como consumidores de GovernanceSnapshot (biyección B286-B lo exige)
    reg = json.loads(pathlib.Path("security/governance_snapshot_consumers.json").read_text(encoding="utf-8"))
    for c in ("tools/check_p0r5_governance.py", "tools/check_action_pins.py", "tools/check_consistency.py"):
        assert c in reg["consumers"], c
        assert "read" in reg["consumers"][c]["operations"], c


def test_p0r5_main_reads_workflow_through_snapshot():
    # regresión estructural: main() abre una GovernanceSnapshot y llama problems() con el texto gobernado + reverify
    src = pathlib.Path("tools/check_p0r5_governance.py").read_text(encoding="utf-8")
    assert "with GovernanceSnapshot(ROOT) as snap:" in src
    assert "snap.read(_WORKFLOW" in src and "snap.reverify()" in src


def test_action_pins_main_reads_inputs_through_snapshot():
    src = pathlib.Path("tools/check_action_pins.py").read_text(encoding="utf-8")
    assert "with GovernanceSnapshot(str(ROOT)) as snap:" in src
    assert 'snap.read(_reg_rel, category="contract")' in src and "snap.reverify()" in src


def test_consistency_reads_rules_through_snapshot():
    src = pathlib.Path("tools/check_consistency.py").read_text(encoding="utf-8")
    assert "_read_rules_text()" in src and "GovernanceSnapshot" in src
