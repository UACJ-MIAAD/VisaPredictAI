"""Tests del gate bifasico con CONTRATOS REALES por productor (auditoria 12/13-jul-2026)."""

from __future__ import annotations

import json

import pytest

import tools.check_campaign_completeness as gate


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for sub in ("reports/campaign", "reports/eval", "reports/governance", "models/FAD/local", "models/FAD/global"):
        (tmp_path / sub).mkdir(parents=True)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "MANIFEST", tmp_path / "reports" / "campaign" / "campaign_manifest.json")
    return tmp_path


def _seal(root, started="2000-01-01T00:00:00+00:00", cid="camp1", sha="abc"):
    (root / "reports" / "campaign" / "campaign_manifest.json").write_text(
        json.dumps({"campaign_id": cid, "sha": sha, "git_sha": sha, "dirty": False, "started_at": started})
    )


def _pool(n_series=25, n_models=24, nan=3):
    hdr = "country,category,table,model,hold_mase"
    rows = [f"c{s},F{s % 5},FAD,m{m},{0.1 + 0.01 * m}" for s in range(n_series) for m in range(n_models)]
    rows += ["cX,F9,FAD,m0,nan"] * nan  # series inelegibles (legitimo)
    return hdr + "\n" + "\n".join(rows) + "\n"


def _seed(models):
    cols = "unique_id,ds," + "y," + ",".join(models)
    body = "\n".join("s1,2026-01-01,100," + ",".join(str(1.0 + i) for i, _ in enumerate(models)) for _ in range(3))
    return cols + "\n" + body + "\n"


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
TUNED = {
    gbm: {g: {"lr": 0.1} for g in ("FAD_family", "DFF_family", "FAD_employment", "DFF_employment")}
    for gbm in ("catboost", "lightgbm", "xgboost")
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
        for variant in ("camp_levels", "camp_diff", "camp_diffls"):
            for seed in range(1, 6):
                (camp / f"global_{t}_{variant}_s{seed}.csv").write_text(_seed(("NHITS", "PatchTST", "TiDE", "BiTCN")))
        for seed in range(1, 6):
            (camp / f"global_{t}_camp_auto_s{seed}.csv").write_text(_seed(("AutoBiTCN", "AutoTiDE", "AutoNHITS")))
    (ev / "tuned_params.json").write_text(json.dumps(TUNED))
    # manifest con locales+globales cuyas rutas EXISTEN
    (root / "models" / "FAD" / "local" / "m.pkl").write_text("x")
    (root / "models" / "FAD" / "global" / "d.pt").write_text("x")
    entry_l = {"model": "ets", "type": "local", "path": "models/FAD/local/m.pkl", "git_sha": "abc", "panel_hash": "h"}
    entry_g = {
        "model": "bitcn",
        "type": "global_deep",
        "path": "models/FAD/global/d.pt",
        "git_sha": "abc",
        "panel_hash": "h",
    }
    lines = [json.dumps(entry_l)] * 260 + [json.dumps(entry_g)] * 8
    (root / "models" / "manifest.jsonl").write_text("\n".join(lines) + "\n")


def _write_outputs(root, cid="camp1", sha="abc"):
    (root / "reports" / "eval" / "significance_summary.json").write_text(
        json.dumps({"ranking": {"FAD": {}, "DFF": {}}, "dm": {"FAD": {}, "DFF": {}}})
    )
    tbl = {"champion": "naive1", "champion_mean": 0.1, "challengers": []}
    (root / "reports" / "governance" / "champion_challenger.json").write_text(
        json.dumps({"FAD": tbl, "DFF": tbl, "campaign_id": cid, "git_sha": sha})
    )
    kf = {k: 1 for k in ("n_series_structural", "n_obs", "fad_champion_mean", "dff_champion_mean", "n_models")}
    (root / "reports" / "governance" / "key_facts.json").write_text(json.dumps(kf))


# ── happy path: sin falsos rechazos ──
def test_inputs_pass_with_realistic_artifacts(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    assert gate.check("inputs", preflight=False) == []


def test_outputs_pass_with_realistic_artifacts(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    assert gate.check("outputs", preflight=False) == []


def test_missing_manifest_fails_closed(sandbox):
    _write_inputs(sandbox)
    assert gate.check("inputs", preflight=False)


# ── #1 HPO por modelo ──
def test_autonhits_valid_passes_without_hidden_size(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    assert not [p for p in gate.check("inputs", preflight=False) if "AutoNHITS" in p]


def test_autonhits_missing_own_key_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    bad = {k: v for k, v in HPO["AutoNHITS"].items() if k != "n_pool_kernel_size"}
    (sandbox / "reports" / "campaign" / "hpo_deep_best_FAD_AutoNHITS.json").write_text(json.dumps(bad))
    assert any("AutoNHITS" in p and "n_pool_kernel_size" in p for p in gate.check("inputs", preflight=False))


# ── #2 pools: modelo + elegibilidad ──
def test_pool_without_model_column_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "campaign_pool_FAD_family.csv").write_text(
        "country,category,table,hold_mase\nc0,F1,FAD,0.1\n" * 25
    )
    assert any("POOL" in p and "FAD_family" in p for p in gate.check("inputs", preflight=False))


def test_pool_single_model_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "campaign_pool_FAD_family.csv").write_text(_pool(n_series=25, n_models=1))
    assert any("POOL" in p and "modelos" in p for p in gate.check("inputs", preflight=False))


def test_degenerate_pool_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "campaign_pool_FAD_family.csv").write_text(_pool(n_series=3))
    assert any("POOL" in p and "series elegibles" in p for p in gate.check("inputs", preflight=False))


# ── #3 semillas: conjunto exacto + contenido ──
def test_extra_string_seed_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_sOLD.csv").write_text(
        _seed(("AutoBiTCN", "AutoTiDE", "AutoNHITS"))
    )
    assert any("SEMILLAS" in p and "sobra" in p for p in gate.check("inputs", preflight=False))


def test_zero_padded_seed_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_s01.csv").write_text(
        _seed(("AutoBiTCN", "AutoTiDE", "AutoNHITS"))
    )
    assert any("SEMILLAS" in p and "sobra" in p for p in gate.check("inputs", preflight=False))


def test_missing_seed_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_s3.csv").unlink()
    assert any("SEMILLAS" in p and "falta" in p for p in gate.check("inputs", preflight=False))


def test_seed_missing_model_column_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_s2.csv").write_text("unique_id,ds,y\ns1,2026-01-01,100\n")
    assert any("SEMILLA" in p and "faltan columnas" in p for p in gate.check("inputs", preflight=False))


def test_seed_no_finite_forecast_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    (sandbox / "reports" / "campaign" / "global_FAD_camp_auto_s2.csv").write_text(
        "unique_id,ds,y,AutoBiTCN,AutoTiDE,AutoNHITS\ns1,2026-01-01,100,nan,nan,nan\n"
    )
    assert any("pronostico finito" in p for p in gate.check("inputs", preflight=False))


# ── #5 tuned (12 grupos) + manifest (rutas, identidad, globales) ──
def test_tuned_missing_group_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    bad = {gbm: {"FAD_family": {}} for gbm in ("catboost", "lightgbm", "xgboost")}  # solo 1 grupo
    (sandbox / "reports" / "eval" / "tuned_params.json").write_text(json.dumps(bad))
    assert any("TUNED" in p and "grupos" in p for p in gate.check("inputs", preflight=False))


def test_manifest_without_global_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    entry_l = {"model": "ets", "type": "local", "path": "models/FAD/local/m.pkl", "git_sha": "abc", "panel_hash": "h"}
    (sandbox / "models" / "manifest.jsonl").write_text("\n".join(json.dumps(entry_l) for _ in range(260)))
    assert any("MANIFEST" in p and "globales" in p for p in gate.check("inputs", preflight=False))


def test_manifest_missing_path_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    entry = {"model": "x", "type": "local", "path": "models/does/not/exist.pkl", "git_sha": "abc", "panel_hash": "h"}
    (sandbox / "models" / "manifest.jsonl").write_text("\n".join(json.dumps(entry) for _ in range(260)))
    assert any("MANIFEST" in p and "no existen" in p for p in gate.check("inputs", preflight=False))


def test_manifest_missing_identity_fails(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    entry = {"model": "x", "type": "local", "path": "models/FAD/local/m.pkl"}  # sin git_sha/panel_hash
    (sandbox / "models" / "manifest.jsonl").write_text("\n".join(json.dumps(entry) for _ in range(260)))
    assert any("MANIFEST" in p and "claves" in p for p in gate.check("inputs", preflight=False))


# ── #4 outputs: contenido anidado + identidad completa ──
def test_trivial_significance_fails(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    (sandbox / "reports" / "eval" / "significance_summary.json").write_text(json.dumps({"ranking": {}, "dm": {}}))
    assert any("SIGNIFICANCIA" in p for p in gate.check("outputs", preflight=False))


def test_key_facts_missing_badge_fails(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    (sandbox / "reports" / "governance" / "key_facts.json").write_text(json.dumps({f"k{i}": i for i in range(25)}))
    assert any("KEY_FACTS" in p and "insignia" in p for p in gate.check("outputs", preflight=False))


def test_champion_non_finite_mean_fails(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    bad = {"champion": "x", "champion_mean": "nan", "challengers": []}
    (sandbox / "reports" / "governance" / "champion_challenger.json").write_text(
        json.dumps({"FAD": bad, "DFF": bad, "campaign_id": "camp1", "git_sha": "abc"})
    )
    assert any("champion_mean no finito" in p for p in gate.check("outputs", preflight=False))


def test_champion_without_git_sha_fails(sandbox):
    _seal(sandbox)
    _write_outputs(sandbox)
    tbl = {"champion": "n", "champion_mean": 0.1, "challengers": []}
    (sandbox / "reports" / "governance" / "champion_challenger.json").write_text(
        json.dumps({"FAD": tbl, "DFF": tbl, "campaign_id": "camp1"})  # sin git_sha
    )
    assert any("git_sha" in p for p in gate.check("outputs", preflight=False))


def test_champion_wrong_campaign_id_fails(sandbox):
    _seal(sandbox, cid="camp1")
    _write_outputs(sandbox, cid="OTRA")
    assert any("campaign_id" in p and "OTRA" in p for p in gate.check("outputs", preflight=False))


def test_preflight_ignores_freshness(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox, started="2099-01-01T00:00:00+00:00")
    assert gate.check("inputs", preflight=True) == []


# ── regresión productor↔gate (auditoría 13-jul): el gate debe aceptar la forma EXACTA
#    que escribe save_finalists_deep, y el productor DEBE incluir git_sha/panel_hash. ──
def test_gate_accepts_real_deep_manifest_entry(sandbox):
    _write_inputs(sandbox)
    _seal(sandbox)
    # entrada EXACTA de save_finalists_deep (con la identidad que ahora escribe)
    (sandbox / "models" / "FAD" / "global" / "d.pt").write_text("x")
    real_deep = {
        "model": "AutoBiTCN",
        "table": "FAD",
        "type": "global_deep",
        "recipe": "diff+global+HPO",
        "path": "models/FAD/global/d.pt",
        "n_series": 25,
        "git_sha": "abc",
        "git_dirty": False,
        "panel_hash": "h",
    }
    local = {"model": "ets", "type": "local", "path": "models/FAD/local/m.pkl", "git_sha": "abc", "panel_hash": "h"}
    (sandbox / "models" / "manifest.jsonl").write_text(
        "\n".join([json.dumps(local)] * 260 + [json.dumps(real_deep)] * 8) + "\n"
    )
    assert not [p for p in gate.check("inputs", preflight=False) if p.startswith("MANIFEST")]


def test_deep_producer_source_writes_identity():
    # guarda contra la regresión que reportó el autor: save_finalists_deep NO escribía
    # git_sha/panel_hash y el gate rechazaba un global válido.
    import pathlib

    repo = pathlib.Path(gate.__file__).resolve().parent.parent
    text = (repo / "experiments" / "save_finalists_deep.py").read_text()
    assert "_identity()" in text and "git_sha" in text and "panel_hash" in text
