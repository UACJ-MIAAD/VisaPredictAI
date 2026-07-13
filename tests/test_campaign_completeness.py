"""Tests del gate bifasico con CONTRATOS REALES por productor (auditoria 12/13-jul-2026)."""

from __future__ import annotations

import json

import pytest

import tools.check_campaign_completeness as gate


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for sub in ("reports/campaign", "reports/eval", "reports/governance", "models"):
        (tmp_path / sub).mkdir(parents=True)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "MANIFEST", tmp_path / "reports" / "campaign" / "campaign_manifest.json")
    return tmp_path


def _seal(root, started="2000-01-01T00:00:00+00:00", cid="camp1"):
    (root / "reports" / "campaign" / "campaign_manifest.json").write_text(
        json.dumps({"campaign_id": cid, "sha": "abc", "started_at": started})
    )


# --- pool con 25 series elegibles (>= piso) y no-finitos legitimos ---
def _pool(n_series=25, n_models=4, inject_nan=3):
    hdr = "country,category,table,hold_mase"
    rows = []
    for s in range(n_series):
        for m in range(n_models):
            rows.append(f"c{s},F{m % 3},FAD,{0.1 + 0.01 * m}")
    for _ in range(inject_nan):  # series inelegibles: hold_mase no finito (legitimo)
        rows.append("cX,F9,FAD,nan")
    return hdr + "\n" + "\n".join(rows) + "\n"


HPO = {
    "AutoBiTCN": {
        "learning_rate": 1e-3,
        "max_steps": 800,
        "input_size": 18,
        "scaler_type": "robust",
        "hidden_size": 8,
        "dropout": 0.2,
    },
    "AutoNHITS": {
        "learning_rate": 1e-3,
        "max_steps": 800,
        "input_size": 18,
        "scaler_type": "robust",
        "n_pool_kernel_size": [2],
        "n_freq_downsample": [1],
    },
    "AutoTiDE": {
        "learning_rate": 1e-3,
        "max_steps": 800,
        "input_size": 18,
        "scaler_type": "robust",
        "hidden_size": 8,
        "decoder_output_dim": 4,
    },
}
SMALL = "col\n" + "\n".join(f"r{i}" for i in range(6))


def _write_inputs(root):
    camp, ev = root / "reports" / "campaign", root / "reports" / "eval"
    for blk, n in (("FAD_family", 25), ("FAD_employment", 30), ("DFF_family", 25), ("DFF_employment", 16)):
        (camp / f"campaign_pool_{blk}.csv").write_text(_pool(n_series=n))
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
        for mdl, cfg in HPO.items():
            (camp / f"hpo_deep_best_{t}_{mdl}.json").write_text(json.dumps(cfg))
        for variant in ("camp_levels", "camp_diff", "camp_diffls", "camp_auto"):
            for seed in range(1, 6):
                (camp / f"global_{t}_{variant}_s{seed}.csv").write_text("m,x\na,1\n")
    (ev / "tuned_params.json").write_text(json.dumps({"catboost": {}, "lightgbm": {}, "xgboost": {}}))
    (root / "models" / "manifest.jsonl").write_text(
        "\n".join('{"model":"ets","type":"local"}' for _ in range(60)) + '\n{"model":"bitcn","type":"global_deep"}\n'
    )


def _write_outputs(root, cid="camp1"):
    (root / "reports" / "eval" / "significance_summary.json").write_text(json.dumps({"ranking": {}, "dm": {}}))
    (root / "reports" / "governance" / "champion_challenger.json").write_text(
        json.dumps({"FAD": {}, "DFF": {}, "campaign_id": cid})
    )
    (root / "reports" / "governance" / "key_facts.json").write_text(json.dumps({f"k{i}": i for i in range(25)}))


# ── el bug del orden: inputs NO exige los outputs ──
def test_inputs_pass_before_outputs_exist(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    assert gate.check("inputs", preflight=False) == []


def test_missing_manifest_fails_closed(sandbox):
    _write_inputs(sandbox)
    assert gate.check("inputs", preflight=False)


# ── #1 HPO por modelo: NHITS valido pasa; sin su llave propia falla ──
def test_autonhits_valid_passes(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    assert not [p for p in gate.check("inputs", preflight=False) if "AutoNHITS" in p]


def test_autonhits_missing_own_key_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    bad = {k: v for k, v in HPO["AutoNHITS"].items() if k != "n_pool_kernel_size"}
    (sandbox / "reports" / "campaign" / "hpo_deep_best_FAD_AutoNHITS.json").write_text(json.dumps(bad))
    assert any("AutoNHITS" in p and "n_pool_kernel_size" in p for p in gate.check("inputs", preflight=False))


def test_hidden_size_not_required_for_nhits(sandbox):
    # regresion del falso-rechazo: AutoNHITS NO tiene hidden_size y debe pasar
    _write_inputs(sandbox)
    _seal(sandbox)
    assert not [p for p in gate.check("inputs", preflight=False) if "AutoNHITS" in p and "hidden_size" in p]


# ── #2 pools: elegibilidad, no "todo finito" ──
def test_pool_with_legit_nonfinite_passes(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    assert not [p for p in gate.check("inputs", preflight=False) if p.startswith("POOL")]


def test_degenerate_pool_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "campaign_pool_FAD_family.csv").write_text(_pool(n_series=3))  # <20
    assert any(p.startswith("POOL") and "FAD_family" in p for p in gate.check("inputs", preflight=False))


# ── #3 semillas exactas ──
def test_extra_sixth_seed_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_s6.csv").write_text("m,x\na,1\n")
    assert any("SEMILLAS" in p and "sobra" in p for p in gate.check("inputs", preflight=False))


def test_missing_seed_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_s3.csv").unlink()
    assert any("SEMILLAS" in p and "falta" in p for p in gate.check("inputs", preflight=False))


def test_empty_seed_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_s2.csv").write_text("only_header\n")
    assert any("SEMILLA vacia" in p for p in gate.check("inputs", preflight=False))


# ── #5 tuned + manifest ──
def test_trivial_tuned_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "eval" / "tuned_params.json").write_text("{}")
    assert any("TUNED" in p for p in gate.check("inputs", preflight=False))


def test_manifest_without_global_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "models" / "manifest.jsonl").write_text(
        "\n".join('{"model":"ets","type":"local"}' for _ in range(60))  # 0 global
    )
    assert any("MANIFEST" in p and "globales" in p for p in gate.check("inputs", preflight=False))


def test_junk_manifest_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "models" / "manifest.jsonl").write_text("\n".join("garbage" for _ in range(60)))
    assert any("MANIFEST" in p for p in gate.check("inputs", preflight=False))


# ── #4 outputs: contenido + identidad obligatoria ──
def test_outputs_valid_passes(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    assert gate.check("outputs", preflight=False) == []


def test_trivial_significance_fails(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    (sandbox / "reports" / "eval" / "significance_summary.json").write_text("{}")
    assert any("SIGNIFICANCIA" in p for p in gate.check("outputs", preflight=False))


def test_trivial_key_facts_fails(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    (sandbox / "reports" / "governance" / "key_facts.json").write_text("{}")
    assert any("KEY_FACTS" in p for p in gate.check("outputs", preflight=False))


def test_champion_without_campaign_id_fails(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    (sandbox / "reports" / "governance" / "champion_challenger.json").write_text(json.dumps({"FAD": {}, "DFF": {}}))
    assert any("CHAMPION" in p and "campaign_id" in p for p in gate.check("outputs", preflight=False))


def test_champion_wrong_campaign_id_fails(sandbox):
    _seal(sandbox, cid="camp1")
    _write_outputs(sandbox, cid="OTRA")
    assert any("CHAMPION" in p and "OTRA" in p for p in gate.check("outputs", preflight=False))


def test_preflight_ignores_freshness(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, started="2099-01-01T00:00:00+00:00")
    assert gate.check("inputs", preflight=True) == []
