"""Helpers de campaña gobernados (P0R.5 · R9.2R/R9.2R2/R9.2R3 · B74/B79/B80/B81/B85/B86/B89):
merge_campaign_pools y check_deep_refit fail-closed contra el esquema REAL de producción."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time

import pandas as pd
import pytest

import tools.lock_contracts as lc
import tools.merge_campaign_pools as mcp

ROOT = lc.ROOT
_POOL_COLS = mcp._POOL_COLS  # 19 columnas canónicas


def _run(mod, cwd, extra_env=None):
    env = {"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "")}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", mod],
        cwd=str(cwd),
        env=env,
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


def _write_all_8(base, run_id="20260706T114535-5464cea", gbm_run_id=None):
    gbm_run_id = run_id + "b" if gbm_run_id is None else gbm_run_id
    camp = base / "reports" / "campaign"
    camp.mkdir(parents=True)
    (base / "reports" / "eval").mkdir(parents=True)
    for table in ("FAD", "DFF"):
        for block in ("family", "employment"):
            _pool_df(run_id, table, block, ("ets",)).to_csv(camp / f"aq_pool_nongbm_{table}_{block}.csv", index=False)
            _pool_df(gbm_run_id, table, block, ("xgboost",)).to_csv(
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


# ----------------------------- B85: texto no numérico ≠ NaN real + identidad de campaña -----------------------------


def _poison_cell(f, col, value):
    """Corrompe UNA celda escribiendo el CSV como texto (como llegaría la corrupción real)."""
    lines = f.read_text().splitlines()
    hdr = lines[0].split(",")
    i = hdr.index(col)
    row = lines[1].split(",")
    row[i] = value
    f.write_text("\n".join([lines[0], ",".join(row), *lines[2:]]) + "\n")


def test_b85_nonnumeric_metric_text_rejected(tmp_path):
    _write_all_8(tmp_path)
    _poison_cell(tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", "sel_mase", "evil")
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0, "texto no numérico coercionado a NaN fue ACEPTADO (B85)"
    assert "texto no num" in r.stderr
    assert not (tmp_path / "reports" / "campaign" / "campaign_pool_FAD_family.csv").exists()


def test_b85_real_nan_metric_still_allowed(tmp_path):
    # celda VACÍA = NaN real = modelo fallido → permitido (no confundir con texto coercionado).
    _write_all_8(tmp_path)
    _poison_cell(tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", "hold_mase", "")
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_b85_infinite_metric_rejected(tmp_path):
    _write_all_8(tmp_path)
    _poison_cell(tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", "sel_rmse", "inf")
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0
    assert "infinito" in r.stderr


def test_b85_campaign_id_mismatch_rejected(tmp_path):
    _write_all_8(tmp_path, run_id="camp_A")  # la mitad gbm queda camp_Ab ≠ camp_A
    r = _run("tools.merge_campaign_pools", tmp_path, extra_env={"CAMPAIGN_ID": "camp_A"})
    assert r.returncode != 0, "mitades de OTRA campaña fusionadas bajo CAMPAIGN_ID (B85)"
    assert "CAMPAIGN_ID" in r.stderr
    assert not (tmp_path / "reports" / "campaign" / "campaign_pool_FAD_family.csv").exists()


def test_b85_campaign_id_match_accepted(tmp_path):
    _write_all_8(tmp_path, run_id="camp_A", gbm_run_id="camp_A")
    r = _run("tools.merge_campaign_pools", tmp_path, extra_env={"CAMPAIGN_ID": "camp_A"})
    assert r.returncode == 0, r.stdout + r.stderr
    out = pd.read_csv(tmp_path / "reports" / "campaign" / "campaign_pool_FAD_family.csv", dtype={"run_id": str})
    assert (out["run_id"] == "camp_A").all()


def test_b85_campaign_id_blank_rejected(tmp_path):
    _write_all_8(tmp_path)
    r = _run("tools.merge_campaign_pools", tmp_path, extra_env={"CAMPAIGN_ID": "  "})
    assert r.returncode != 0
    assert "CAMPAIGN_ID" in r.stderr


def test_b85_standalone_mode_unchanged(tmp_path):
    # sin CAMPAIGN_ID: run_id de salida = máximo lexicográfico, original en source_run_id (B79 intacto).
    _write_all_8(tmp_path, run_id="rA", gbm_run_id="rB")
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    out = pd.read_csv(tmp_path / "reports" / "campaign" / "campaign_pool_FAD_family.csv", dtype={"run_id": str})
    assert (out["run_id"] == "rB").all()
    assert set(out["source_run_id"]) == {"rA", "rB"}


# ----------------------------- B89: exclusión concurrente + rollback en cada promoción -----------------------------


def test_b89_second_merge_waits_for_lock(tmp_path):
    # con el lock tomado por OTRO proceso, el merge debe ESPERAR (no completar ni promover nada).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    fd = os.open(str(camp / ".merge.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tools.merge_campaign_pools"],
            cwd=str(tmp_path),
            env={"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            assert proc.poll() is None, "el merge COMPLETÓ con el lock tomado por otro proceso (B89)"
            time.sleep(0.25)
        assert not (camp / "campaign_pool_FAD_family.csv").exists(), "promovió outputs bajo lock ajeno"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert proc.wait(timeout=60) == 0  # liberado el lock, opera sobre estado completo
    assert (camp / "campaign_pool_FAD_family.csv").exists()


def test_b89_two_concurrent_merges_both_complete(tmp_path):
    _write_all_8(tmp_path)
    env = {"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "tools.merge_campaign_pools"],
            cwd=str(tmp_path),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(2)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    out = pd.read_csv(tmp_path / "reports" / "campaign" / "campaign_pool_FAD_family.csv", dtype={"run_id": str})
    assert len(out) == 2  # estado completo y consistente, jamás intercalado


@pytest.mark.parametrize("fail_at", list(range(1, 9)))
def test_b89_failure_at_each_promotion_rolls_back_clean(tmp_path, monkeypatch, fail_at):
    """Fallo inyectado en CADA una de las 8 promociones, con mezcla de outputs preexistentes y ausentes:
    los preexistentes quedan byte-idénticos, los ausentes siguen ausentes, cero .bak y cero temporales."""
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    ev = tmp_path / "reports" / "eval"
    pre = {
        camp / "campaign_pool_FAD_family.csv": b"PRE-CAMP-1\n",
        camp / "campaign_pool_DFF_employment.csv": b"PRE-CAMP-2\n",
        ev / "model_comparison_DFF21.csv": b"PRE-EV-1\n",
        ev / "model_comparison_EB_FAD21.csv": b"PRE-EV-2\n",
    }
    for p, b in pre.items():
        p.write_bytes(b)
    outputs = [camp / f"campaign_pool_{t}_{blk}.csv" for t in ("FAD", "DFF") for blk in ("family", "employment")]
    outputs += [
        ev / n
        for n in (
            "model_comparison_FAD21.csv", "model_comparison_EB_FAD21.csv",
            "model_comparison_DFF21.csv", "model_comparison_EB_DFF21.csv",
        )
    ]  # fmt: skip
    missing = [p for p in outputs if p not in pre]
    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(src, dst, *a, **k):
        if str(dst).endswith(".csv") and "reports" in str(dst):  # solo promociones/restauraciones a outputs
            state["n"] += 1
            if state["n"] == fail_at:
                raise OSError("inyectado")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "replace", flaky_replace)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(OSError):
        mcp.merge()
    for p, b in pre.items():
        assert p.read_bytes() == b, f"{p.name} no quedó byte-idéntico tras el rollback (fail_at={fail_at})"
    for p in missing:
        assert not p.exists(), f"{p.name} apareció pese al rollback (fail_at={fail_at})"
    residue = [q.name for d in (camp, ev) for q in d.iterdir() if q.name.startswith(".") and q.name != ".merge.lock"]
    assert residue == [], f"temporales/respaldos residuales tras el rollback: {residue}"


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


# ----------------------------- B86: unique_id ausente/vacío -----------------------------


def test_b86_missing_unique_id_rejected(tmp_path):
    # celda vacía → NaN al leer; astype(str) la enmascararía como el string "nan" (el falso verde). La fila
    # mala va en las CINCO semillas — consistentes entre sí — para que el rechazo sea POR el uid, no por
    # una inconsistencia entre semillas.
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row(u="")])
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b86_none_unique_id_rejected(tmp_path):
    # el literal "None" es NA por defecto para read_csv → NaN → debe bloquear.
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row(u="None")])
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b86_whitespace_unique_id_rejected(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row(u="   ")])
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b81_real_shape_seeds_pass(tmp_path):
    # forma REAL de producción (25 series × 24 meses = 600 filas por semilla), idéntica entre las 5 —
    # el 600 es dato del fixture, NO contrato del checker (la elegibilidad 580/600 vive en P2b).
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    countries = ("mexico", "india", "china", "philippines", "all")
    cats = ("F1", "F2A", "F2B", "F3", "F4")
    dates = pd.date_range("2019-08-01", periods=24, freq="MS")
    rows = [
        {"unique_id": f"{c}|{cat}|FAD", "ds": d.strftime("%Y-%m-%d"), "y": float(i), "AutoBiTCN": float(i) + 0.5}
        for i, (c, cat, d) in enumerate((c, cat, d) for c in countries for cat in cats for d in dates)
    ]
    assert len(rows) == 600
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, rows)
    assert _run("tools.check_deep_refit", tmp_path).returncode == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
