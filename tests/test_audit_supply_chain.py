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
        "owner": "Javier",
        "expires_at": "2026-08-12",
    },
    {
        "id": "PYSEC-2026-3043",
        "aliases": ["CVE-2026-31221"],
        "package": "pytorch-lightning",
        "versions": ["2.5.6"],
        "profiles": ["model"],
        "decision": "accept",
        "owner": "Javier",
        "expires_at": "2026-08-12",
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


def test_reconcile_coherent_passes():
    assert m.reconcile(_OBS, ENTRIES, TODAY) == []


def test_reconcile_new_advisory_fails():
    obs = {"model": _OBS["model"] + [{"package": "numpy", "version": "2.5", "id": "CVE-9999-1", "aliases": []}]}
    assert any("NUEVO" in p for p in m.reconcile(obs, ENTRIES, TODAY))


def test_reconcile_orphan_allowed_fails():
    # solo se observa uno de los dos permitidos -> el otro es huérfano
    obs = {"model": _OBS["model"][:1]}
    assert any("HUÉRFANA" in p for p in m.reconcile(obs, ENTRIES, TODAY))


def test_reconcile_wrong_version_fails():
    obs = {"model": [{"package": "torch", "version": "2.99.0", "id": "CVE-2025-3000", "aliases": []}, _OBS["model"][1]]}
    assert any("versión" in p for p in m.reconcile(obs, ENTRIES, TODAY))


def test_reconcile_wrong_profile_fails():
    # el mismo aviso observado en 'deep', pero solo está permitido para 'model'
    obs = {"deep": [_OBS["model"][0]], "model": _OBS["model"]}
    assert any("NO permitido para este perfil" in p for p in m.reconcile(obs, ENTRIES, TODAY))


def test_reconcile_expired_fails():
    assert any("EXPIRADA" in p for p in m.reconcile(_OBS, ENTRIES, dt.date(2026, 9, 1)))


def test_reconcile_runtime_must_be_empty():
    obs = {
        "runtime": [{"package": "torch", "version": "2.12.0", "id": "CVE-2025-3000", "aliases": []}],
        "model": _OBS["model"],
    }
    # torch aparece en runtime (sintético): el aviso existe pero NO está permitido para runtime
    assert any("[runtime]" in p for p in m.reconcile(obs, ENTRIES, TODAY))


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

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(ValueError, match="ilegible"):
        m.run_pip_audit(lock)


def test_real_repo_advisories_schema_valid():
    # el JSON REAL del repo debe validar su esquema
    assert m.validate_advisory_schema(m.load_advisories(m.ADVISORIES)) == []
