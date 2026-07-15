"""Helpers de campaña gobernados (P0R.5 · R9.2R/R9.2R2 · B74/B79/B80/B81): merge_campaign_pools y
check_deep_refit fail-closed contra el esquema REAL de producción."""

from __future__ import annotations

import os
import subprocess
import sys

import pandas as pd
import pytest

import tools.lock_contracts as lc
import tools.merge_campaign_pools as mcp

ROOT = lc.ROOT
_POOL_COLS = mcp._POOL_COLS  # 19 columnas canónicas


def _run(mod, cwd):
    return subprocess.run(
        [sys.executable, "-m", mod],
        cwd=str(cwd),
        env={"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )


def _pool_df(run_id, table, block, models=("ets", "theta")):
    rows = []
    for m in models:
        rows.append(
            {
                "run_id": run_id, "model": m, "country": "all", "category": "F3" if block == "family" else "EB1",
                "table": table,
                "sel_mase": 0.11, "sel_smape": 5.0, "sel_mae": 22.0, "sel_rmse": 30.0,
                "hold_mase": 0.12, "hold_smape": 6.0, "hold_mae": 25.0, "hold_rmse": 33.0, "hold_msis": 1.0,
                "hold_interval_score": 2.0, "hold_coverage": 0.95, "sel_mase1": 0.10, "hold_mase1": 0.13,
                "secs": 1.5,
            }
        )  # fmt: skip
    return pd.DataFrame(rows)[list(_POOL_COLS)]


def _write_all_8(base, run_id="20260706T114535-5464cea"):
    camp = base / "reports" / "campaign"
    camp.mkdir(parents=True)
    (base / "reports" / "eval").mkdir(parents=True)
    for table in ("FAD", "DFF"):
        for block in ("family", "employment"):
            _pool_df(run_id, table, block, ("ets",)).to_csv(camp / f"aq_pool_nongbm_{table}_{block}.csv", index=False)
            _pool_df(run_id + "b", table, block, ("xgboost",)).to_csv(
                camp / f"aq_pool_gbm_{table}_{block}.csv", index=False
            )


# ----------------------------- merge: run_id REAL (B79) -----------------------------


@pytest.mark.parametrize("rid", ["20260706T034508-5464cea", "rederiv_5464cea_20260706T034508"])
def test_b79_merge_accepts_string_run_id(tmp_path, rid):
    _write_all_8(tmp_path, run_id=rid)
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    out = pd.read_csv(tmp_path / "reports" / "campaign" / "campaign_pool_FAD_family.csv", dtype={"run_id": str})
    assert out["run_id"].iloc[0] in (rid, rid + "b")  # máximo lexicográfico de las dos mitades


# ----------------------------- merge: esquema exacto (B80) -----------------------------


def test_b80_merge_rejects_minimal_schema(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    for table in ("FAD", "DFF"):
        for block in ("family", "employment"):
            for kind in ("nongbm", "gbm"):
                pd.DataFrame([{"run_id": "r", "model": "ets"}]).to_csv(
                    camp / f"aq_pool_{kind}_{table}_{block}.csv", index=False
                )
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0
    assert not (camp / "campaign_pool_FAD_family.csv").exists()


def test_b80_merge_rejects_missing_column(tmp_path):
    _write_all_8(tmp_path)
    df = _pool_df("r1", "FAD", "family").drop(columns=["secs"])
    df.to_csv(tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", index=False)
    assert _run("tools.merge_campaign_pools", tmp_path).returncode != 0


def test_b80_merge_rejects_table_mismatch(tmp_path):
    _write_all_8(tmp_path)
    _pool_df("r1", "DFF", "family").to_csv(  # table=DFF en un fichero FAD
        tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", index=False
    )
    assert _run("tools.merge_campaign_pools", tmp_path).returncode != 0


def test_b80_merge_rejects_empty_category(tmp_path):
    _write_all_8(tmp_path)
    df = _pool_df("r1", "FAD", "family")
    df["category"] = ""
    df.to_csv(tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", index=False)
    assert _run("tools.merge_campaign_pools", tmp_path).returncode != 0


def test_b80_merge_rejects_multiple_run_id_in_half(tmp_path):
    _write_all_8(tmp_path)
    df = pd.concat([_pool_df("rA", "FAD", "family"), _pool_df("rB", "FAD", "family")], ignore_index=True)
    df.to_csv(tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", index=False)
    assert _run("tools.merge_campaign_pools", tmp_path).returncode != 0


def test_b80_merge_rejects_symlink_half(tmp_path):
    _write_all_8(tmp_path)
    target = tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv"
    outside = tmp_path / "outside.csv"
    _pool_df("r1", "FAD", "family").to_csv(outside, index=False)
    target.unlink()
    target.symlink_to(outside)
    assert _run("tools.merge_campaign_pools", tmp_path).returncode != 0


def test_b80_merge_full_success(tmp_path):
    _write_all_8(tmp_path)
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    for table in ("FAD", "DFF"):
        for block in ("family", "employment"):
            assert (tmp_path / "reports" / "campaign" / f"campaign_pool_{table}_{block}.csv").exists()


# ----------------------------- merge: promoción transaccional (B80) -----------------------------


def test_b80_injected_failure_rolls_back_no_partial(tmp_path, monkeypatch):
    # falla en la 2ª promoción -> el output preexistente sobrevive byte-idéntico (rollback transaccional).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_text("PREEXISTING\n")
    pre_bytes = pre.read_bytes()
    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(src, dst, *a, **k):
        if str(dst).endswith(".csv") and "reports" in str(dst):  # solo promociones a outputs finales .csv
            state["n"] += 1
            if state["n"] == 2:
                raise OSError("inyectado")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "replace", flaky_replace)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(OSError):
        mcp.merge()
    assert pre.read_bytes() == pre_bytes, "el output preexistente NO sobrevivió byte-idéntico al rollback"


# ----------------------------- check_deep_refit (B81) -----------------------------


def _deep_seed(camp, s, rows):
    pd.DataFrame(rows).to_csv(camp / f"global_FAD_camp_auto_s{s}.csv", index=False)


def _row(u="a", d="2020-01-01", y=1.0, bitcn=0.5):
    return {"unique_id": u, "ds": d, "y": y, "AutoBiTCN": bitcn}


def test_b81_check_deep_refit_rejects_duplicate_key_in_seed(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    _deep_seed(camp, 1, [_row(), _row()])  # (unique_id, ds) duplicado
    for s in (2, 3, 4, 5):
        _deep_seed(camp, s, [_row()])
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b81_check_deep_refit_rejects_different_row_counts(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4):
        _deep_seed(camp, s, [_row("a"), _row("b", "2020-02-01")])
    _deep_seed(camp, 5, [_row("a")])  # menos filas
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b81_check_deep_refit_rejects_symlink(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4):
        _deep_seed(camp, s, [_row()])
    outside = tmp_path / "outside.csv"
    pd.DataFrame([_row()]).to_csv(outside, index=False)
    (camp / "global_FAD_camp_auto_s5.csv").symlink_to(outside)
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b81_check_deep_refit_rejects_bad_ds(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    _deep_seed(camp, 1, [_row(d="not-a-date")])
    for s in (2, 3, 4, 5):
        _deep_seed(camp, s, [_row()])
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b81_check_deep_refit_rejects_nonfinite_y(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    _deep_seed(camp, 1, [_row(y=float("inf"))])
    for s in (2, 3, 4, 5):
        _deep_seed(camp, s, [_row()])
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b81_check_deep_refit_complete_exits_zero(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row("a"), _row("b", "2020-02-01", 2.0)])
    assert _run("tools.check_deep_refit", tmp_path).returncode == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
