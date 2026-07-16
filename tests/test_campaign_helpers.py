"""Helpers de campaña gobernados (P0R.5 · R9.2R…R9.2R5 · B74/B79/B80/B81/B85/B86/B89/B90/B91/B92/B93/B94/B95):
merge_campaign_pools y check_deep_refit fail-closed contra el esquema REAL de producción."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time

import pandas as pd
import pytest

import tools.check_deep_refit as cdr
import tools.governed_read as gr
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


# R9.2R9: la limpieza es por CUARENTENA (B117), no unlink — `.merge-quarantine/<txid>/` puede persistir
# (su GC vive en P2b). "Residuo suelto" = dotfiles temporales/respaldo DIRECTAMENTE en camp/ev (no el lock ni
# la cuarentena inventariada). Todo objeto en cuarentena DEBE aparecer en su MANIFEST.jsonl.
def _loose_residue(*dirs):
    skip = {".merge.lock", mcp._QUARANTINE_DIR}
    return [q.name for d in dirs for q in d.iterdir() if q.name.startswith(".") and q.name not in skip]


def _assert_quarantine_manifested(base):
    import json as _json

    for qroot in base.rglob(mcp._QUARANTINE_DIR):
        for txid in qroot.iterdir():
            if not txid.is_dir():
                continue
            manifest = txid / "MANIFEST.jsonl"
            named = set()
            if manifest.exists():
                assert (manifest.stat().st_mode & 0o777) == 0o600, "manifiesto de cuarentena no es 0600"
                for line in manifest.read_text().splitlines():
                    named.add(_json.loads(line)["quarantined_as"])
            for item in txid.iterdir():
                if item.name != "MANIFEST.jsonl":
                    assert item.name in named, f"objeto en cuarentena sin manifiesto: {item}"


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


# ----------------------------- B90: gobernanza de rutas del merge (ancestros symlink + swaps) -----------------------------


def _symlink_dir(link_path, external_target):
    ext = external_target
    ext.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_dir() and not link_path.is_symlink():
            for c in link_path.iterdir():
                c.replace(ext / c.name)
            link_path.rmdir()
        else:
            link_path.unlink()
    link_path.symlink_to(ext)
    return ext


def test_b90_campaign_symlink_to_external_rejected(tmp_path):
    _write_all_8(tmp_path)
    ext = _symlink_dir(tmp_path / "reports" / "campaign", tmp_path / "external_camp")
    (ext / "sentinel.txt").write_bytes(b"UNTOUCHED\n")
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0, "reports/campaign symlink a árbol externo fue ACEPTADO (B90)"
    assert not (ext / ".merge.lock").exists(), "creó el lock FUERA del repo"
    assert not list(ext.glob("campaign_pool_*.csv")), "promovió outputs FUERA del repo"
    assert (ext / "sentinel.txt").read_bytes() == b"UNTOUCHED\n"


def test_b90_eval_symlink_to_external_rejected(tmp_path):
    _write_all_8(tmp_path)
    ext = _symlink_dir(tmp_path / "reports" / "eval", tmp_path / "external_eval")
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0
    assert not list(ext.glob("model_comparison_*.csv")), "escribió model_comparison FUERA del repo"


def test_b90_reports_symlink_to_external_rejected(tmp_path):
    _write_all_8(tmp_path)
    # mueve el reports COMPLETO a un externo y deja reports como symlink (ancestro de campaign y eval).
    ext = tmp_path / "external_reports"
    (tmp_path / "reports").replace(ext)
    (tmp_path / "reports").symlink_to(ext)
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0, "reports symlink (ancestro) fue ACEPTADO (B90)"


def test_b90_broken_campaign_symlink_rejected(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "eval").mkdir()
    (tmp_path / "reports" / "campaign").symlink_to(tmp_path / "does_not_exist")
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0


def test_b90_group_writable_campaign_rejected(tmp_path):
    _write_all_8(tmp_path)
    os.chmod(tmp_path / "reports" / "campaign", 0o775)  # escribible por grupo
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0
    assert "escribible" in r.stderr or "gobernado" in r.stderr


def test_b93_lock_recreated_during_wait_rejected(tmp_path, monkeypatch):
    # si el .merge.lock se sustituye por otro inode (nlink/identidad) entre el open y el post-flock, el
    # segundo _check_lock_fd debe cazarlo. Aquí forzamos un hardlink (nlink==2) tras adquirir el flock.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_flock = mcp.fcntl.flock

    def hooked_flock(fd, op):
        r = real_flock(fd, op)
        # crea un hardlink al lock → nlink pasa a 2; el _check_lock_fd posterior debe fallar
        os.link(camp / ".merge.lock", camp / ".merge.lock.hardlink")
        return r

    monkeypatch.setattr(mcp.fcntl, "flock", hooked_flock)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        mcp.merge()


def test_b90_ancestor_swap_after_lock_aborts(tmp_path, monkeypatch):
    # swap del contenido de campaign tras adquirir el lock: la reverificación de identidad debe abortar.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    external = tmp_path / "external_camp"
    external.mkdir()
    real_reverify = mcp._Chain.reverify
    state = {"n": 0}

    def swapping_reverify(self, when):
        state["n"] += 1
        if state["n"] == 1:  # justo tras adquirir el lock: reemplaza el DIRECTORIO campaign por otro inode
            camp.rename(tmp_path / "reports" / ".campaign_old")
            external.rename(camp)
        return real_reverify(self, when)

    monkeypatch.setattr(mcp._Chain, "reverify", swapping_reverify)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        mcp.merge()
    assert not list(camp.glob("campaign_pool_*.csv")), "promovió al directorio swapeado"


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
    los preexistentes quedan byte-idénticos, los ausentes siguen ausentes, cero .bak y cero temporales.
    Rollback fd-relativo: os.replace ahora usa nombres relativos + src_dir_fd/dst_dir_fd; las 8 primeras
    llamadas a os.replace son las promociones (temporales/respaldos usan open+write, no replace)."""
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
        state["n"] += 1  # las 8 primeras llamadas a os.replace son las promociones
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
    residue = _loose_residue(camp, ev)
    assert residue == [], f"temporales/respaldos sueltos tras el rollback (fail_at={fail_at}): {residue}"
    _assert_quarantine_manifested(tmp_path)


def test_b95_merge_group_writable_csv_rejected(tmp_path):
    _write_all_8(tmp_path)
    os.chmod(tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", 0o666)
    r = _run("tools.merge_campaign_pools", tmp_path)
    assert r.returncode != 0, "CSV escribible por grupo/otros fue ACEPTADO (B95)"
    assert "escribible" in r.stderr


def test_b95_merge_other_writable_csv_rejected(tmp_path):
    _write_all_8(tmp_path)
    os.chmod(tmp_path / "reports" / "campaign" / "aq_pool_gbm_FAD_family.csv", 0o664)
    assert _run("tools.merge_campaign_pools", tmp_path).returncode != 0


def test_b95_merge_csv_mutated_during_read_rejected(tmp_path, monkeypatch):
    # muta EN SITIO una mitad de entrada tras leerla (mismo inode): el lease (fd vivo + snapshot + digest) lo
    # revalida antes de promover → aborta, el output jamás corresponde a un CSV oficial distinto (B95/B115).
    _write_all_8(tmp_path)
    target = tmp_path / "reports" / "campaign" / "aq_pool_nongbm_FAD_family.csv"  # la 1ª mitad leída
    real_read_csv = mcp.pd.read_csv
    done = {"x": False}

    def mutating_read_csv(buf, *a, **k):
        df = real_read_csv(buf, *a, **k)
        if not done["x"]:  # tras capturar los bytes de la 1ª mitad, muta su fichero en sitio (crece → snapshot difiere)
            done["x"] = True
            with open(target, "ab") as extra:
                extra.write(b"\n")
        return df

    monkeypatch.setattr(mcp.pd, "read_csv", mutating_read_csv)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((SystemExit, mcp.RollbackError)):
        mcp.merge()
    assert not (tmp_path / "reports" / "campaign" / "campaign_pool_FAD_family.csv").exists()


def test_b94_backup_cleanup_failure_after_success_not_ok(tmp_path, monkeypatch):
    # fallo al poner un backup en CUARENTENA tras el COMMIT → CommittedStateError (B104/B112/B117: post-commit
    # tipado; los outputs nuevos ya son la autoridad, un residuo/limpieza fallida NO es verde).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")  # fuerza un backup
    real_rename = os.rename

    def flaky_rename(src, dst, *a, **k):
        if ".bak." in str(src):  # el move de cuarentena del backup post-commit
            raise PermissionError("inyectado")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "rename", flaky_rename)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError):
        mcp.merge()


def test_b94_rollback_cleanup_failure_raises_with_context(tmp_path, monkeypatch):
    # fallo de limpieza DURANTE el rollback → error explícito (no silenciado), con el error original.
    _write_all_8(tmp_path)
    real_replace = os.replace
    real_rename = os.rename
    state = {"n": 0}

    def flaky_replace(src, dst, *a, **k):
        state["n"] += 1
        if state["n"] == 3:
            raise OSError("promo-inyectado")
        return real_replace(src, dst, *a, **k)

    def flaky_rename(src, dst, *a, **k):  # la cuarentena del temporal durante el rollback falla
        if ".tmp." in str(src):
            raise PermissionError("cleanup-inyectado")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "replace", flaky_replace)
    monkeypatch.setattr(mcp.os, "rename", flaky_rename)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError) as exc:
        mcp.merge()
    assert "rollback" in str(exc.value)


# ----------------------------- B100-B104: propiedad por descriptor + errores tipados -----------------------------


def _bak_files(camp):
    return [p.name for p in camp.iterdir() if ".bak." in p.name]


def _temp_files(camp, ev):
    return [q.name for d in (camp, ev) for q in d.iterdir() if ".tmp." in q.name]


def test_b100_quarantine_never_destroys_foreign(tmp_path):
    # la CUARENTENA (B117) jamás DESTRUYE un objeto ajeno: si el nombre fue sustituido por un inode ajeno, se
    # mueve y PRESERVA (resultado FOREIGN_OBJECT_PRESERVED), nunca se borra — sin ventana check→unlink.
    d = tmp_path / "reports" / "campaign"
    d.mkdir(parents=True)
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    quar = mcp._Quarantine("test.deadbeefdeadbeef")
    errs: list[str] = []
    try:
        name, fd = mcp._create_governed(dfd, "x", "tmp", 0)  # nuestro artefacto
        os.unlink(name, dir_fd=dfd)  # lo quitamos y ponemos un AJENO con el mismo nombre
        foreign = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dfd)
        os.write(foreign, b"FOREIGN\n")
        os.close(foreign)
        o = mcp._Out(dfd, "campaign", "x", None)
        res = quar.move(o, name, fd, phase="TEST", reason="foreign-substitution")
        assert res == mcp._FOREIGN_OBJECT_PRESERVED
        survivors = [p for p in d.rglob("*") if p.is_file() and p.read_bytes() == b"FOREIGN\n"]
        assert len(survivors) == 1, "el objeto ajeno fue destruido (B100/B117)"
        os.close(fd)
    finally:
        quar.close(errs)
        os.close(dfd)


def test_b100_predictive_sentinel_untouched(tmp_path):
    # nombres de nonce: un sentinel con nombre "estilo temporal" jamás se borra.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    sentinel = camp / ".campaign_pool_FAD_family.csv.tmp.999999.0.deadbeef"
    sentinel.write_bytes(b"SENTINEL\n")
    assert _run("tools.merge_campaign_pools", tmp_path).returncode == 0
    assert sentinel.exists() and sentinel.read_bytes() == b"SENTINEL\n"


def test_b102_temp_substituted_before_promote_rejected(tmp_path, monkeypatch):
    # sustituir el inode del temporal antes de promover → el binding lo caza; NADA inyectado se publica.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"ORIGINAL\n")
    real_binding = mcp._binding_problem
    done = {"x": False}

    def hooked(dir_fd, name, fd, *, mode):
        if not done["x"] and name and ".tmp." in name:
            done["x"] = True
            try:
                os.unlink(name, dir_fd=dir_fd)
                nfd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dir_fd)
                os.write(nfd, b"INJECTED,DATA\n")
                os.close(nfd)
            except OSError:
                pass
        return real_binding(dir_fd, name, fd, mode=mode)

    monkeypatch.setattr(mcp, "_binding_problem", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, SystemExit)):
        mcp.merge()
    assert b"INJECTED" not in pre.read_bytes(), "publicó contenido inyectado (B102)"


def test_b103_substituted_backup_recovers_from_trusted_bytes(tmp_path, monkeypatch):
    # sustituir el backup por uno FALSIFICADO + forzar fallo de promoción → el rollback NO restaura el falso,
    # recupera desde previous_bytes (copia de confianza) → el output previo vuelve byte-idéntico.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"ORIGINAL\n")
    real_replace = os.replace
    st = {"n": 0}

    def hooked(src, dst, *a, **k):
        if isinstance(src, str) and ".bak." not in src and ".tmp." in src:
            for p in camp.iterdir():
                if ".bak." in p.name and "campaign_pool_FAD_family.csv" in p.name:
                    p.write_text("FORGED-BACKUP\n")
        st["n"] += 1
        if st["n"] == 3:
            raise OSError("promotion failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "replace", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()
    assert pre.read_bytes() == b"ORIGINAL\n", "restauró un backup falsificado (B103)"


def test_b101_recovery_message_points_to_real_file(tmp_path, monkeypatch):
    # cuando el backup desaparece antes de restaurar, el mensaje de recuperación nombra un fichero que EXISTE.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"ORIGINAL\n")
    real_replace = os.replace
    st = {"n": 0}

    def hooked(src, dst, *a, **k):
        if isinstance(src, str) and ".tmp." in src and ".bak." not in src:
            for p in list(camp.iterdir()):
                if ".bak." in p.name and "campaign_pool_FAD_family.csv" in p.name:
                    p.unlink()  # el backup desaparece
        st["n"] += 1
        if st["n"] == 3:
            raise OSError("promotion failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "replace", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError) as exc:
        mcp.merge()
    assert pre.read_bytes() == b"ORIGINAL\n"  # recuperado desde bytes de confianza
    assert "RECUPERACIÓN PRESERVADA" in str(exc.value)


def test_b104_post_commit_fsync_failure_is_typed(tmp_path, monkeypatch):
    # un fallo de fsync DESPUÉS del punto de commit → CommittedStateError (NUNCA un OSError/rollback ambiguo).
    _write_all_8(tmp_path)
    real_fsync = os.fsync
    dir_fsyncs = {"n": 0}

    def counting(fd):
        import stat as _stat

        if _stat.S_ISDIR(os.fstat(fd).st_mode):
            dir_fsyncs["n"] += 1
            if dir_fsyncs["n"] == 3:  # #1,#2 = pre-commit; #3 = post-commit
                raise OSError("postcommit dir fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(mcp.os, "fsync", counting)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError) as exc:
        mcp.merge()
    assert "COMMIT CRUZADO" in str(exc.value)


def test_b104_pre_commit_failure_is_rollback_error(tmp_path, monkeypatch):
    # un fallo ANTES del commit → RollbackError (tipado), no un OSError ambiguo.
    _write_all_8(tmp_path)
    real_replace = os.replace
    st = {"n": 0}

    def flaky(src, dst, *a, **k):
        st["n"] += 1
        if st["n"] == 2:
            raise OSError("pre-commit promote failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "replace", flaky)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()


@pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="sin /dev/fd (no macOS/Linux)")
def test_r9r7_no_fd_leak_across_runs(tmp_path, monkeypatch):
    # Fase 8.6: correr merge repetidamente no debe hacer crecer los descriptores abiertos del proceso.
    _write_all_8(tmp_path)
    monkeypatch.chdir(tmp_path)
    mcp.merge()  # calienta (crea outputs → siguientes corridas hacen backups)
    before = len(os.listdir("/dev/fd"))
    for _ in range(15):
        mcp.merge()
    after = len(os.listdir("/dev/fd"))
    assert after <= before + 1, f"fuga de descriptores: {before} -> {after}"


# ----------------------------- B105-B109: cleanup explícito, recovery total, verificación final, cierre -----------------------------


def test_b105_substituted_backup_after_commit_is_committed_state_error(tmp_path, monkeypatch):
    # sustituir un backup por un objeto ajeno tras el commit → cleanup NO lo borra pero tampoco es verde:
    # CommittedStateError + el residuo ajeno sobrevive (nunca se toca).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")
    real = mcp._Chain.reverify

    def hooked(self, when):
        r = real(self, when)
        if when == "punto de commit":
            for p in list(camp.iterdir()):
                if ".bak." in p.name and "campaign_pool_FAD_family.csv" in p.name:
                    p.unlink()
                    p.write_text("FOREIGN-SENTINEL\n")
        return r

    monkeypatch.setattr(mcp._Chain, "reverify", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError):
        mcp.merge()
    # el objeto ajeno se PRESERVA (movido a cuarentena, jamás destruido) y el residuo se reporta (B105/B117).
    foreign = [p for p in camp.rglob("*") if p.is_file() and p.read_bytes() == b"FOREIGN-SENTINEL\n"]
    assert len(foreign) == 1, "destruyó un objeto ajeno o no reportó el residuo (B105)"


def test_b107_target_mutated_at_commit_point_is_intercepted(tmp_path, monkeypatch):
    # mutar el MISMO inode del target durante reverify("punto de commit") → la re-verificación final (binding+
    # digest) lo intercepta ANTES del commit; el contenido falsificado NO cruza.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real = mcp._Chain.reverify

    def hooked(self, when):
        if when == "punto de commit":
            with open(camp / "campaign_pool_FAD_family.csv", "wb") as f:
                f.write(b"FORGED-AFTER-VERIFY\n")
        return real(self, when)

    monkeypatch.setattr(mcp._Chain, "reverify", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()
    out = camp / "campaign_pool_FAD_family.csv"
    assert not (out.exists() and b"FORGED" in out.read_bytes()), "contenido falsificado cruzó el commit (B107)"


def test_b106_recovery_fsync_failure_does_not_interrupt_rollback(tmp_path, monkeypatch):
    # un fallo dentro de _recover_from_bytes (fsync) NO debe dejar escapar una excepción cruda ni residuos:
    # el rollback global termina y eleva RollbackError.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"ORIGINAL\n")
    real_replace = os.replace
    real_fsync = os.fsync
    st = {"n": 0, "armed": False}

    def bad_replace(src, dst, *a, **k):
        if isinstance(src, str) and ".tmp." in src and ".bak." not in src:
            for p in camp.iterdir():
                if ".bak." in p.name and "campaign_pool_FAD_family.csv" in p.name:
                    p.write_text("FORGED\n")  # falsifica el backup → fuerza la rama de recovery
        st["n"] += 1
        if st["n"] == 3:
            raise OSError("promotion failure")
        return real_replace(src, dst, *a, **k)

    def bad_fsync(fd):
        import stat as _s

        if st["armed"] and _s.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("recovery fsync failure")
        return real_fsync(fd)

    orig = mcp._recover_from_bytes

    def wrapped(o, errs, recs):
        st["armed"] = True
        monkeypatch.setattr(mcp.os, "fsync", bad_fsync)
        try:
            return orig(o, errs, recs)
        finally:
            st["armed"] = False
            monkeypatch.setattr(mcp.os, "fsync", real_fsync)

    monkeypatch.setattr(mcp.os, "replace", bad_replace)
    monkeypatch.setattr(mcp, "_recover_from_bytes", wrapped)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):  # NUNCA un OSError crudo
        mcp.merge()
    assert not [p for p in camp.iterdir() if ".tmp." in p.name], "temporales huérfanos tras recovery fallido (B106)"


def test_b108_governed_reader_catches_mutation_during_read(tmp_path):
    # el snapshot pre/post de read_governed_bytes/_governed_reader caza una mutación in-place durante la lectura.
    d = tmp_path
    f = d / "x.csv"
    f.write_bytes(b"AAAA\n")
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    try:

        def mutating_reader(fd):
            with open(f, "ab") as extra:  # muta el MISMO inode durante la "lectura"
                extra.write(b"B")
            return b"ignored"

        result, err = gr._governed_reader(dfd, "x.csv", mutating_reader)
        assert result is None and err is not None and "mutado" in err
    finally:
        os.close(dfd)


def test_b108_previous_output_mutated_during_read_rejected(tmp_path, monkeypatch):
    # INTEGRACIÓN: mutación in-place del output previo DURANTE la lectura de su LEASE (fd vivo) → el snapshot
    # pre/post del lease difiere → abortar antes de promover (B108/B114/B115).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    target = camp / "campaign_pool_FAD_family.csv"
    target.write_bytes(b"PREVIOUS\n")  # 9 bytes: identifica la lectura del OUTPUT previo (no las mitades aq_pool)
    real_read = os.read
    done = {"x": False}

    def hooked_read(fd, n):
        # el bucle de lectura del lease del previo (mcp.os.read) sobre el inode de 9 bytes: al primer read, crece
        # el MISMO inode → el snapshot fstat post difiere. digest_fd usa gr.os.read (no parcheado) → no interfiere.
        try:
            if not done["x"] and os.fstat(fd).st_size == 9:
                done["x"] = True
                with open(target, "ab") as extra:
                    extra.write(b"MUT\n")
        except OSError:
            pass
        return real_read(fd, n)

    monkeypatch.setattr(mcp.os, "read", hooked_read)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()


def test_b109_close_error_does_not_replace_primary(tmp_path, monkeypatch):
    # un fallo de cierre de fd NO reemplaza el error primario ni convierte un éxito real en verde silencioso.
    # Forzamos éxito (commit) + un cierre que falla → CommittedStateError (post-commit) con la nota de cierre.
    _write_all_8(tmp_path)
    real_close = os.close
    armed = {"x": False}

    def bad_close(fd):
        if armed["x"]:
            armed["x"] = False
            raise OSError("close failure")
        return real_close(fd)

    orig_close_fds = mcp._Out.close_fds

    def hooked_close_fds(self, errs):
        armed["x"] = True  # arma el fallo para el próximo os.close (un temp/backup fd)
        monkeypatch.setattr(mcp.os, "close", bad_close)
        try:
            return orig_close_fds(self, errs)
        finally:
            monkeypatch.setattr(mcp.os, "close", real_close)

    monkeypatch.setattr(mcp._Out, "close_fds", hooked_close_fds)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError) as exc:
        mcp.merge()
    assert "cierre" in str(exc.value)


# ----------------------------- B96: nombre relativo del lector gobernado -----------------------------


def test_b96_read_governed_csv_rejects_absolute(tmp_path):
    (tmp_path / "inside").mkdir()
    outside = tmp_path / "outside.csv"
    pd.DataFrame([{"a": 1}]).to_csv(outside, index=False)
    fd = os.open(str(tmp_path / "inside"), os.O_RDONLY | os.O_DIRECTORY)
    try:
        _, err = gr.read_governed_csv(fd, str(outside))
        assert err is not None and "absolut" in err
    finally:
        os.close(fd)


@pytest.mark.parametrize("name", ["../outside.csv", "sub/x.csv", "", ".", "..", "a\x00b.csv", "/etc/passwd"])
def test_b96_relative_name_problem_rejects(name):
    assert gr.relative_name_problem(name) is not None


def test_b96_relative_name_problem_accepts_plain():
    assert gr.relative_name_problem("global_FAD_camp_auto_s1.csv") is None


# ----------------------------- B97/B98/B99: modelo transaccional explícito -----------------------------


def test_b97_temp_write_failure_leaves_no_orphan(tmp_path, monkeypatch):
    # falla el to_csv del PRIMER temporal: el temporal ya está registrado → el rollback lo borra (sin huérfano).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_to_csv = pd.DataFrame.to_csv
    state = {"n": 0}

    def flaky_to_csv(self, *a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("temp-write-fail")
        return real_to_csv(self, *a, **k)

    monkeypatch.setattr(mcp.pd.DataFrame, "to_csv", flaky_to_csv)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(OSError):
        mcp.merge()
    residue = [q.name for d in (camp, tmp_path / "reports" / "eval") for q in d.iterdir() if q.name.startswith(".")]
    residue = [r for r in residue if r != ".merge.lock"]
    assert residue == [], f"temporal huérfano tras fallo de escritura (B97): {residue}"


def test_b98_failed_restore_recovers_from_trusted_bytes(tmp_path, monkeypatch):
    # promoción falla en la 3ª; en el rollback, la RESTAURACIÓN del output 0 por backup falla → se recupera
    # desde previous_bytes (copia de confianza) → el output previo vuelve byte-idéntico (B98 semántica R9.2R7).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"  # output 0
    pre.write_bytes(b"PRE-PRESERVED\n")
    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(src, dst, *a, **k):
        if ".bak." in str(src) and str(dst) == "campaign_pool_FAD_family.csv":
            raise OSError("restore-from-backup-fail")  # falla SOLO la restauración por backup del output 0
        state["n"] += 1
        if state["n"] == 3:
            raise OSError("promo-fail")  # dispara el rollback tras promover 0 y 1
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "replace", flaky_replace)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError) as exc:
        mcp.merge()
    assert pre.read_bytes() == b"PRE-PRESERVED\n", "no se recuperó el output previo desde bytes de confianza (B98)"
    assert "RECUPERACIÓN PRESERVADA" in str(exc.value)


def test_b99_final_swap_rolls_back(tmp_path, monkeypatch):
    # swap detectado en la reverificación del PUNTO DE COMMIT (backups aún presentes) → rollback completo.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"PRE\n")
    real_reverify = mcp._Chain.reverify

    def hooked(self, when):
        if when == "punto de commit":
            raise mcp._ValidationError("swap final inyectado")  # dominio → atrapado → rollback
        return real_reverify(self, when)

    monkeypatch.setattr(mcp._Chain, "reverify", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()
    assert pre.read_bytes() == b"PRE\n", "el swap final no restauró el output previo (B99)"
    assert not _bak_files(camp), "backups sueltos (no en cuarentena) tras rollback"


@pytest.mark.parametrize("phase", ["prepare_temp", "prepare_backup", "promote", "restore", "cleanup"])
def test_b97_b98_injection_matrix_preserves_external_and_diagnoses(tmp_path, monkeypatch, phase):
    # matriz por fase: en cada punto de fallo, el output preexistente sobrevive (byte-idéntico o recuperable)
    # y no quedan temporales; la ausencia previa sigue ausente.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    ev = tmp_path / "reports" / "eval"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"PRE\n")
    real_replace, real_to_csv = os.replace, pd.DataFrame.to_csv
    st = {"replace": 0, "tocsv": 0}

    def fr(src, dst, *a, **k):
        if phase == "restore" and ".bak." in str(src) and str(dst) == "campaign_pool_FAD_family.csv":
            raise OSError("restore-fail")
        st["replace"] += 1
        if phase == "promote" and st["replace"] == 2:
            raise OSError("promote-fail")
        return real_replace(src, dst, *a, **k)

    def ftc(self, *a, **k):
        st["tocsv"] += 1
        if phase == "prepare_temp" and st["tocsv"] == 1:
            raise OSError("temp-fail")
        return real_to_csv(self, *a, **k)

    monkeypatch.setattr(mcp.os, "replace", fr)
    monkeypatch.setattr(mcp.pd.DataFrame, "to_csv", ftc)
    if phase == "prepare_backup":
        # falla al respaldar: monkeypatch de os.open para reventar en el fd de backup (.bak.)
        real_open = os.open

        def fo(path, *a, **k):
            if isinstance(path, str) and ".bak." in path:
                raise OSError("backup-open-fail")
            return real_open(path, *a, **k)

        monkeypatch.setattr(mcp.os, "open", fo)
    if phase == "restore":
        # fuerza que la restauración se ejerza: promoción falla en la 3ª (0 y 1 quedan promovidos)
        st["_"] = 0

        def fr2(src, dst, *a, **k):
            if ".bak." in str(src) and str(dst) == "campaign_pool_FAD_family.csv":
                raise OSError("restore-fail")
            st["replace"] += 1
            if st["replace"] == 3:
                raise OSError("promo-fail")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(mcp.os, "replace", fr2)
    if phase == "cleanup":
        real_rename = os.rename

        def fu(src, dst, *a, **k):  # la cuarentena del backup (post-commit) falla → CommittedStateError
            if ".bak." in str(src):
                raise PermissionError("cleanup-fail")
            return real_rename(src, dst, *a, **k)

        monkeypatch.setattr(mcp.os, "rename", fu)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.CommittedStateError, SystemExit)):
        mcp.merge()
    # el output preexistente sobrevive byte-idéntico O es recuperable desde un backup preservado
    recoverable = pre.exists() and pre.read_bytes() == b"PRE\n"
    bak_present = any((".bak." in p.name and "campaign_pool_FAD_family.csv" in p.name) for p in camp.iterdir())
    assert recoverable or bak_present, f"[{phase}] output previo ni intacto ni recuperable"
    # ningún temporal residual (los .bak preservados son recuperación legítima)
    temps = [q.name for d in (camp, ev) for q in d.iterdir() if q.name.startswith(".") and ".tmp." in q.name]
    assert temps == [], f"[{phase}] temporales huérfanos: {temps}"


def test_b92_rollback_is_durable_fsyncs_dirs(tmp_path, monkeypatch):
    # B92: el camino de ERROR también hace fsync de campaign Y eval (durabilidad del rollback), no solo éxito.
    _write_all_8(tmp_path)
    real_fsync, real_replace = os.fsync, os.replace
    state = {"n": 0}
    dir_fsyncs = {"n": 0}

    def counting_fsync(fd):
        # un fd de directorio: fstat dice S_ISDIR
        import stat as _stat

        if _stat.S_ISDIR(os.fstat(fd).st_mode):
            dir_fsyncs["n"] += 1
        return real_fsync(fd)

    def flaky_replace(src, dst, *a, **k):
        state["n"] += 1
        if state["n"] == 3:  # falla en la 3ª promoción → dispara el rollback
            raise OSError("inyectado")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(mcp.os, "fsync", counting_fsync)
    monkeypatch.setattr(mcp.os, "replace", flaky_replace)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(OSError):
        mcp.merge()
    assert dir_fsyncs["n"] >= 2, "el rollback no hizo fsync de campaign y eval (B92)"


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


def test_b91_check_deep_refit_rejects_external_campaign_symlink(tmp_path):
    # reports/campaign symlink a un árbol externo con 5 semillas válidas → NO se certifica evidencia externa.
    ext = tmp_path / "external_camp"
    ext.mkdir()
    for s in (1, 2, 3, 4, 5):
        _deep_seed(ext, s, [_row()])
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "campaign").symlink_to(ext)
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b91_check_deep_refit_rejects_reports_symlink(tmp_path):
    ext = tmp_path / "external_reports"
    (ext / "campaign").mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(ext / "campaign", s, [_row()])
    (tmp_path / "reports").symlink_to(ext)
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b91_check_deep_refit_rejects_seed_symlink(tmp_path):
    # una semilla individual como symlink a un CSV externo → rechazada (openat O_NOFOLLOW).
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    outside = tmp_path / "outside_s5.csv"
    pd.DataFrame([_row()]).to_csv(outside, index=False)
    for s in (1, 2, 3, 4):
        _deep_seed(camp, s, [_row()])
    (camp / "global_FAD_camp_auto_s5.csv").symlink_to(outside)
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b95_deep_group_writable_seed_rejected(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row()])
    os.chmod(camp / "global_FAD_camp_auto_s3.csv", 0o666)
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b95_deep_other_writable_seed_rejected(tmp_path):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row()])
    os.chmod(camp / "global_FAD_camp_auto_s3.csv", 0o664)
    assert _run("tools.check_deep_refit", tmp_path).returncode == 1


def test_b93_deep_campaign_swap_during_reads_rejected(tmp_path, monkeypatch):
    # swap de reports/campaign a un árbol externo DESPUÉS de la 1ª lectura: aunque el descriptor original evite
    # leer evidencia externa, la ruta oficial ya no representa la evidencia validada → NO se certifica.
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    ext = tmp_path / "external_camp"
    ext.mkdir()
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row()])
        _deep_seed(ext, s, [_row()])
    real_seed = cdr._seed_keys_at
    state = {"n": 0}

    def hooked(camp_fd, fname):
        r = real_seed(camp_fd, fname)
        state["n"] += 1
        if state["n"] == 1:
            camp.rename(tmp_path / "reports" / "campaign-original")
            os.symlink(str(ext), str(camp))
        return r

    monkeypatch.setattr(cdr, "_seed_keys_at", hooked)
    monkeypatch.chdir(tmp_path)
    assert cdr.main() == 1


def test_b93_deep_reports_swap_during_reads_rejected(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    camp = reports / "campaign"
    camp.mkdir(parents=True)
    ext = tmp_path / "external_reports"
    (ext / "campaign").mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row()])
        _deep_seed(ext / "campaign", s, [_row()])
    real_seed = cdr._seed_keys_at
    state = {"n": 0}

    def hooked(camp_fd, fname):
        r = real_seed(camp_fd, fname)
        state["n"] += 1
        if state["n"] == 2:
            reports.rename(tmp_path / "reports-original")
            os.symlink(str(ext), str(reports))
        return r

    monkeypatch.setattr(cdr, "_seed_keys_at", hooked)
    monkeypatch.chdir(tmp_path)
    assert cdr.main() == 1


def test_b95_deep_seed_mutated_during_read_rejected(tmp_path, monkeypatch):
    camp = tmp_path / "reports" / "campaign"
    camp.mkdir(parents=True)
    for s in (1, 2, 3, 4, 5):
        _deep_seed(camp, s, [_row()])
    target = camp / "global_FAD_camp_auto_s1.csv"
    import tools.governed_read as gr

    real_read_csv = gr.pd.read_csv

    def mutating(fh, *a, **k):
        df = real_read_csv(fh, *a, **k)
        with open(target, "ab") as extra:
            extra.write(b"\n")
        return df

    monkeypatch.setattr(gr.pd, "read_csv", mutating)
    monkeypatch.chdir(tmp_path)
    assert cdr.main() == 1


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


# ----------------------------- B110-B118: estado transaccional, leases, cuarentena, recovery total -----------------------------


def test_b110_post_commit_unexpected_exception_is_committed_state_error(tmp_path, monkeypatch):
    # una excepción INESPERADA (no-OSError) DESPUÉS del commit no puede tragarse: `primary_error` post-commit
    # tipa CommittedStateError, jamás rc=0 (B110). En 16a0967 el merge devolvía 0.
    _write_all_8(tmp_path)
    real_fsync = os.fsync
    armed = {"x": False}
    real_rv = mcp._Chain.reverify

    def rv(self, when):
        r = real_rv(self, when)
        if when == "punto de commit":
            armed["x"] = True
        return r

    def bad_fsync(fd):
        import stat as _s

        if armed["x"] and _s.S_ISDIR(os.fstat(fd).st_mode):
            armed["x"] = False
            raise ValueError("post-commit non-OSError")  # NO-OSError: el except OSError post-commit no la ve
        return real_fsync(fd)

    monkeypatch.setattr(mcp._Chain, "reverify", rv)
    monkeypatch.setattr(mcp.os, "fsync", bad_fsync)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError):
        mcp.merge()


def test_b111_rollback_exception_does_not_interrupt_other_outputs(tmp_path, monkeypatch):
    # una excepción en el rollback de UN output no puede interrumpir la reversión de los demás (B111). En
    # 16a0967 escapaba cruda y dejaba a B sin restaurar; aquí B DEBE volver byte-idéntico y el error se tipa.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    a = camp / "campaign_pool_DFF_employment.csv"  # idx6: restaurado PRIMERO (rollback inverso) → aquí falla
    b = camp / "campaign_pool_FAD_family.csv"  # idx0: restaurado DESPUÉS → prueba que la reversión continuó
    a.write_bytes(b"PRE-A\n")
    b.write_bytes(b"PRE-B\n")
    real_replace = os.replace
    st = {"n": 0}

    def flaky(src, dst, *args, **k):
        if ".bak." in str(src) and str(dst) == "campaign_pool_DFF_employment.csv":
            raise ValueError("rollback-inyectado")  # la restauración de A eleva una excepción inesperada
        if ".tmp." in str(src):
            st["n"] += 1
            if st["n"] == 8:
                raise OSError("promo-fail")  # dispara el rollback tras promover idx0..6 (A y B incluidos)
        return real_replace(src, dst, *args, **k)

    monkeypatch.setattr(mcp.os, "replace", flaky)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):  # tipado, NUNCA un ValueError crudo
        mcp.merge()
    assert b.read_bytes() == b"PRE-B\n", "el rollback global se interrumpió: B no se restauró (B111)"


def test_b112_rollback_cleanup_failure_is_recorded_not_silent(tmp_path, monkeypatch):
    # un fallo poniendo un temporal en cuarentena durante el rollback se REGISTRA (B112). En 16a0967 el
    # resultado de la limpieza se descartaba (rollback_errors vacío).
    _write_all_8(tmp_path)
    real_replace, real_rename, real_unlink = os.replace, os.rename, os.unlink
    st = {"n": 0}

    def flaky_replace(src, dst, *args, **k):
        if ".tmp." in str(src):
            st["n"] += 1
            if st["n"] == 3:
                raise OSError("promo-fail")
        return real_replace(src, dst, *args, **k)

    def flaky_rename(src, dst, *args, **k):  # NEW: la cuarentena del temporal
        if ".tmp." in str(src):
            raise PermissionError("cleanup-fail")
        return real_rename(src, dst, *args, **k)

    def flaky_unlink(name, *args, **k):  # OLD: el unlink del temporal (16a0967)
        if ".tmp." in str(name):
            raise PermissionError("cleanup-fail")
        return real_unlink(name, *args, **k)

    monkeypatch.setattr(mcp.os, "replace", flaky_replace)
    monkeypatch.setattr(mcp.os, "rename", flaky_rename)
    monkeypatch.setattr(mcp.os, "unlink", flaky_unlink)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError) as exc:
        mcp.merge()
    msg = str(exc.value)
    assert "temporal" in msg and "cuarentena" in msg, "el fallo de limpieza del temporal no se reportó (B112)"


def test_b113_chain_close_failure_is_reported(tmp_path, monkeypatch):
    # un fallo cerrando un descriptor de la cadena gobernada se REPORTA (B113). En 16a0967 `_Chain.close()` lo
    # tragaba y el proceso devolvía 0. Aquí un cierre fallido en éxito → CommittedStateError.
    _write_all_8(tmp_path)
    orig_close = mcp._Chain.close
    real_close = os.close

    def hooked(self, *args):
        if not args:  # cadena FRESCA de reverify (sin errs) → cierre normal, sin inyección
            return orig_close(self)
        calls = {"n": 0}

        def bad(fd):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("chain close failure")
            return real_close(fd)

        monkeypatch.setattr(mcp.os, "close", bad)
        try:
            return orig_close(self, *args)
        finally:
            monkeypatch.setattr(mcp.os, "close", real_close)

    monkeypatch.setattr(mcp._Chain, "close", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError) as exc:
        mcp.merge()
    assert "cadena" in str(exc.value) or "cierre" in str(exc.value)


def test_b114_rollback_never_clobbers_concurrent_update(tmp_path, monkeypatch):
    # un output modificado por un tercero DESPUÉS de promoverlo nosotros no debe ser sobrescrito por el rollback
    # con bytes viejos (B114, lost update). En 16a0967 el rollback restauraba el backup encima de la V2.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    tgt = camp / "campaign_pool_FAD_family.csv"  # idx0: promovido primero
    tgt.write_bytes(b"PRE\n")
    real_replace = os.replace
    st = {"n": 0}

    def flaky(src, dst, *args, **k):
        if ".tmp." in str(src):
            st["n"] += 1
            if st["n"] == 8:  # tras promover idx0..6, un tercero actualiza idx0 con contenido MÁS NUEVO
                tmp = camp / ".v2"
                tmp.write_bytes(b"V2-CONCURRENT\n")
                real_replace(str(tmp), str(tgt))
                raise OSError("promo-fail")  # dispara el rollback
        return real_replace(src, dst, *args, **k)

    monkeypatch.setattr(mcp.os, "replace", flaky)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()
    assert tgt.read_bytes() == b"V2-CONCURRENT\n", "el rollback sobrescribió una actualización concurrente (B114)"


def test_b115_input_replaced_after_read_aborts(tmp_path, monkeypatch):
    # una mitad de entrada sustituida (unlink+recreate = inode distinto) tras leerla: el lease (nombre↔inode)
    # lo caza antes de promover (B115). En 16a0967 no había lease → se promovía con el input viejo y rc=0.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_concat = mcp.pd.concat
    done = {"x": False}

    def hooked_concat(*args, **k):
        if not done["x"]:  # tras cargar la 1ª mitad como lease, un tercero la REEMPLAZA por otro inode
            done["x"] = True
            f = camp / "aq_pool_nongbm_FAD_family.csv"
            f.unlink()
            _pool_df("SWAPPED", "FAD", "family", ("ets",)).to_csv(f, index=False)
        return real_concat(*args, **k)

    monkeypatch.setattr(mcp.pd, "concat", hooked_concat)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()
    assert not (camp / "campaign_pool_FAD_family.csv").exists(), "promovió con un input reemplazado (B115)"


def test_b116_lock_unlinked_recreated_after_flock_aborts(tmp_path, monkeypatch):
    # .merge.lock desligado+recreado tras el flock: el lease del lock lo caza (B116). PRUEBA MULTIPROCESO: un
    # 2º proceso PUEDE tomar el flock del nuevo inode → el 1º DEBE abortar antes de publicar. En 16a0967 el
    # lock se validaba solo al adquirirlo → el merge continuaba y devolvía 0.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    lock = camp / ".merge.lock"
    real_concat = mcp.pd.concat
    done = {"x": False}
    holder = {"proc": None}

    def hooked_concat(*args, **k):
        if not done["x"]:
            done["x"] = True
            lock.unlink()  # desliga el inode que el merge tiene flockeado
            lock.write_bytes(b"")  # NUEVO inode del lock
            os.chmod(lock, 0o600)
            holder["proc"] = subprocess.Popen(  # un 2º proceso toma el flock del nuevo inode (vulnerabilidad real)
                [
                    sys.executable,
                    "-c",
                    f"import fcntl,os,time;fd=os.open({str(lock)!r},os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX);time.sleep(2)",
                ]
            )
        return real_concat(*args, **k)

    monkeypatch.setattr(mcp.pd, "concat", hooked_concat)
    monkeypatch.chdir(tmp_path)
    try:
        with pytest.raises(mcp.RollbackError):
            mcp.merge()
        assert not (camp / "campaign_pool_FAD_family.csv").exists(), "publicó bajo un lock robado (B116)"
    finally:
        if holder["proc"] is not None:
            holder["proc"].wait(timeout=30)


def test_b117_toctou_foreign_object_never_destroyed(tmp_path, monkeypatch):
    # objeto ajeno sustituido EXACTAMENTE en la ventana check→unlink jamás se destruye (B117). En 16a0967
    # `_safe_unlink_bound` borraba lo que quedara tras la verificación; la cuarentena no tiene esa ventana.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")  # fuerza un backup a limpiar
    real_binding = mcp._binding_problem
    fired = {"x": False}

    def racing_binding(dir_fd, name, fd, *, mode):
        prob = real_binding(dir_fd, name, fd, mode=mode)
        if prob is None and not fired["x"] and name and ".bak." in name:  # ventana tras la verificación del backup
            fired["x"] = True
            try:
                os.unlink(name, dir_fd=dir_fd)
                nfd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dir_fd)
                os.write(nfd, b"FOREIGN-RACE\n")
                os.close(nfd)
            except OSError:
                pass
        return prob

    monkeypatch.setattr(mcp, "_binding_problem", racing_binding)
    monkeypatch.chdir(tmp_path)
    try:
        mcp.merge()
    except mcp.CommittedStateError, mcp.RollbackError:
        pass
    survivors = [p for p in camp.rglob("*") if p.is_file() and p.read_bytes() == b"FOREIGN-RACE\n"]
    assert survivors, "objeto ajeno destruido en la ventana check→unlink (B117)"


def test_b118_recovery_nonoserror_never_escapes(tmp_path, monkeypatch):
    # una excepción NO-OSError dentro de _recover_from_bytes no puede escapar (B118). En 16a0967 solo se
    # capturaba OSError; un ValueError de la rama de verificación escapaba crudo e interrumpía el rollback.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"ORIGINAL\n")
    real_replace, real_fsync = os.replace, os.fsync
    st = {"n": 0, "armed": False}

    def bad_replace(src, dst, *args, **k):
        if isinstance(src, str) and ".tmp." in src and ".bak." not in src:
            for p in camp.iterdir():
                if ".bak." in p.name and "campaign_pool_FAD_family.csv" in p.name:
                    p.write_text("FORGED\n")  # falsifica el backup → fuerza la rama de recovery
        st["n"] += 1
        if st["n"] == 3:
            raise OSError("promotion failure")
        return real_replace(src, dst, *args, **k)

    def bad_fsync(fd):
        import stat as _s

        if st["armed"] and _s.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("recovery fsync NON-OSError")  # NO-OSError
        return real_fsync(fd)

    orig = mcp._recover_from_bytes

    def wrapped(o, errs, recs):
        st["armed"] = True
        monkeypatch.setattr(mcp.os, "fsync", bad_fsync)
        try:
            return orig(o, errs, recs)
        finally:
            st["armed"] = False
            monkeypatch.setattr(mcp.os, "fsync", real_fsync)

    monkeypatch.setattr(mcp.os, "replace", bad_replace)
    monkeypatch.setattr(mcp, "_recover_from_bytes", wrapped)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):  # NUNCA un ValueError crudo
        mcp.merge()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
