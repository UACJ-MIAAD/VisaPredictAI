"""Helpers de campaña gobernados (P0R.5 · R9.2R/B74): merge_campaign_pools y check_deep_refit fail-closed."""

from __future__ import annotations

import subprocess
import sys

import pandas as pd
import pytest

ROOT = __import__("tools.lock_contracts", fromlist=["ROOT"]).ROOT


def _run(mod, cwd, args=()):
    return subprocess.run(
        [sys.executable, "-m", mod, *args],
        cwd=str(cwd),
        env={"PYTHONPATH": str(ROOT), "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )


def _pool(rows):
    return pd.DataFrame(rows)


def _write_all_8(base):
    """Escribe las 8 mitades válidas (2 tablas x 2 bloques x 2 mitades)."""
    camp = base / "reports" / "campaign"
    camp.mkdir(parents=True)
    (base / "reports" / "eval").mkdir(parents=True)
    for table in ("FAD", "DFF"):
        for block in ("family", "employment"):
            for kind, model in (("nongbm", "ets"), ("gbm", "xgboost")):
                _pool([{"run_id": 7, "model": model, "mase": 0.11}]).to_csv(
                    camp / f"aq_pool_{kind}_{table}_{block}.csv", index=False
                )


# ----------------------------- merge_campaign_pools -----------------------------


def test_b74_merge_requires_all_eight_halves(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    # solo una mitad presente
    _pool([{"run_id": 7, "model": "ets", "mase": 0.11}]).to_csv(camp / "aq_pool_nongbm_FAD_family.csv", index=False)
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0, r.stdout + r.stderr
    assert not (camp / "campaign_pool_FAD_family.csv").exists(), "escribió un pool PARCIAL (B74)"


def test_b74_merge_rejects_empty_half(tmp_path):
    _write_all_8(tmp_path)
    (tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv").write_text("run_id,model,mase\n")  # vacío
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0
    assert not (tmp_path / "reports" / "campaign" / "campaign_pool_FAD_family.csv").exists()


def test_b74_merge_rejects_mismatched_schema(tmp_path):
    _write_all_8(tmp_path)
    _pool([{"run_id": 7, "OTHER": "x"}]).to_csv(
        tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", index=False
    )
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0


def test_b74_merge_rejects_null_run_id(tmp_path):
    _write_all_8(tmp_path)
    _pool([{"run_id": None, "model": "xgboost", "mase": 0.1}]).to_csv(
        tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", index=False
    )
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0


def test_b74_merge_full_success_writes_all(tmp_path):
    _write_all_8(tmp_path)
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    for table in ("FAD", "DFF"):
        for block in ("family", "employment"):
            assert (tmp_path / "reports" / "campaign" / f"campaign_pool_{table}_{block}.csv").exists()
    assert (tmp_path / "reports" / "eval" / "model_comparison_FAD21.csv").exists()
    assert (tmp_path / "reports" / "eval" / "model_comparison_EB_FAD21.csv").exists()


# ----------------------------- check_deep_refit -----------------------------


def _deep_seed(camp, s, keys=((("a", "2020-01-01", 1.0)),), bitcn=0.5):
    rows = [{"unique_id": u, "ds": d, "y": y, "AutoBiTCN": bitcn} for (u, d, y) in keys]
    pd.DataFrame(rows).to_csv(camp / f"global_FAD_camp_auto_s{s}.csv", index=False)


def test_b74_check_deep_refit_requires_all_five_seeds(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3):  # faltan s4, s5
        _deep_seed(camp, s)
    r = _run("tools.check_deep_refit", tmp_path)
    assert r.returncode == 1


def test_b74_check_deep_refit_requires_bitcn_column(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        pd.DataFrame([{"unique_id": "a", "ds": "2020-01-01", "y": 1.0}]).to_csv(
            camp / f"global_FAD_camp_auto_s{s}.csv", index=False
        )  # sin AutoBiTCN
    r = _run("tools.check_deep_refit", tmp_path)
    assert r.returncode == 1


def test_b74_check_deep_refit_requires_same_keys_across_seeds(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4):
        _deep_seed(camp, s, keys=[("a", "2020-01-01", 1.0)])
    _deep_seed(camp, 5, keys=[("b", "2020-02-01", 2.0)])  # claves distintas
    r = _run("tools.check_deep_refit", tmp_path)
    assert r.returncode == 1


def test_b74_check_deep_refit_complete_exits_zero(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, keys=[("a", "2020-01-01", 1.0), ("b", "2020-02-01", 2.0)])
    r = _run("tools.check_deep_refit", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
