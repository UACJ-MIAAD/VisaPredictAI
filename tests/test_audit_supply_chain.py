"""Ejecutor único de supply chain: biyección exacta observado↔permitido (P0R.3, ronda 10)."""

from __future__ import annotations

import datetime as dt

import pytest

import tools.audit_python_supply_chain as m

# entradas permitidas de ejemplo (2 avisos del perfil model)
ENTRIES = [
    {
        "id": "CVE-2025-3000",
        "aliases": [],
        "package": "torch",
        "versions": ["2.12.0"],
        "profiles": ["model"],
        "decision": "accept",
        "severity": "low",
        "scope": "offline",
        "owner": "Javier",
        "expires_at": "2026-08-12",
        "rationale": "fixture",
    },
    {
        "id": "PYSEC-2026-3043",
        "aliases": ["CVE-2026-31221"],
        "package": "pytorch-lightning",
        "versions": ["2.5.6"],
        "profiles": ["model"],
        "decision": "accept",
        "severity": "low",
        "scope": "offline",
        "owner": "Javier",
        "expires_at": "2026-08-12",
        "rationale": "fixture",
    },
]
TODAY = dt.date(2026, 7, 20)  # antes de expirar
_OBS = {
    "model": [
        {"package": "torch", "version": "2.12.0", "id": "CVE-2025-3000", "aliases": []},
        {"package": "pytorch-lightning", "version": "2.5.6", "id": "CVE-2026-31221", "aliases": ["PYSEC-2026-3043"]},
    ]
}


def test_schema_valid():
    assert m.validate_advisory_schema(ENTRIES) == []


def _reconcile(obs=None, profile="model", lock="locks/model-cpu.txt"):
    return m.reconcile_lock((obs or _OBS)[profile], ENTRIES, profile=profile, lock=lock, today=TODAY)


def test_reconcile_coherent_passes():
    assert _reconcile() == []


def test_reconcile_new_advisory_fails():
    obs = {"model": _OBS["model"] + [{"package": "numpy", "version": "2.5", "id": "CVE-9999-1", "aliases": []}]}
    assert any("NUEVO" in p for p in _reconcile(obs))


def test_reconcile_orphan_allowed_fails():
    # solo se observa uno de los dos permitidos -> el otro es huérfano
    obs = {"model": _OBS["model"][:1]}
    assert any("HUÉRFANA" in p for p in _reconcile(obs))


def test_reconcile_wrong_version_fails():
    obs = {"model": [{"package": "torch", "version": "2.99.0", "id": "CVE-2025-3000", "aliases": []}, _OBS["model"][1]]}
    assert any("versión" in p for p in _reconcile(obs))


def test_reconcile_wrong_profile_fails():
    # el mismo aviso observado en 'deep', pero solo está permitido para 'model'
    assert any(
        "NO permitido" in p
        for p in m.reconcile_lock(
            [_OBS["model"][0]], ENTRIES, profile="deep", lock="locks/deep-macos-arm64.txt", today=TODAY
        )
    )


def test_reconcile_expired_fails():
    assert any(
        "EXPIRADA" in p
        for p in m.reconcile_lock(
            _OBS["model"], ENTRIES, profile="model", lock="locks/model-cpu.txt", today=dt.date(2026, 9, 1)
        )
    )


def test_reconcile_runtime_must_be_empty():
    obs = [{"package": "torch", "version": "2.12.0", "id": "CVE-2025-3000", "aliases": []}]
    assert any(
        "[runtime:" in p
        for p in m.reconcile_lock(obs, ENTRIES, profile="runtime", lock="locks/runtime.txt", today=TODAY)
    )


def test_reconcile_is_per_lock_other_platform_cannot_mask_orphan():
    # macOS observa ambos; Linux vacío debe fallar por sí mismo.
    assert _reconcile() == []
    probs = m.reconcile_lock([], ENTRIES, profile="model", lock="locks/model-cpu-linux-x86_64.txt", today=TODAY)
    assert len([p for p in probs if "HUÉRFANA" in p]) == 2


def test_schema_alias_reuse_fails():
    bad = [ENTRIES[0], {**ENTRIES[1], "aliases": ["CVE-2025-3000"]}]  # alias == id de otra entrada
    assert any("reutilizado" in p for p in m.validate_advisory_schema(bad))


def test_schema_bad_date_fails():
    assert any("expires_at" in p for p in m.validate_advisory_schema([{**ENTRIES[0], "expires_at": "no-fecha"}]))


def test_schema_missing_field_fails():
    assert any("faltan campos" in p for p in m.validate_advisory_schema([{"id": "X"}]))


def test_load_rejects_dup_keys(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"advisories": [], "advisories": []}')
    with pytest.raises(ValueError, match="duplicada"):
        m.load_advisories(p)


def test_run_pip_audit_missing_lock(tmp_path):
    with pytest.raises(FileNotFoundError):
        m.run_pip_audit(tmp_path / "nope.txt")


def test_run_pip_audit_bad_json(tmp_path, monkeypatch):
    lock = tmp_path / "l.txt"
    lock.write_text("torch==2.12.0\n")

    class _P:
        stdout = "no es json"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(ValueError, match="ilegible"):
        m.run_pip_audit(lock)


def test_run_pip_audit_operational_failure_is_not_empty_success(tmp_path, monkeypatch):
    lock = tmp_path / "l.txt"
    lock.write_text("requests==2.33.0\n")

    class _P:
        stdout = '{"dependencies":[]}'
        stderr = "network failure"
        returncode = 2

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(ValueError, match="operacionalmente"):
        m.run_pip_audit(lock)


def test_run_pip_audit_exit_and_findings_must_agree(tmp_path, monkeypatch):
    lock = tmp_path / "l.txt"
    lock.write_text("requests==2.33.0\n")

    class _P:
        stdout = '{"dependencies":[]}'
        stderr = ""
        returncode = 1

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(ValueError, match="incoherente"):
        m.run_pip_audit(lock)


def test_real_repo_advisories_schema_valid():
    # el JSON REAL del repo debe validar su esquema
    assert m.validate_advisory_schema(m.load_advisories(m.ADVISORIES)) == []


# ----------------------------- P0R.4: perfil deep + versión local -----------------------------

CPU_LOCK = "locks/deep-linux-x86_64-cpu.txt"
MAC_LOCK = "locks/deep-macos-arm64.txt"


def test_read_pins_ignores_comments_options_hashes(tmp_path):
    lock = tmp_path / "l.txt"
    lock.write_text(
        "# header\n--index-url https://pypi.org/simple\n"
        "torch==2.12.1+cpu \\\n    --hash=sha256:" + "a" * 64 + "\n"
        "numpy==2.4.6 \\\n"
    )
    pins = m.read_pins(lock)
    assert pins == {"torch": "2.12.1+cpu", "numpy": "2.4.6"}


def test_local_version_declared_ok():
    assert m.check_local_version_policy(CPU_LOCK, {"torch": "2.12.1+cpu", "numpy": "2.4.6"}) == []


def test_local_version_undeclared_blocks():
    probs = m.check_local_version_policy(CPU_LOCK, {"torch": "2.12.1+cpu", "evil": "1.0+xyz"})
    assert any("NO declarada" in p and "evil" in p for p in probs)


def test_local_version_public_mismatch_blocks():
    probs = m.check_local_version_policy(CPU_LOCK, {"torch": "2.11.0+cpu"})
    assert any("normalización declarada" in p for p in probs)


def test_local_version_declared_but_absent_blocks():
    probs = m.check_local_version_policy(CPU_LOCK, {"numpy": "2.4.6"})
    assert any("ausente del lock" in p for p in probs)


def test_local_version_declared_but_not_local_blocks():
    probs = m.check_local_version_policy(CPU_LOCK, {"torch": "2.12.1"})  # sin +cpu
    assert any("no fija versión local" in p for p in probs)


def test_macos_deep_has_no_local_version_policy():
    # el lock macOS no tiene consultas declaradas y torch va sin sufijo local
    assert m.check_local_version_policy(MAC_LOCK, {"torch": "2.12.1", "numpy": "2.4.6"}) == []


def _write_fake_deep(root, mutate=None):
    """Escribe los 3 locks deep VÁLIDOS bajo root/locks; mutate(rel, pins, torch) -> (pins, torch, extra_text)."""
    (root / "locks").mkdir(parents=True, exist_ok=True)
    for rel in m.DEEP_LOCKS:
        pins = dict(m.DEEP_DIRECT)
        torch = m.DEEP_TORCH[rel]
        extra = "    --hash=sha256:" + "0" * 64 + "\n"
        if mutate:
            pins, torch, extra = mutate(rel, pins, torch, extra)
        lines = ["# lock deep"]
        for pkg, ver in pins.items():
            lines.append(f"{pkg}=={ver} \\")
        if torch is not None:
            lines.append(f"torch=={torch} \\")
        (root / rel).write_text("\n".join(lines) + "\n" + extra)


def test_deep_contract_real_locks_pass():
    # los locks REALES del repo deben cumplir el contrato deep
    assert m.validate_deep_lock_contract() == []


def test_deep_contract_wrong_direct_version_blocks(tmp_path, monkeypatch):
    def mutate(rel, pins, torch, extra):
        pins["pandas"] = "3.0.0"  # deep exige 2.3.3
        return pins, torch, extra

    _write_fake_deep(tmp_path, mutate)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    probs = m.validate_deep_lock_contract()
    assert any("pandas" in p and "3.0.0" in p for p in probs)


def test_deep_contract_wrong_torch_variant_blocks(tmp_path, monkeypatch):
    def mutate(rel, pins, torch, extra):
        return pins, "2.12.1", extra  # macOS OK, pero linux debería llevar +cpu/+cu126

    _write_fake_deep(tmp_path, mutate)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    probs = m.validate_deep_lock_contract()
    assert any("torch" in p and CPU_LOCK in p for p in probs)


def test_deep_contract_missing_hashes_blocks(tmp_path, monkeypatch):
    def mutate(rel, pins, torch, extra):
        return pins, torch, ""  # sin línea --hash

    _write_fake_deep(tmp_path, mutate)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    probs = m.validate_deep_lock_contract()
    assert any("sin hashes" in p for p in probs)


def test_normalized_query_only_for_matching_lock(monkeypatch):
    captured = {}

    def fake_audit(path):
        captured["content"] = path.read_text()
        return [{"package": "torch", "version": "2.12.1", "id": "CVE-X", "aliases": []}]

    monkeypatch.setattr(m, "run_pip_audit", fake_audit)
    # lock con consulta declarada -> consulta la versión PÚBLICA
    out = m.run_pip_audit_normalized(CPU_LOCK)
    assert out and captured["content"].strip() == "torch==2.12.1"
    # lock sin consulta declarada -> no consulta nada
    captured.clear()
    assert m.run_pip_audit_normalized("locks/runtime.txt") == []
