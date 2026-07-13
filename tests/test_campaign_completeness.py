"""Tests del gate de completitud+frescura de campana, dos fases (auditoria 12-jul-2026)."""

from __future__ import annotations

import json

import pytest

import tools.check_campaign_completeness as gate


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    (tmp_path / "reports" / "campaign").mkdir(parents=True)
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    (tmp_path / "reports" / "governance").mkdir(parents=True)
    (tmp_path / "models").mkdir(parents=True)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "MANIFEST", tmp_path / "reports" / "campaign" / "campaign_manifest.json")
    return tmp_path


def _seal(root, started: str, cid: str = "camp1") -> None:
    (root / "reports" / "campaign" / "campaign_manifest.json").write_text(
        json.dumps({"campaign_id": cid, "sha": "abc", "started_at": started})
    )


POOL_CSV = (
    "run_id,model,hold_mase\nr1,naive1,0.10\nr2,ets,0.11\nr3,theta,0.12\nr4,drift,0.13\nr5,sarima,0.14\nr6,arima,0.15\n"
)
HPO_JSON = json.dumps({"learning_rate": 0.001, "hidden_size": 8, "dropout": 0.2})
SMALL = "col\n" + "\n".join(f"row{i}" for i in range(6))


def _write_inputs(root) -> None:
    camp = root / "reports" / "campaign"
    ev = root / "reports" / "eval"
    for blk in ("FAD_family", "FAD_employment", "DFF_family", "DFF_employment"):
        (camp / f"campaign_pool_{blk}.csv").write_text(POOL_CSV)
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
        (ev / f"{name}.csv").write_text(SMALL)
    for t in ("FAD", "DFF"):
        for mdl in ("AutoBiTCN", "AutoTiDE", "AutoNHITS"):
            (camp / f"hpo_deep_best_{t}_{mdl}.json").write_text(HPO_JSON)
        for variant in ("camp_levels", "camp_diff", "camp_diffls", "camp_auto"):
            for seed in range(1, 6):
                (camp / f"global_{t}_{variant}_s{seed}.csv").write_text(POOL_CSV)
    (ev / "tuned_params.json").write_text("{}")
    (root / "models" / "manifest.jsonl").write_text("\n".join('{"model":"m"}' for _ in range(60)))


def _write_outputs(root, cid: str = "camp1") -> None:
    (root / "reports" / "eval" / "significance_summary.json").write_text("{}")
    (root / "reports" / "governance" / "champion_challenger.json").write_text(json.dumps({"campaign_id": cid}))
    (root / "reports" / "governance" / "key_facts.json").write_text("{}")


# ── el bug critico que reporto el autor: los outputs NO se exigen en la fase inputs ──
def test_inputs_phase_passes_before_outputs_exist(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    assert gate.check("inputs", preflight=False) == []


def test_missing_manifest_fails_closed(sandbox):
    _write_inputs(sandbox)
    assert gate.check("inputs", preflight=False)


def test_missing_seed_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_s3.csv").unlink()
    probs = gate.check("inputs", preflight=False)
    assert any("SEMILLA ausente" in p and "camp_auto_s3" in p for p in probs)


def test_pool_with_nonfinite_metric_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    (sandbox / "reports" / "campaign" / "campaign_pool_FAD_family.csv").write_text(
        "run_id,model,hold_mase\nr1,naive1,\nr2,ets,nan\n"
    )
    probs = gate.check("inputs", preflight=False)
    assert any("METRICA" in p for p in probs)


def test_trivial_hpo_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    (sandbox / "reports" / "campaign" / "hpo_deep_best_FAD_AutoTiDE.json").write_text("{}")
    probs = gate.check("inputs", preflight=False)
    assert any("HPO" in p for p in probs)


def test_stale_input_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, "2099-01-01T00:00:00+00:00")
    probs = gate.check("inputs", preflight=False)
    assert any("STALE" in p or "stale" in p for p in probs)


def test_thin_manifest_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    (sandbox / "models" / "manifest.jsonl").write_text('{"model":"m"}\n')
    probs = gate.check("inputs", preflight=False)
    assert any("manifest.jsonl" in p for p in probs)


def test_outputs_phase_passes_when_present(sandbox):
    _seal(sandbox, "2000-01-01T00:00:00+00:00")
    _write_outputs(sandbox)
    assert gate.check("outputs", preflight=False) == []


def test_outputs_campaign_id_mismatch_fails(sandbox):
    _seal(sandbox, "2000-01-01T00:00:00+00:00", cid="camp1")
    _write_outputs(sandbox, cid="OTRA")
    probs = gate.check("outputs", preflight=False)
    assert any("IDENTIDAD" in p for p in probs)


def test_preflight_ignores_freshness(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, "2099-01-01T00:00:00+00:00")
    assert gate.check("inputs", preflight=True) == []
