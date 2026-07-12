"""Tests del gate de completitud+frescura de campaña (auditoría 12-jul-2026)."""

from __future__ import annotations

import json

import pytest

import tools.check_campaign_completeness as gate


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Repo sintético con la estructura mínima de artefactos de campaña."""
    (tmp_path / "reports" / "campaign").mkdir(parents=True)
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    (tmp_path / "reports" / "governance").mkdir(parents=True)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "MANIFEST", tmp_path / "reports" / "campaign" / "campaign_manifest.json")
    return tmp_path


def _seal(root, started: str) -> None:
    (root / "reports" / "campaign" / "campaign_manifest.json").write_text(
        json.dumps({"campaign_id": "t", "sha": "abc", "started_at": started})
    )


def _write_all(root) -> None:
    body = "col\n" + "\n".join(f"row{i}" for i in range(6))  # 6 filas útiles > pisos
    for name in (
        "campaign_pool_FAD_family",
        "campaign_pool_FAD_employment",
        "campaign_pool_DFF_family",
        "campaign_pool_DFF_employment",
    ):
        (root / "reports" / "campaign" / f"{name}.csv").write_text(body)
    for name in (
        "model_comparison_FAD21",
        "model_comparison_EB_FAD21",
        "model_comparison_DFF21",
        "model_comparison_EB_DFF21",
        "finalist_forecasts_FAD",
        "finalist_forecasts_DFF",
        "holdout_forecasts_FAD",
        "holdout_forecasts_DFF",
    ):
        (root / "reports" / "eval" / f"{name}.csv").write_text(body)
    for t in ("FAD", "DFF"):
        for mdl in ("AutoBiTCN", "AutoTiDE", "AutoNHITS"):
            (root / "reports" / "campaign" / f"hpo_deep_best_{t}_{mdl}.json").write_text("{}")
    (root / "reports" / "eval" / "significance_summary.json").write_text("{}")
    (root / "reports" / "governance" / "champion_challenger.json").write_text("{}")


def test_missing_manifest_fails_closed(sandbox):
    _write_all(sandbox)  # todo presente pero SIN manifiesto
    assert gate.check(preflight=False), "sin manifiesto debe reportar problema"


def test_complete_and_fresh_passes(sandbox):
    _write_all(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")  # inicio en el pasado ⇒ todo fresco
    assert gate.check(preflight=False) == []


def test_stale_artifacts_fail(sandbox):
    _write_all(sandbox)
    _seal(sandbox, "2099-01-01T00:00:00+00:00")  # inicio en el futuro ⇒ todo stale
    problems = gate.check(preflight=False)
    assert any("STALE" in p for p in problems)
    assert len(problems) >= 16


def test_missing_block_fails(sandbox):
    _write_all(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    (sandbox / "reports" / "campaign" / "campaign_pool_DFF_employment.csv").unlink()
    problems = gate.check(preflight=False)
    assert any("CONTEO" in p and "DFF_employment" in p for p in problems)


def test_header_only_csv_fails(sandbox):
    _write_all(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    (sandbox / "reports" / "campaign" / "campaign_pool_FAD_family.csv").write_text("only_header\n")
    problems = gate.check(preflight=False)
    assert any("VACÍO" in p for p in problems)


def test_missing_hpo_config_fails(sandbox):
    _write_all(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    (sandbox / "reports" / "campaign" / "hpo_deep_best_FAD_AutoTiDE.json").unlink()
    problems = gate.check(preflight=False)
    assert any("CONTEO" in p and "hpo_deep_best_FAD" in p for p in problems)


def test_preflight_skips_freshness(sandbox):
    _write_all(sandbox)
    _seal(sandbox, "2099-01-01T00:00:00+00:00")  # todo "stale" pero preflight lo ignora
    assert gate.check(preflight=True) == []
