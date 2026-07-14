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


# El contrato deep (versiones/torch/hashes/índices) vive ahora en tools/lock_contracts.py y se
# prueba en tests/test_lock_contracts.py. Aquí solo los FALSOS VERDES del auditor (P0R.4R).

# --- run_pip_audit: separa auditados de OMITIDOS (skip_reason) y valida cada dependency ----------


def _fake_proc(stdout, returncode=0, stderr=""):
    class _P:
        pass

    p = _P()
    p.stdout, p.returncode, p.stderr = stdout, returncode, stderr
    return p


def test_run_pip_audit_separates_skipped(tmp_path, monkeypatch):
    lock = tmp_path / "l.txt"
    lock.write_text("torch==2.12.1+cpu\nnumpy==2.4.6\n")
    payload = {
        "dependencies": [
            {"name": "torch", "skip_reason": "Dependency not found on PyPI: torch (2.12.1+cpu)"},
            {"name": "numpy", "version": "2.4.6", "vulns": []},
        ]
    }
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _fake_proc(m.json.dumps(payload)))
    res = m.run_pip_audit(lock)
    assert res["skipped"] == {"torch": "Dependency not found on PyPI: torch (2.12.1+cpu)"}
    assert res["audited"] == {"numpy": "2.4.6"} and res["findings"] == []


def test_run_pip_audit_dep_without_version_or_skip_blocks(tmp_path, monkeypatch):
    lock = tmp_path / "l.txt"
    lock.write_text("numpy==2.4.6\n")
    payload = {"dependencies": [{"name": "numpy", "vulns": []}]}  # ni version ni skip_reason
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _fake_proc(m.json.dumps(payload)))
    with pytest.raises(ValueError, match="sin versión ni skip_reason"):
        m.run_pip_audit(lock)


def test_run_pip_audit_duplicate_dependency_blocks(tmp_path, monkeypatch):
    lock = tmp_path / "l.txt"
    lock.write_text("numpy==2.4.6\n")
    payload = {
        "dependencies": [
            {"name": "numpy", "version": "2.4.6", "vulns": []},
            {"name": "numpy", "version": "2.4.6", "vulns": []},
        ]
    }
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _fake_proc(m.json.dumps(payload)))
    with pytest.raises(ValueError, match="DUPLICADO"):
        m.run_pip_audit(lock)


def _audit_one_lock(monkeypatch, *, lock="locks/dev.txt", pins, run_result, syn_result=None, entries=None):
    """Corre m.audit() sobre UN lock simulado, saltando el contrato/advisories reales.
    lock por defecto 'dev.txt' (sin consulta local); usa el CPU deep para probar torch +cpu."""
    monkeypatch.setattr(m.lc, "validate_all", lambda *a, **k: [])
    monkeypatch.setattr(m, "LOCKS", [(lock, "deep" if "deep" in lock else "dev")])
    monkeypatch.setattr(m, "read_pins", lambda p: pins)
    monkeypatch.setattr(m, "run_pip_audit", lambda p: run_result)
    monkeypatch.setattr(
        m, "_run_pip_audit_req", lambda req: syn_result or {"findings": [], "audited": {}, "skipped": {}}
    )
    monkeypatch.setattr(m, "load_advisories", lambda p: entries or [])
    monkeypatch.setattr(m, "validate_advisory_schema", lambda e: [])
    return m.audit(TODAY)[0]


CPU = "locks/deep-linux-x86_64-cpu.txt"


def test_audit_blocks_unauthorized_skip(monkeypatch):
    probs = _audit_one_lock(
        monkeypatch,
        pins={"evil": "1.0.0"},
        run_result={"findings": [], "audited": {}, "skipped": {"evil": "not on PyPI"}},
    )
    assert any("OMITIDO" in p and "sin autorización" in p for p in probs)


def test_audit_blocks_omitted_package(monkeypatch):
    probs = _audit_one_lock(
        monkeypatch, pins={"a": "1.0", "b": "2.0"}, run_result={"findings": [], "audited": {"a": "1.0"}, "skipped": {}}
    )
    assert any("pip-audit cubrió != lock" in p for p in probs)


def test_audit_public_query_runs_even_when_torch_audited(monkeypatch):
    # B1: torch AUDITADO por pip-audit (no omitido) — la consulta pública corre IGUAL. Se prueba con un
    # finding sintético que SOLO aflora si la consulta se ejecutó: aparece como advisory NUEVO.
    probs = _audit_one_lock(
        monkeypatch,
        lock=CPU,
        pins={"torch": "2.12.1+cpu"},
        run_result={"findings": [], "audited": {"torch": "2.12.1+cpu"}, "skipped": {}},
        syn_result={
            "findings": [{"package": "torch", "version": "2.12.1", "id": "CVE-9999-2", "aliases": []}],
            "audited": {"torch": "2.12.1"},
            "skipped": {},
        },
    )
    assert any("NUEVO" in p for p in probs)  # el finding sintético afloró -> la consulta corrió sin skip


def test_audit_public_query_wrong_version_blocks(monkeypatch):
    probs = _audit_one_lock(
        monkeypatch,
        lock=CPU,
        pins={"torch": "2.12.1+cpu"},
        run_result={"findings": [], "audited": {}, "skipped": {"torch": "not on PyPI"}},
        syn_result={"findings": [], "audited": {"torch": "2.12.0"}, "skipped": {}},
    )
    assert any("consulta pública" in p for p in probs)


def test_audit_public_query_extra_package_blocks(monkeypatch):
    probs = _audit_one_lock(
        monkeypatch,
        lock=CPU,
        pins={"torch": "2.12.1+cpu"},
        run_result={"findings": [], "audited": {}, "skipped": {"torch": "not on PyPI"}},
        syn_result={"findings": [], "audited": {"torch": "2.12.1", "evil": "1.0"}, "skipped": {}},
    )
    assert any("consulta pública" in p for p in probs)


def test_audit_public_query_still_skipped_blocks(monkeypatch):
    probs = _audit_one_lock(
        monkeypatch,
        lock=CPU,
        pins={"torch": "2.12.1+cpu"},
        run_result={"findings": [], "audited": {}, "skipped": {"torch": "not on PyPI"}},
        syn_result={"findings": [], "audited": {}, "skipped": {"torch": "still not found"}},
    )
    assert any("consulta pública" in p for p in probs)


def test_audit_local_audited_version_mismatch_blocks(monkeypatch):
    # pip-audit auditó torch en una versión local DISTINTA del pin -> bloquea
    probs = _audit_one_lock(
        monkeypatch,
        lock=CPU,
        pins={"torch": "2.12.1+cpu"},
        run_result={"findings": [], "audited": {"torch": "2.12.0+cpu"}, "skipped": {}},
        syn_result={"findings": [], "audited": {"torch": "2.12.1"}, "skipped": {}},
    )
    assert any("!= pin local" in p for p in probs)


def test_audit_advisory_only_via_public_query_blocks(monkeypatch):
    # un advisory que SOLO aparece en la consulta pública (no en el lock) igual bloquea
    probs = _audit_one_lock(
        monkeypatch,
        lock=CPU,
        pins={"torch": "2.12.1+cpu"},
        run_result={"findings": [], "audited": {}, "skipped": {"torch": "not on PyPI"}},
        syn_result={
            "findings": [{"package": "torch", "version": "2.12.1", "id": "CVE-9999-1", "aliases": []}],
            "audited": {"torch": "2.12.1"},
            "skipped": {},
        },
    )
    assert any("NUEVO" in p for p in probs)


def test_audit_authorized_skip_with_clean_public_passes(monkeypatch):
    probs = _audit_one_lock(
        monkeypatch,
        lock=CPU,
        pins={"torch": "2.12.1+cpu"},
        run_result={"findings": [], "audited": {}, "skipped": {"torch": "not on PyPI"}},
        syn_result={"findings": [], "audited": {"torch": "2.12.1"}, "skipped": {}},
    )
    assert probs == []


def test_audit_blocks_on_contract(monkeypatch):
    # si el contrato estático/manifiesto falla (p. ej. lock mutado tras el manifiesto), audit() corta
    monkeypatch.setattr(m.lc, "validate_all", lambda *a, **k: ["manifiesto.locks[locks/dev.txt] sha256 != real"])
    probs, receipt = m.audit(TODAY)
    assert receipt == {} and any("contrato" in p for p in probs)
