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
    # R9.2R10: cada objeto en cuarentena DEBE tener un registro INTENT y uno COMPLETED/FOREIGN_PRESERVED (B126).
    import hashlib as _hashlib
    import json as _json

    for qroot in base.rglob(mcp._QUARANTINE_DIR):
        for txid in qroot.iterdir():
            if not txid.is_dir():
                continue
            manifest = txid / "MANIFEST.jsonl"
            intents, completed = set(), set()
            prev = ""
            if manifest.exists():
                assert (manifest.stat().st_mode & 0o777) == 0o600, "manifiesto de cuarentena no es 0600"
                for i, line in enumerate(manifest.read_text().splitlines(), start=1):
                    rec = _json.loads(line)
                    # cadena de hashes íntegra (secuencia 1..N, previous/record sha encadenados) — B136
                    assert rec["sequence"] == i and rec["previous_record_sha256"] == prev, "cadena de journal rota"
                    body = {k: v for k, v in rec.items() if k != "record_sha256"}
                    assert _hashlib.sha256(mcp._canon(body)).hexdigest() == rec["record_sha256"], (
                        "hash de journal inválido"
                    )
                    prev = rec["record_sha256"]
                    if rec["record"] == "MOVE_INTENT":
                        intents.add(rec["destination_name"])
                    elif rec["record"] in ("MOVE_COMPLETED", "MOVE_FOREIGN_PRESERVED"):
                        completed.add(rec["destination_name"])
            for item in txid.iterdir():
                if item.name != "MANIFEST.jsonl":
                    assert item.name in intents, f"objeto en cuarentena sin MOVE_INTENT: {item}"
                    assert item.name in completed, f"objeto en cuarentena sin MOVE_COMPLETED/FOREIGN: {item}"


def _fail_nth_cas(monkeypatch, n, exc=None):
    """R9.2R10: falla la N-ésima promoción CAS (rename_exchange/noreplace) → dispara el rollback. Cuenta AMBAS
    primitivas (una promoción usa exchange si el output existía, noreplace si estaba ausente); N<=8 apunta a una
    promoción (los movimientos de cuarentena del rollback son llamadas posteriores)."""
    real_ex, real_nr = mcp.rename_exchange, mcp.rename_noreplace
    exc = exc or OSError("promo-fail")
    st = {"n": 0}

    def ex(*a, **k):
        st["n"] += 1
        if st["n"] == n:
            raise exc
        return real_ex(*a, **k)

    def nr(*a, **k):
        st["n"] += 1
        if st["n"] == n:
            raise exc
        return real_nr(*a, **k)

    monkeypatch.setattr(mcp, "rename_exchange", ex)
    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    return st


def _force_recovery(monkeypatch, tmp_path):
    """R9.2R10: fuerza la rama de recuperación desde previous_bytes para campaign_pool_FAD_family (idx0). Éste
    es preexistente → se promueve por exchange (su original queda desplazado en temp_name); una promoción
    posterior falla → rollback; al iniciar el rollback se ELIMINA el original desplazado → `_cas_restore` cae a
    `_recover_from_bytes`, que instala los bytes de confianza. Requiere `pre.write_bytes(...)` en idx0."""
    camp = tmp_path / "reports" / "campaign"
    real_ex, real_nr = mcp.rename_exchange, mcp.rename_noreplace
    st = {"n": 0, "killed": False}

    def _kill_displaced():
        st["killed"] = True
        for p in list(camp.iterdir()):
            if p.name.startswith(".campaign_pool_FAD_family.csv.tmp."):
                p.unlink()

    def ex(*a, **k):
        st["n"] += 1
        return real_ex(*a, **k)

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        st["n"] += 1
        if st["n"] == 4:  # 4ª promoción (una noreplace de un output ausente) → falla → rollback
            raise OSError("promo-fail")
        if src_dir_fd != dst_dir_fd and not st["killed"]:  # 1er move de cuarentena del rollback
            _kill_displaced()
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_exchange", ex)
    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    return st


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
    _fail_nth_cas(monkeypatch, fail_at)  # falla la fail_at-ésima promoción CAS → rollback limpio
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):  # rollback COMPLETO (sin concurrencia) → tipado seguro
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


def test_b94_original_cleanup_failure_after_success_not_ok(tmp_path, monkeypatch):
    # fallo al poner el ORIGINAL desplazado en CUARENTENA tras el COMMIT → CommittedStateError (B104/B112/B117:
    # post-commit tipado; los outputs nuevos ya son la autoridad, una limpieza fallida NO es verde).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")  # fuerza un original desplazado a limpiar
    real_nr = mcp.rename_noreplace

    def flaky_nr(src_dir_fd, src, dst_dir_fd, dst):
        if src_dir_fd != dst_dir_fd:  # movimiento a cuarentena (cross-dir); la promoción es same-dir
            raise PermissionError("inyectado")
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", flaky_nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError):
        mcp.merge()


def test_b94_rollback_cleanup_failure_is_incomplete(tmp_path, monkeypatch):
    # fallo de limpieza (cuarentena del temporal) DURANTE el rollback → RollbackIncompleteError (B112/B127: no
    # silenciado y NO reintentable automáticamente).
    _write_all_8(tmp_path)
    real_ex, real_nr = mcp.rename_exchange, mcp.rename_noreplace
    st = {"n": 0}

    def ex(*a, **k):
        st["n"] += 1
        if st["n"] == 3:
            raise OSError("promo-inyectado")
        return real_ex(*a, **k)

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        st["n"] += 1
        if st["n"] == 3:
            raise OSError("promo-inyectado")
        if src_dir_fd != dst_dir_fd:  # cuarentena del temporal durante el rollback
            raise PermissionError("cleanup-inyectado")
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_exchange", ex)
    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError) as exc:
        mcp.merge()
    assert "ROLLBACK INCOMPLETO" in str(exc.value)


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
        assert res.status == mcp._FOREIGN_OBJECT_PRESERVED
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


def test_b103_lost_displaced_original_recovers_from_trusted_bytes(tmp_path, monkeypatch):
    # si el original desplazado (temp_name) desaparece antes de restaurar, el rollback NO deja el output en un
    # estado arbitrario: recupera desde previous_bytes (copia de confianza) → el output previo vuelve byte-idéntico.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"ORIGINAL\n")
    _force_recovery(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()
    assert pre.read_bytes() == b"ORIGINAL\n", "no recuperó los bytes de confianza (B103)"


def test_b101_recovery_message_points_to_real_file(tmp_path, monkeypatch):
    # el mensaje de recuperación nombra un fichero que EXISTE (el target con los bytes de confianza instalados).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"ORIGINAL\n")
    _force_recovery(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError) as exc:
        mcp.merge()
    assert pre.read_bytes() == b"ORIGINAL\n"  # recuperado desde bytes de confianza
    assert "RECUPERACIÓN" in str(exc.value)
    assert pre.exists(), "el mensaje de recuperación nombra un fichero que debe existir"


def test_b104_post_commit_fsync_failure_is_typed(tmp_path, monkeypatch):
    # Incremento 2: un fallo de fsync DESPUÉS de que el CommitCertificate de CURRENT cruzó → CommittedStateError
    # (NUNCA ambiguo). Se ARMA el fallo cuando `_publish_bundle` retorna el certificado; el 1er fsync de directorio
    # posterior (durabilidad post-commit del CLEANING) falla.
    _write_all_8(tmp_path)
    real_fsync = os.fsync
    armed = {"x": False}

    def counting(fd):
        import stat as _stat

        if armed["x"] and _stat.S_ISDIR(os.fstat(fd).st_mode):
            armed["x"] = False
            raise OSError("postcommit dir fsync failure")
        return real_fsync(fd)

    orig = mcp._publish_bundle

    def wrapped(*a):
        cert = orig(*a)  # CURRENT ya cruzó y se certificó
        armed["x"] = True  # commit cruzado → el próximo fsync de dir (durabilidad post-commit) falla
        return cert

    monkeypatch.setattr(mcp.os, "fsync", counting)
    monkeypatch.setattr(mcp, "_publish_bundle", wrapped)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError) as exc:
        mcp.merge()
    assert "COMMIT CRUZADO" in str(exc.value)


def test_b104_pre_commit_failure_is_rollback_error(tmp_path, monkeypatch):
    # un fallo ANTES del commit con rollback LIMPIO → RollbackError (tipado), no un OSError ambiguo.
    _write_all_8(tmp_path)
    _fail_nth_cas(monkeypatch, 2)
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


def test_b105_substituted_displaced_original_after_commit_is_committed_state_error(tmp_path, monkeypatch):
    # sustituir el ORIGINAL desplazado (temp_name) por un objeto ajeno tras el commit → la limpieza NO lo
    # borra pero tampoco es verde: CommittedStateError + el ajeno sobrevive en cuarentena (B105/B117).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")  # preexistente → su original se desplaza a temp_name
    real = mcp._Chain.reverify

    def hooked(self, when):
        r = real(self, when)
        if when == "certificación":  # tras promover, el original vive en temp_name; sustitúyelo por un ajeno
            for p in list(camp.iterdir()):
                if p.name.startswith(".campaign_pool_FAD_family.csv.tmp."):
                    p.unlink()
                    p.write_text("FOREIGN-SENTINEL\n")
        return r

    monkeypatch.setattr(mcp._Chain, "reverify", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError):
        mcp.merge()
    foreign = [p for p in camp.rglob("*") if p.is_file() and p.read_bytes() == b"FOREIGN-SENTINEL\n"]
    assert len(foreign) == 1, "destruyó un objeto ajeno o no reportó el residuo (B105)"


def test_b107_target_mutated_at_commit_point_is_intercepted(tmp_path, monkeypatch):
    # mutar el MISMO inode del target durante la certificación → la re-verificación final (binding+digest)
    # lo intercepta ANTES del commit; el contenido falsificado NO cruza.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real = mcp._Chain.reverify

    def hooked(self, when):
        if when == "certificación":
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
    # un fallo (fsync) DENTRO de _recover_from_bytes no deja escapar una excepción cruda: se contiene y el
    # resultado se tipa (RollbackIncompleteError, B106/B127) — jamás un OSError crudo.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"ORIGINAL\n")
    _force_recovery(monkeypatch, tmp_path)
    real_fsync = os.fsync
    armed = {"x": False}

    def bad_fsync(fd):
        import stat as _s

        if armed["x"] and _s.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("recovery fsync failure")
        return real_fsync(fd)

    orig = mcp._recover_from_bytes

    def wrapped(o, ctx):
        armed["x"] = True
        try:
            return orig(o, ctx)
        finally:
            armed["x"] = False

    monkeypatch.setattr(mcp.os, "fsync", bad_fsync)
    monkeypatch.setattr(mcp, "_recover_from_bytes", wrapped)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):  # NUNCA un OSError crudo
        mcp.merge()


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
    # cuando el original desplazado no está disponible para restaurar, el rollback recupera desde previous_bytes
    # (copia de confianza) → el output previo vuelve byte-idéntico (B98 semántica R9.2R7, ahora vía CAS).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"  # output 0
    pre.write_bytes(b"PRE-PRESERVED\n")
    _force_recovery(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError) as exc:
        mcp.merge()
    assert pre.read_bytes() == b"PRE-PRESERVED\n", "no se recuperó el output previo desde bytes de confianza (B98)"
    assert "RECUPERACIÓN" in str(exc.value)


def test_b99_final_swap_rolls_back(tmp_path, monkeypatch):
    # swap detectado en la reverificación del PUNTO DE COMMIT (backups aún presentes) → rollback completo.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"PRE\n")
    real_reverify = mcp._Chain.reverify

    def hooked(self, when):
        if when == "certificación":
            raise mcp._ValidationError("swap final inyectado")  # dominio → atrapado → rollback
        return real_reverify(self, when)

    monkeypatch.setattr(mcp._Chain, "reverify", hooked)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
        mcp.merge()
    assert pre.read_bytes() == b"PRE\n", "el swap final no restauró el output previo (B99)"
    assert not _bak_files(camp), "backups sueltos (no en cuarentena) tras rollback"


@pytest.mark.parametrize("phase", ["prepare_temp", "promote", "restore", "cleanup"])
def test_b97_b98_injection_matrix_preserves_external_and_diagnoses(tmp_path, monkeypatch, phase):
    # matriz por fase (CAS): en cada punto de fallo el output preexistente se preserva/recupera y no quedan
    # temporales sueltos; un fallo post-commit tipa CommittedStateError.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    ev = tmp_path / "reports" / "eval"
    pre = camp / "campaign_pool_FAD_family.csv"
    pre.write_bytes(b"PRE\n")
    if phase == "prepare_temp":
        real_to_csv = pd.DataFrame.to_csv
        st = {"n": 0}

        def ftc(self, *a, **k):
            st["n"] += 1
            if st["n"] == 1:
                raise OSError("temp-fail")
            return real_to_csv(self, *a, **k)

        monkeypatch.setattr(mcp.pd.DataFrame, "to_csv", ftc)
        expect = (mcp.RollbackError, mcp.RollbackIncompleteError)
    elif phase == "promote":
        _fail_nth_cas(monkeypatch, 3)  # 3ª promoción CAS falla
        expect = (mcp.RollbackError, mcp.RollbackIncompleteError)
    elif phase == "restore":
        _force_recovery(monkeypatch, tmp_path)  # rollback cae a la recuperación desde bytes de confianza
        expect = (mcp.RollbackError, mcp.RollbackIncompleteError)
    else:  # cleanup: la cuarentena del original desplazado (post-commit) falla
        real_nr = mcp.rename_noreplace

        def nr(src_dir_fd, src, dst_dir_fd, dst):
            if src_dir_fd != dst_dir_fd:
                raise PermissionError("cleanup-fail")
            return real_nr(src_dir_fd, src, dst_dir_fd, dst)

        monkeypatch.setattr(mcp, "rename_noreplace", nr)
        expect = (mcp.CommittedStateError,)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(expect):
        mcp.merge()
    if phase == "cleanup":
        assert pre.exists()  # el commit cruzó → pre tiene el contenido nuevo (autoridad durable)
    else:
        assert pre.read_bytes() == b"PRE\n", f"[{phase}] output previo no preservado/recuperado"
    if phase in ("prepare_temp", "promote"):  # rutas sin recuperación → cero residuo suelto
        assert _loose_residue(camp, ev) == [], f"[{phase}] residuo suelto"
    _assert_quarantine_manifested(tmp_path)


def test_b92_rollback_is_durable_fsyncs_dirs(tmp_path, monkeypatch):
    # B92: el camino de ERROR también hace fsync de campaign Y eval (durabilidad del rollback), no solo éxito.
    _write_all_8(tmp_path)
    real_fsync = os.fsync
    dir_fsyncs = {"n": 0}

    def counting_fsync(fd):
        import stat as _stat

        if _stat.S_ISDIR(os.fstat(fd).st_mode):
            dir_fsyncs["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(mcp.os, "fsync", counting_fsync)
    _fail_nth_cas(monkeypatch, 3)  # falla la 3ª promoción → dispara el rollback
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError):
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

    def bad_fsync(fd):
        import stat as _s

        if armed["x"] and _s.S_ISDIR(os.fstat(fd).st_mode):
            armed["x"] = False
            raise ValueError("post-commit non-OSError")  # NO-OSError: el except OSError post-commit no la ve
        return real_fsync(fd)

    orig = mcp._publish_bundle

    def wrapped(*a):
        cert = orig(*a)  # Incremento 2: CURRENT cruzó y se certificó
        armed["x"] = True  # commit cruzado → el próximo fsync de dir post-commit eleva un NO-OSError
        return cert

    monkeypatch.setattr(mcp.os, "fsync", bad_fsync)
    monkeypatch.setattr(mcp, "_publish_bundle", wrapped)
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
    real_ex, real_nr = mcp.rename_exchange, mcp.rename_noreplace
    st = {"rolling": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "model_comparison_EB_DFF21.csv":  # última promoción falla → rollback (A y B ya promovidos)
            st["rolling"] = True
            raise OSError("promo-fail")
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    def ex(src_dir_fd, src, dst_dir_fd, dst):
        if st["rolling"] and dst == "campaign_pool_DFF_employment.csv":
            raise ValueError("rollback-inyectado")  # la restauración CAS de A eleva una excepción inesperada
        return real_ex(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.setattr(mcp, "rename_exchange", ex)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):  # tipado, NUNCA un ValueError crudo
        mcp.merge()
    assert b.read_bytes() == b"PRE-B\n", "el rollback global se interrumpió: B no se restauró (B111)"


def test_b112_rollback_cleanup_failure_is_recorded_not_silent(tmp_path, monkeypatch):
    # un fallo poniendo un temporal en cuarentena durante el rollback se REGISTRA (B112). En 16a0967 el
    # resultado de la limpieza se descartaba (rollback_errors vacío).
    _write_all_8(tmp_path)
    real_ex, real_nr = mcp.rename_exchange, mcp.rename_noreplace
    st = {"n": 0}

    def ex(*args, **k):
        st["n"] += 1
        if st["n"] == 3:
            raise OSError("promo-fail")
        return real_ex(*args, **k)

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        st["n"] += 1
        if st["n"] == 3:
            raise OSError("promo-fail")
        if src_dir_fd != dst_dir_fd:  # la cuarentena del temporal durante el rollback (cross-dir) falla
            raise PermissionError("cleanup-fail")
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_exchange", ex)
    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError) as exc:
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
    tgt = camp / "campaign_pool_FAD_family.csv"  # idx0 preexistente: promovido por exchange
    tgt.write_bytes(b"PRE\n")
    real_nr = mcp.rename_noreplace

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "model_comparison_EB_DFF21.csv":  # última promoción → idx0 (exchange) ya promovido
            v2 = camp / ".v2"
            v2.write_bytes(b"V2-CONCURRENT\n")
            os.replace(str(v2), str(tgt))  # actualización concurrente ENTRE promover y el rollback
            raise OSError("promo-fail")  # dispara el rollback
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    assert tgt.read_bytes() == b"V2-CONCURRENT\n", "el rollback sobrescribió una actualización concurrente (B114/B123)"


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


def test_b117_quarantine_move_never_destroys_foreign_integration(tmp_path, monkeypatch):
    # INTEGRACIÓN: la cuarentena usa rename_noreplace + verificación de binding — un ajeno sustituido por el
    # original desplazado JAMÁS se destruye: se mueve y se preserva (B117, sin ventana check→unlink).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")  # preexistente → original desplazado a temp_name
    real = mcp._Chain.reverify

    def hooked(self, when):
        r = real(self, when)
        if when == "certificación":  # sustituye el original desplazado por un AJENO justo antes del commit
            for p in list(camp.iterdir()):
                if p.name.startswith(".campaign_pool_FAD_family.csv.tmp."):
                    p.unlink()
                    p.write_text("FOREIGN-RACE\n")
        return r

    monkeypatch.setattr(mcp._Chain, "reverify", hooked)
    monkeypatch.chdir(tmp_path)
    try:
        mcp.merge()
    except mcp.CommittedStateError, mcp.RollbackError, mcp.RollbackIncompleteError:
        pass
    survivors = [p for p in camp.rglob("*") if p.is_file() and p.read_bytes() == b"FOREIGN-RACE\n"]
    assert survivors, "objeto ajeno destruido por la cuarentena (B117)"


def test_b118_recovery_nonoserror_never_escapes(tmp_path, monkeypatch):
    # una excepción NO-OSError dentro de _recover_from_bytes no puede escapar (B118). En 16a0967 solo se
    # capturaba OSError; un ValueError de la rama de verificación escapaba crudo e interrumpía el rollback.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"ORIGINAL\n")
    _force_recovery(monkeypatch, tmp_path)
    real_fsync = os.fsync
    armed = {"x": False}

    def bad_fsync(fd):
        import stat as _s

        if armed["x"] and _s.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("recovery fsync NON-OSError")  # NO-OSError: escaparía crudo sin el guard except Exception
        return real_fsync(fd)

    orig = mcp._recover_from_bytes

    def wrapped(o, ctx):
        armed["x"] = True
        try:
            return orig(o, ctx)
        finally:
            armed["x"] = False

    monkeypatch.setattr(mcp.os, "fsync", bad_fsync)
    monkeypatch.setattr(mcp, "_recover_from_bytes", wrapped)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):  # NUNCA un ValueError crudo
        mcp.merge()


# ----------------------------- B119-B127: CAS atómico + journal durable + semántica de errores -----------------------------


def test_b119_manifest_write_failure_is_not_silent(tmp_path, monkeypatch):
    # si el journal del manifiesto falla, el objeto NO entra en cuarentena en silencio con rc=0: el movimiento
    # se degrada a QUARANTINE_FAILED → post-commit tipa CommittedStateError (B119).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")  # fuerza un original desplazado a limpiar
    real_write_all = mcp._write_all

    def bad_write_all(fd, data):
        if b"INTENT" in data or b"COMPLETED" in data:  # registros del manifiesto de cuarentena
            raise OSError("manifest write failure")
        return real_write_all(fd, data)

    monkeypatch.setattr(mcp, "_write_all", bad_write_all)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError):
        mcp.merge()


def test_b120_manifest_hardlink_race_rejected(tmp_path, monkeypatch):
    # B120/B138: un hardlink a un fichero externo plantado como MANIFEST.jsonl JUSTO tras crear el txid y ANTES
    # de abrirlo (la carrera real) es rechazado por O_EXCL — el fichero externo NO se modifica.
    d = tmp_path / "reports" / "campaign"
    d.mkdir(parents=True)
    external = tmp_path / "external.txt"
    external.write_bytes(b"EXTERNAL-UNTOUCHED\n")
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    quar = mcp._Quarantine("planted.deadbeef")
    real_mkdir = os.mkdir

    def racing_mkdir(path, *a, dir_fd=None, **k):
        r = real_mkdir(path, *a, dir_fd=dir_fd, **k)
        if path == quar.txid:  # el txid acaba de crearse → un tercero planta el MANIFEST como hardlink al externo
            os.link(str(external), str(d / ".merge-quarantine" / quar.txid / "MANIFEST.jsonl"))
        return r

    monkeypatch.setattr(mcp.os, "mkdir", racing_mkdir)
    monkeypatch.chdir(tmp_path)
    try:
        with pytest.raises((OSError, mcp._ValidationError)):
            quar._prepare(dfd, "campaign")  # el O_EXCL del MANIFEST cae sobre el hardlink plantado → rechazado
        assert external.read_bytes() == b"EXTERNAL-UNTOUCHED\n", "escribió fuera de la cuarentena (B120)"
    finally:
        quar.close([])
        os.close(dfd)


def test_b121_quarantine_collision_preserves_existing(tmp_path, monkeypatch):
    # B121: la cuarentena usa rename_noreplace — si el destino de cuarentena ya existe (colisión forzada), el
    # objeto que estaba allí NO se destruye; el movimiento se degrada a QUARANTINE_FAILED.
    d = tmp_path / "reports" / "campaign"
    d.mkdir(parents=True)
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    quar = mcp._Quarantine("coll.deadbeef")
    real_token = mcp.secrets.token_hex
    monkeypatch.setattr(mcp.secrets, "token_hex", lambda n=6: "FIXED")  # qname determinista → forzar colisión
    try:
        name, fd = mcp._create_governed(dfd, "y", "tmp", 0)
        os.write(fd, b"OURS\n")
        o = mcp._Out(dfd, "campaign", "y", None)
        qtx = quar._prepare(dfd, "campaign").qtx
        # planta el destino de cuarentena que `move` intentará usar (label.name.FIXED)
        collided = f"{o.label}.{name.lstrip('.')}.FIXED"
        cfd = os.open(collided, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=qtx)
        os.write(cfd, b"ALREADY-THERE\n")
        os.close(cfd)
        res = quar.move(o, name, fd, phase="TEST", reason="collision")
        assert res.status == mcp._QUARANTINE_FAILED, "no degradó ante la colisión de cuarentena (B121)"
        got = os.open(collided, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=qtx)
        assert os.read(got, 64) == b"ALREADY-THERE\n", "rename_noreplace destruyó el objeto en el destino (B121)"
        os.close(got)
        os.close(fd)
    finally:
        monkeypatch.setattr(mcp.secrets, "token_hex", real_token)
        quar.close([])
        os.close(dfd)


def test_b122_promote_never_destroys_concurrent_creation(tmp_path, monkeypatch):
    # una creación concurrente de un output AUSENTE entre validar y promover no se sobrescribe: rename_noreplace
    # da FileExistsError → abort, el contenido concurrente SOBREVIVE intacto (B122).
    _write_all_8(tmp_path)
    ev = tmp_path / "reports" / "eval"
    real_nr = mcp.rename_noreplace
    done = {"x": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if not done["x"]:  # tras validar la ausencia, un tercero CREA el último output antes de su promoción
            done["x"] = True
            (ev / "model_comparison_EB_DFF21.csv").write_bytes(b"CONCURRENT-CREATE\n")
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    assert (ev / "model_comparison_EB_DFF21.csv").read_bytes() == b"CONCURRENT-CREATE\n", (
        "destruyó una creación concurrente (B122)"
    )


def test_b124_recovery_never_clobbers_concurrent_update(tmp_path, monkeypatch):
    # durante la recuperación desde bytes, una actualización concurrente en el target NO se sobrescribe: se
    # preserva y el rollback se marca incompleto (B124).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    tgt = camp / "campaign_pool_FAD_family.csv"
    tgt.write_bytes(b"ORIGINAL\n")
    real_ex, real_nr = mcp.rename_exchange, mcp.rename_noreplace
    st = {"killed": False}

    def _kill_and_v2():
        st["killed"] = True
        for p in list(camp.iterdir()):  # elimina el original desplazado → fuerza recovery
            if p.name.startswith(".campaign_pool_FAD_family.csv.tmp."):
                p.unlink()
        v2 = camp / ".v2"  # y coloca una actualización concurrente en el target
        v2.write_bytes(b"V2-CONCURRENT\n")
        os.replace(str(v2), str(tgt))

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "model_comparison_EB_DFF21.csv":  # última promoción → idx0 (exchange) ya promovido
            raise OSError("promo-fail")
        if src_dir_fd != dst_dir_fd and not st["killed"]:  # 1er move de cuarentena del rollback
            _kill_and_v2()
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.setattr(mcp, "rename_exchange", real_ex)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    assert tgt.read_bytes() == b"V2-CONCURRENT\n", "la recuperación sobrescribió una actualización concurrente (B124)"


def test_b125_absent_rollback_preserves_concurrent_on_official_path(tmp_path, monkeypatch):
    # para un output AUSENTE, si un tercero lo reemplaza durante el rollback, la actualización concurrente NO
    # queda huérfana en cuarentena: se devuelve a su ruta oficial (B125).
    _write_all_8(tmp_path)
    ev = tmp_path / "reports" / "eval"
    absent_tgt = ev / "model_comparison_DFF21.csv"  # idx5 (ausente), se revierte antes que idx0
    real_nr = mcp.rename_noreplace
    st = {"done": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "model_comparison_EB_DFF21.csv":  # última promoción falla → rollback
            raise OSError("promo-fail")
        if src_dir_fd != dst_dir_fd and not st["done"] and absent_tgt.exists():
            st["done"] = True  # justo antes de poner en cuarentena, un tercero reemplaza el output ausente
            v2 = ev / ".v2"
            v2.write_bytes(b"V2-ABSENT-CONCURRENT\n")
            os.replace(str(v2), str(absent_tgt))
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError):  # B139: divergencia externa ⇒ no reintentable
        mcp.merge()
    # B137: la concurrente DEBE volver a su RUTA OFICIAL, no sobrevivir "en cualquier lugar" (cuarentena huérfana).
    assert absent_tgt.exists() and absent_tgt.read_bytes() == b"V2-ABSENT-CONCURRENT\n", (
        "la actualización concurrente no volvió a la ruta oficial (B125/B137)"
    )
    q_orphans = [
        p
        for p in ev.rglob("*")
        if mcp._QUARANTINE_DIR in p.parts and p.is_file() and p.read_bytes() == b"V2-ABSENT-CONCURRENT\n"
    ]
    assert not q_orphans, "la concurrente quedó huérfana en cuarentena en vez de volver a la ruta oficial (B137)"


def test_b126_quarantine_journal_has_intent_and_completed(tmp_path, monkeypatch):
    # cada objeto en cuarentena tiene MOVE_INTENT (antes del move) y MOVE_COMPLETED/FOREIGN (después), con cadena
    # de hashes válida y fsync (B126). Un merge con preexistentes desplaza originales → cuarentena inventariada.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")
    real_fsync = os.fsync
    fsyncs = {"n": 0}

    def counting(fd):
        fsyncs["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(mcp.os, "fsync", counting)
    monkeypatch.chdir(tmp_path)
    assert mcp.merge() == 0
    import json as _json

    manifest = next(camp.rglob("MANIFEST.jsonl"))
    records = [_json.loads(line) for line in manifest.read_text().splitlines()]
    intents = [r for r in records if r["record"] == "MOVE_INTENT"]
    completed = [r for r in records if r["record"] in ("MOVE_COMPLETED", "MOVE_FOREIGN_PRESERVED")]
    assert intents and completed and len(intents) == len(completed), (
        "el journal no tiene MOVE_INTENT+COMPLETED pareados (B126)"
    )
    assert records[0]["record"] == "MOVE_INTENT", "el primer evento de un objeto debe ser INTENT"
    assert fsyncs["n"] > 0
    _assert_quarantine_manifested(tmp_path)  # valida la cadena de hashes íntegra


def test_b127_error_taxonomy_is_distinct(tmp_path, monkeypatch):
    # RollbackError (reintento seguro) y RollbackIncompleteError (no reintentar) son CLASES DISTINTAS (B127);
    # un rollback limpio → RollbackError; uno con actualización concurrente → RollbackIncompleteError.
    assert not issubclass(mcp.RollbackIncompleteError, mcp.RollbackError)
    assert not issubclass(mcp.RollbackError, mcp.RollbackIncompleteError)
    # rollback limpio (fallo de promoción sin concurrencia) → RollbackError
    _write_all_8(tmp_path)
    _fail_nth_cas(monkeypatch, 3)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackError) as exc:
        mcp.merge()
    assert not isinstance(exc.value, mcp.RollbackIncompleteError), "un rollback limpio no debe ser INCOMPLETO (B127)"


def test_no_raw_fs_mutations_in_merge_module():
    # GATE ESTÁTICO (R9.2R10 §7): merge_campaign_pools NO puede usar os.replace/os.rename/os.unlink (mutaciones
    # no-CAS), el MANIFEST debe abrirse O_EXCL, y no puede haber `except: pass` en journal/CAS/rollback/cuarentena.
    import ast
    import pathlib

    src = pathlib.Path(mcp.__file__).read_text()
    tree = ast.parse(src)
    banned = {("os", "replace"), ("os", "rename"), ("os", "unlink")}
    bad_calls = [
        f"{n.func.value.id}.{n.func.attr}:{n.lineno}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and (n.func.value.id, n.func.attr) in banned
    ]
    assert not bad_calls, f"mutación de FS cruda (usar tools.atomic_fs): {bad_calls}"
    assert "os.O_EXCL" in src and "_MANIFEST_NAME" in src, "el MANIFEST debe abrirse O_EXCL"
    assert "rename_exchange" in src and "rename_noreplace" in src, "debe usar las primitivas atómicas gobernadas"

    # `except: pass` está PROHIBIDO cuando swallowea un error AMPLIO/inesperado (OSError/Exception/BaseException/
    # bare) en journal/CAS/rollback/cuarentena; un `pass` sobre una excepción ESPECÍFICA de control de flujo
    # (FileExistsError para tolerar EEXIST, FileNotFoundError para "sigue ausente") sí es legítimo.
    def _is_broad(handler):
        exc = handler.type
        if exc is None:  # bare except
            return True
        names = [n.id for n in ast.walk(exc) if isinstance(n, ast.Name)]
        return any(nm in ("OSError", "Exception", "BaseException") for nm in names)

    allowed = {  # limpieza de fds en un constructor fallido (antes de re-elevar) es un patrón seguro reconocido
        h.lineno
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name == "__init__"
        for h in ast.walk(fn)
        if isinstance(h, ast.ExceptHandler)
    }
    bad_pass = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.ExceptHandler)
        and len(n.body) == 1
        and isinstance(n.body[0], ast.Pass)
        and _is_broad(n)
        and n.lineno not in allowed
    ]
    assert not bad_pass, f"except (amplio): pass en journal/CAS/rollback/cuarentena (B119): líneas {bad_pass}"


# ----------------------------- B128-B139: journal bidireccional, compensación verificable, recibo de commit -----------------------------


def test_b128_restore_is_journaled(tmp_path, monkeypatch):
    # `restore()` (devolver una concurrente a su ruta oficial) escribe RESTORE_INTENT/COMPLETED en el journal
    # (B128); en R9.2R10 no escribía nada y devolvía éxito sin evidencia durable.
    _write_all_8(tmp_path)
    ev = tmp_path / "reports" / "eval"
    absent_tgt = ev / "model_comparison_DFF21.csv"
    real_nr = mcp.rename_noreplace
    st = {"done": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "model_comparison_EB_DFF21.csv":
            raise OSError("promo-fail")
        if src_dir_fd != dst_dir_fd and not st["done"] and absent_tgt.exists():
            st["done"] = True
            v2 = ev / ".v2"
            v2.write_bytes(b"V2\n")
            os.replace(str(v2), str(absent_tgt))
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError):
        mcp.merge()
    import json as _json

    records = []
    for m in ev.rglob("MANIFEST.jsonl"):
        records += [_json.loads(line)["record"] for line in m.read_text().splitlines()]
    assert "RESTORE_INTENT" in records and "RESTORE_COMPLETED" in records, "restore no dejó evidencia durable (B128)"


def test_b129_failed_compensation_is_incomplete(tmp_path, monkeypatch):
    # si la compensación (swap de vuelta) tras detectar concurrencia EN LA PROMOCIÓN falla, el output oficial
    # quedó modificado por un exchange no compensado → RollbackIncompleteError, NUNCA un RollbackError
    # "reintentable" (B129). El estado `exchange_applied` sin `compensation_verified` fuerza la clasificación.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    tgt = camp / "campaign_pool_FAD_family.csv"  # idx0 preexistente → se promueve por exchange
    tgt.write_bytes(b"PRE\n")
    real_ex = mcp.rename_exchange
    st = {"n": 0}

    def ex(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "campaign_pool_FAD_family.csv":
            st["n"] += 1
            if st["n"] == 1:  # ANTES del 1er exchange (promoción), un tercero reemplaza el target
                v2 = camp / ".v2"
                v2.write_bytes(b"V2-CONCURRENT\n")
                os.replace(str(v2), str(tgt))
            elif st["n"] == 2:  # el 2º exchange = COMPENSACIÓN (swap de vuelta) → falla
                raise OSError("compensation-fail")
        return real_ex(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_exchange", ex)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError):  # exchange aplicado sin compensación verificada
        mcp.merge()


def test_b130_rollback_fsync_failure_is_incomplete(tmp_path, monkeypatch):
    # un fsync de directorio fallido durante el rollback activa `incomplete` → RollbackIncompleteError, no un
    # RollbackError con el fallo como mero texto (B130).
    _write_all_8(tmp_path)
    _fail_nth_cas(monkeypatch, 3)  # promoción #3 falla → rollback
    real_fsync = os.fsync
    armed = {"x": False}

    def bad_fsync(fd):
        import stat as _s

        if armed["x"] and _s.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("rollback dir fsync failure")
        return real_fsync(fd)

    orig_promote = mcp._promote_transactionally

    def wrapped(*a):
        armed["x"] = True  # a partir de la promoción, todo fsync de directorio falla → el rollback queda incompleto
        return orig_promote(*a)

    monkeypatch.setattr(mcp.os, "fsync", bad_fsync)
    monkeypatch.setattr(mcp, "_promote_transactionally", wrapped)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError):
        mcp.merge()


def test_b131_input_changed_during_certification_aborts(tmp_path, monkeypatch):
    # un input cambiado DESPUÉS de la última revalidación (durante la certificación del recibo) es cazado por la
    # re-lectura del recibo → NO rc=0 (B131); en R9.2R10 producía rc=0.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_nr = mcp.rename_noreplace
    done = {"x": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if not done["x"] and str(dst).startswith(mcp._RECEIPT_PREFIX):  # al promover el recibo, cambia un input
            done["x"] = True
            f = camp / "aq_pool_nongbm_FAD_family.csv"
            f.unlink()
            _pool_df("SWAPPED", "FAD", "family", ("ets",)).to_csv(f, index=False)
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    assert not (camp / "campaign_pool_FAD_family.csv").exists(), (
        "publicó con un input cambiado en la certificación (B131)"
    )


def test_b132_lock_replaced_during_certification_aborts(tmp_path, monkeypatch):
    # el lock desligado+recreado DESPUÉS de la última revalidación (durante la certificación) es cazado por la
    # re-lectura del recibo → NO rc=0 (B132).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    lock = camp / ".merge.lock"
    real_nr = mcp.rename_noreplace
    done = {"x": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if not done["x"] and str(dst).startswith(mcp._RECEIPT_PREFIX):  # al promover el recibo, sustituye el lock
            done["x"] = True
            lock.unlink()
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    assert not (camp / "campaign_pool_FAD_family.csv").exists(), (
        "publicó con el lock sustituido en la certificación (B132)"
    )


def test_b134_atomic_errno_mapping():
    # B134: el backend mapea ENOTDIR→NotADirectoryError e IsADirectoryError (además de EEXIST/ENOENT) — el
    # docstring lo promete y el backend lo produce (las clases estándar, no un AtomicRenameError genérico).
    import errno as _errno

    import tools.atomic_fs as afs

    assert afs._ERRNO_EXC[_errno.ENOTDIR] is NotADirectoryError
    assert afs._ERRNO_EXC[_errno.EISDIR] is IsADirectoryError
    assert afs._ERRNO_EXC[_errno.EEXIST] is FileExistsError
    assert afs._ERRNO_EXC[_errno.ENOENT] is FileNotFoundError


def test_b135_preexisting_quarantine_wrong_mode_not_repaired(tmp_path, monkeypatch):
    # un `.merge-quarantine` preexistente en 0777 se RECHAZA (fail-closed), NO se "repara" a 0700 (B135).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    (camp / "campaign_pool_FAD_family.csv").write_bytes(b"PRE\n")  # fuerza cleanup post-commit
    q = camp / ".merge-quarantine"
    q.mkdir()
    os.chmod(q, 0o777)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError):
        mcp.merge()
    assert (q.stat().st_mode & 0o777) == 0o777, "reparó silenciosamente el directorio de cuarentena (B135)"


def test_b136_journal_truncation_between_records_detected(tmp_path):
    # B136: un manifiesto truncado entre INTENT y COMPLETED se caza en la RE-LECTURA de la cadena de hashes.
    d = tmp_path / "reports" / "campaign"
    d.mkdir(parents=True)
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    quar = mcp._Quarantine("trunc.deadbeef")
    try:
        st = quar._prepare(dfd, "campaign")
        intent = {
            "record": "MOVE_INTENT", "operation_id": "0123456789abcdef", "source_name": "s", "destination_name": "dst",
            "expected_dev": 0, "expected_ino": 0, "expected_digest": None,
        }  # fmt: skip
        assert quar._journal(st, dfd, "campaign", intent), "INTENT válido"
        manifest = d / ".merge-quarantine" / "trunc.deadbeef" / "MANIFEST.jsonl"
        os.truncate(str(manifest), 0)  # un tercero trunca el manifiesto entre eventos
        completed = {
            "record": "MOVE_COMPLETED", "operation_id": "0123456789abcdef", "destination_name": "dst",
            "moved_dev": 0, "moved_ino": 0, "bound_to_tx_fd": True,
        }  # fmt: skip
        assert not quar._journal(st, dfd, "campaign", completed), "no cazó el truncado del journal (B136)"
    finally:
        quar.close([])
        os.close(dfd)


def test_b139_concurrency_preserved_is_incomplete(tmp_path, monkeypatch):
    # una actualización concurrente de un output AUSENTE, devuelta CORRECTAMENTE a su ruta oficial durante el
    # rollback, produce RollbackIncompleteError, NO un RollbackError reintentable: hubo divergencia externa
    # (B139). En R9.2R10 este caso (ABSENT_CONCURRENT_RETURNED) se clasificaba como RollbackError.
    _write_all_8(tmp_path)
    ev = tmp_path / "reports" / "eval"
    absent_tgt = ev / "model_comparison_DFF21.csv"
    real_nr = mcp.rename_noreplace
    st = {"done": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "model_comparison_EB_DFF21.csv":
            raise OSError("promo-fail")
        if src_dir_fd != dst_dir_fd and not st["done"] and absent_tgt.exists():
            st["done"] = True
            v2 = ev / ".v2"
            v2.write_bytes(b"V2\n")
            os.replace(str(v2), str(absent_tgt))
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError):  # aunque la concurrente vuelva a su ruta oficial
        mcp.merge()
    assert absent_tgt.read_bytes() == b"V2\n", "la concurrente no quedó en su ruta oficial (B139)"


def test_r11_static_gate_receipt_and_journal():
    # GATE ESTÁTICO (R9.2R11 §9 + Incremento 2): la AUTORIDAD del commit es el CommitCertificate de CURRENT, NO el
    # recibo. commit_reached es DERIVADO (property); el LATCH `_committed=True` SÓLO se pone en mark_current_certified/
    # mark_committed_incomplete; _certify_receipt NO lo toca. MANIFEST O_RDWR|O_APPEND|O_EXCL; journal encadenado.
    import ast
    import pathlib

    src = pathlib.Path(mcp.__file__).read_text()
    tree = ast.parse(src)
    funcs = {fn.name: fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)}
    # `self._committed = True` (el latch) aparece EXACTAMENTE dos veces: en mark_current_certified y mark_committed_incomplete
    latch = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_committed" for t in n.targets)
        and isinstance(n.value, ast.Constant)
        and n.value.value is True
    ]
    assert len(latch) == 2, "el latch _committed=True vive SÓLO en mark_current_certified/mark_committed_incomplete"
    for name in ("mark_current_certified", "mark_committed_incomplete"):
        fn = funcs[name]
        assert any(fn.lineno <= a.lineno <= (fn.end_lineno or a.lineno) for a in latch), f"{name} debe poner el latch"
    cr = funcs["_certify_receipt"]  # el recibo NUNCA declara el commit (es evidencia)
    assert not any(cr.lineno <= a.lineno <= (cr.end_lineno or a.lineno) for a in latch), (
        "_certify_receipt NO declara commit"
    )
    assert "@property" in src and "def commit_reached" in src, "commit_reached debe ser una property DERIVADA"
    assert "authority_crossed" in src, "el commit se declara consumiendo un CommitCertificate (authority_crossed)"
    assert "O_RDWR" in src and "O_APPEND" in src and "O_EXCL" in src, "MANIFEST debe abrirse O_RDWR|O_APPEND|O_EXCL"
    assert "exchange_applied" in src and "compensation_verified" in src, "debe rastrear el estado CAS explícito"
    assert "record_sha256" in src and "previous_record_sha256" in src, "el journal debe encadenar hashes"
    assert "_RECEIPT_PREFIX" in src and "_certify_receipt" in src, (
        "debe existir el recibo de commit gobernado (evidencia)"
    )


# ----------------------------- B140-B147: recibo/journal/compensación endurecidos -----------------------------


def test_b140_input_inplace_mutation_during_certification_aborts(tmp_path, monkeypatch):
    # una mutación IN-PLACE (mismo inode) de un input durante la promoción del recibo es cazada porque el recibo
    # RECALCULA el digest ACTUAL del input, no reutiliza el cacheado (B140). En f967a3c devolvía rc=0.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_nr = mcp.rename_noreplace
    done = {"x": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        if not done["x"] and str(dst).startswith(mcp._RECEIPT_PREFIX):
            done["x"] = True
            with open(camp / "aq_pool_nongbm_FAD_family.csv", "ab") as fh:  # mismo inode, contenido cambiado
                fh.write(b"\n# tampered\n")
        return real_nr(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    assert not (camp / "campaign_pool_FAD_family.csv").exists(), "publicó con un input mutado in-place (B140)"


def test_b141_concurrent_substituted_during_compensation_is_incomplete(tmp_path, monkeypatch):
    # si el objeto CONCURRENTE es sustituido durante la compensación (el lado oficial queda con un ajeno), la
    # verificación de AMBOS lados lo caza → RollbackIncompleteError, no un RollbackError reintentable (B141).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    tgt = camp / "campaign_pool_FAD_family.csv"
    tgt.write_bytes(b"PRE\n")
    real_ex = mcp.rename_exchange
    st = {"n": 0}

    def ex(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "campaign_pool_FAD_family.csv":
            st["n"] += 1
            if st["n"] == 1:  # antes del 1er exchange: un tercero reemplaza el target (concurrencia)
                v2 = camp / ".v2"
                v2.write_bytes(b"V2-CONCURRENT\n")
                os.replace(str(v2), str(tgt))
            elif st["n"] == 2:  # antes de la COMPENSACIÓN: sustituye el concurrente desplazado en temp_name
                for p in camp.iterdir():
                    if p.name.startswith(".campaign_pool_FAD_family.csv.tmp."):
                        with open(p, "wb") as fh:
                            fh.write(b"V3-FORGED\n")
        return real_ex(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_exchange", ex)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError):  # NUNCA RollbackError reintentable
        mcp.merge()


def test_b142_manifest_relinked_during_journaling_fails(tmp_path):
    # si el MANIFEST.jsonl oficial se desliga y se sustituye por otro, `_journal` (que verifica el binding
    # nombre↔fd antes/después) lo caza y NO valida sobre el fd huérfano (B142).
    d = tmp_path / "reports" / "campaign"
    d.mkdir(parents=True)
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    quar = mcp._Quarantine("relink.deadbeef")
    try:
        st = quar._prepare(dfd, "campaign")
        manifest = d / ".merge-quarantine" / "relink.deadbeef" / "MANIFEST.jsonl"
        manifest.unlink()  # desliga el oficial
        (d / ".merge-quarantine" / "relink.deadbeef" / "MANIFEST.jsonl").write_text('{"record":"FORGED-OFFICIAL"}\n')
        rec = {
            "record": "MOVE_INTENT", "operation_id": "0123456789abcdef", "source_name": "s", "destination_name": "dst",
            "expected_dev": 0, "expected_ino": 0, "expected_digest": None,
        }  # fmt: skip
        assert not quar._journal(st, dfd, "campaign", rec), "escribió/validó sobre el manifiesto huérfano (B142)"
    finally:
        quar.close([])
        os.close(dfd)


def test_b143_receipt_hardlink_rejected(tmp_path, monkeypatch):
    # un recibo con nlink=2 (hardlink) o sustituido por otro inode se RECHAZA en la re-apertura (B143): no basta
    # comparar bytes, se exige regular/UID/modo 0600/nlink==1.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_nr = mcp.rename_noreplace
    done = {"x": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        r = real_nr(src_dir_fd, src, dst_dir_fd, dst)
        if not done["x"] and str(dst).startswith(mcp._RECEIPT_PREFIX):  # tras promover el recibo, plántale un hardlink
            done["x"] = True
            os.link(str(camp / dst), str(camp / (dst + ".hardlink")))  # nlink pasa a 2
        return r

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()


def test_b144_receipt_aborted_on_rollback(tmp_path, monkeypatch):
    # si la certificación falla DESPUÉS de publicar el recibo (aquí: se muta un output tras publicarlo, la
    # re-verificación del recibo diverge), el rollback lo mueve al dir gobernado `.merge-aborted/<txid>/` — jamás
    # queda un recibo "oficial" con los outputs revertidos (B144/B150).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_nr = mcp.rename_noreplace
    done = {"x": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        r = real_nr(src_dir_fd, src, dst_dir_fd, dst)
        if not done["x"] and str(dst).startswith(mcp._RECEIPT_PREFIX):  # tras publicar el recibo, muta un output
            done["x"] = True
            with open(camp / "campaign_pool_FAD_family.csv", "ab") as fh:
                fh.write(b"\n# tampered after receipt\n")
        return r

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    official = list(camp.glob(".merge-receipt.*.json"))
    aborted = list((camp / ".merge-aborted").rglob("receipt.*")) if (camp / ".merge-aborted").exists() else []
    assert not official and aborted, "quedó un recibo oficial tras el rollback (B144/B150)"


@pytest.mark.parametrize(
    "tamper",
    [
        b'{"schema_version":1,"txid":"x","sequence":1,"source_dir":"campaign","record":"MOVE_INTENT","operation_id":"o","source_name":"s","destination_name":"d","expected_dev":0,"expected_ino":0,"expected_digest":null,"expected_digest":"dup","previous_record_sha256":"","record_sha256":"z"}',
        b'{"schema_version":999,"txid":"x","sequence":1,"source_dir":"campaign","record":"MOVE_INTENT","operation_id":"o","source_name":"s","destination_name":"d","expected_dev":0,"expected_ino":0,"expected_digest":null,"previous_record_sha256":"","record_sha256":"z"}',
        b'{"schema_version":1,"txid":"x","sequence":1,"source_dir":"campaign","record":"UNKNOWN_EVENT","operation_id":"o","previous_record_sha256":"","record_sha256":"z"}',
        b'{"schema_version":1,"txid":"x","sequence":1,"source_dir":"campaign","record":"MOVE_INTENT","operation_id":"o","source_name":"s","destination_name":"d","expected_dev":0,"expected_ino":0,"expected_digest":null,"EXTRA":"field","previous_record_sha256":"","record_sha256":"z"}',
    ],
)
def test_b146_journal_schema_rejects_tampering(tamper):
    # B146: el reloader del journal rechaza claves duplicadas, schema_version falso, tipo desconocido y campos extra.
    import hashlib as _h

    # completa el record_sha256 correcto para aislar el fallo al esquema (no al hash)
    import json as _json

    try:
        rec = mcp._strict_loads(tamper)  # las claves duplicadas mueren ya aquí
        body = {k: v for k, v in rec.items() if k != "record_sha256"}
        rec["record_sha256"] = _h.sha256(mcp._canon(body)).hexdigest()
        line = mcp._canon(rec)
        parsed = mcp._strict_loads(line)
        ok = (
            parsed["record"] in mcp._JOURNAL_SCHEMAS
            and set(parsed.keys()) == mcp._JOURNAL_COMMON | mcp._JOURNAL_SCHEMAS[parsed["record"]]
            and parsed["schema_version"] == mcp._SCHEMA_VERSION
        )
        assert not ok, "el journal aceptó un registro con esquema inválido (B146)"
    except ValueError:
        pass  # clave duplicada rechazada por _strict_loads — correcto
    del _json


def test_b147_receipt_provenance_is_complete():
    # B147: la procedencia liga git/python/contrato/env_id y los hashes COMPLETOS (64 hex) de los tres módulos
    # gobernantes, no 16 chars de uno solo.
    prov = mcp._provenance()
    assert set(prov) >= {"git_head", "python", "env_id", "contract_sha256", "modules"}
    assert set(prov["modules"]) == {"merge_campaign_pools", "atomic_fs", "governed_read"}
    for h in prov["modules"].values():
        assert h == "unknown" or len(h) == 64, "hash de módulo incompleto (B147)"


# ----------------------------- B149-B154 (R9.2R12R): recibo ligado al fd, aborted gobernado, journal semántico -----------------------------


def test_b149_receipt_substituted_same_bytes_rejected(tmp_path, monkeypatch):
    # sustituir el recibo por OTRO inode con los MISMOS bytes/modo 0600/nlink==1 tras publicarlo es cazado porque
    # el recibo se lee del fd que CREAMOS y se exige que el nombre LIGUE a ese fd (B149). En 9734648 devolvía 0.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_nr = mcp.rename_noreplace
    done = {"x": False}

    def nr(src_dir_fd, src, dst_dir_fd, dst):
        r = real_nr(src_dir_fd, src, dst_dir_fd, dst)
        if not done["x"] and str(dst).startswith(mcp._RECEIPT_PREFIX):
            done["x"] = True
            rp = camp / dst
            data = rp.read_bytes()
            rp.unlink()
            alt = camp / (dst + ".alt")  # NUEVO inode, mismos bytes, 0600, nlink==1
            alt.write_bytes(data)
            os.chmod(alt, 0o600)
            os.replace(str(alt), str(rp))
        return r

    monkeypatch.setattr(mcp, "rename_noreplace", nr)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    assert not (camp / "campaign_pool_FAD_family.csv").exists(), "publicó con el recibo sustituido (B149)"


def test_b150_aborted_collision_is_incomplete(tmp_path, monkeypatch):
    # una colisión PREPLANTADA del dir aborted del txid hace que `_abort_receipt` falle CERRADO (RECEIPT_ABORT_
    # FAILED → incompleto), no que se ignore con el recibo oficial intacto (B150).
    d = tmp_path / "reports" / "campaign"
    d.mkdir(parents=True)
    (d / ".merge-receipt.tx150.json").write_bytes(b"{}\n")
    os.chmod(d / ".merge-receipt.tx150.json", 0o600)
    (d / ".merge-aborted").mkdir()
    (d / ".merge-aborted" / "tx150").mkdir()  # colisión preplantada del <txid>
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.chdir(d)
    ctx = mcp._TxContext()
    ctx.receipt_name = ".merge-receipt.tx150.json"
    try:
        mcp._abort_receipt(dfd, "tx150", ctx)
        assert ctx.incomplete, "una colisión del dir aborted no marcó incompleto (B150)"
        assert (d / ".merge-receipt.tx150.json").exists(), "el recibo oficial no debe destruirse en el fallo"
    finally:
        os.close(dfd)


def test_b152_official_run_requires_full_provenance(tmp_path, monkeypatch):
    # una ejecución marcada OFICIAL (`VP_OFFICIAL_RUN=1`) sin procedencia completa (env_id/git/etc.) aborta
    # fail-closed (B152). En 9734648 no había gate → rc=0.
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    monkeypatch.setenv("VP_OFFICIAL_RUN", "1")
    monkeypatch.delenv("VP_ENV_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    assert not (camp / "campaign_pool_FAD_family.csv").exists(), "commit oficial sin procedencia (B152)"


def test_b153_verified_compensation_still_incomplete(tmp_path, monkeypatch):
    # una concurrencia cuya compensación RESTAURA ambos lados correctamente sigue siendo RollbackIncompleteError,
    # nunca un RollbackError reintentable: la concurrencia DETECTADA es una divergencia externa (B153).
    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    tgt = camp / "campaign_pool_FAD_family.csv"
    tgt.write_bytes(b"PRE\n")
    real_ex = mcp.rename_exchange
    st = {"n": 0}

    def ex(src_dir_fd, src, dst_dir_fd, dst):
        if dst == "campaign_pool_FAD_family.csv":
            st["n"] += 1
            if st["n"] == 1:  # concurrencia antes de promover; la compensación luego restaura AMBOS lados
                v2 = camp / ".v2"
                v2.write_bytes(b"V2-CONCURRENT\n")
                os.replace(str(v2), str(tgt))
        return real_ex(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(mcp, "rename_exchange", ex)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.RollbackIncompleteError):  # aunque la compensación verifique ambos lados
        mcp.merge()


def test_b154_incompatible_terminal_rejected(tmp_path):
    # el journal RECHAZA un terminal de familia incorrecta (MOVE_INTENT no cierra con RESTORE_*) (B154).
    d = tmp_path / "reports" / "campaign"
    d.mkdir(parents=True)
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    quar = mcp._Quarantine("fam.deadbeef")
    try:
        st = quar._prepare(dfd, "campaign")
        intent = {
            "record": "MOVE_INTENT", "operation_id": "0123456789abcdef", "source_name": "s",
            "destination_name": "dst", "expected_dev": 0, "expected_ino": 0, "expected_digest": None,
        }  # fmt: skip
        assert quar._journal(st, dfd, "campaign", intent), "MOVE_INTENT válido"
        wrong = {"record": "RESTORE_COMPLETED", "operation_id": "0123456789abcdef", "destination_name": "dst"}
        assert not quar._journal(st, dfd, "campaign", wrong), "aceptó un terminal RESTORE_* para un MOVE_INTENT (B154)"
    finally:
        quar.close([])
        os.close(dfd)


# --------------------- B148/B145 (increment 1): el bundle content-addressed es la AUTORIDAD del commit ---------------------


def test_b148_merge_publishes_bundle_authority_and_current(tmp_path, monkeypatch):
    # tras un merge exitoso el commit publica un bundle inmutable + puntero CURRENT por CAS; los consumidores
    # resuelven la añada oficial vía open_current_bundle()/read_current_csv() — no por leer el CSV suelto.
    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    monkeypatch.chdir(tmp_path)
    assert mcp.merge() == 0
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        bundle_id, manifest = cb.open_current_bundle(cfd)  # RED en HEAD: no hay CURRENT ⇒ BundleError
        assert len(bundle_id) == 64 and len(manifest["outputs"]) == 8  # 4 tablas×bloques × (campaign+eval)
        sealed = cb.read_current_csv(cfd, "campaign", "campaign_pool_FAD_family.csv")
        assert sealed == (camp / "campaign_pool_FAD_family.csv").read_bytes(), (
            "el bundle no selló el output committeado"
        )
    finally:
        os.close(cfd)


def test_b148_post_commit_csv_mutation_does_not_corrupt_authority(tmp_path, monkeypatch):
    # B148 raíz: mutar un CSV committeado DESPUÉS del commit no altera la autoridad. El consumidor que resuelve
    # por el bundle sigue leyendo los bytes SELLADOS (el CSV es una proyección; la verdad vive en el bundle).
    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    monkeypatch.chdir(tmp_path)
    assert mcp.merge() == 0
    csv = camp / "campaign_pool_FAD_family.csv"
    original = csv.read_bytes()
    os.chmod(csv, 0o600)
    with open(csv, "ab") as fh:  # deriva del corte oficial tras cruzar el commit
        fh.write(b"9,9,9\n")
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        sealed = cb.read_current_csv(cfd, "campaign", "campaign_pool_FAD_family.csv")
        assert sealed == original, "la autoridad del bundle se corrompió con una mutación post-commit del CSV"
    finally:
        os.close(cfd)


def test_b158_mutation_between_receipt_and_sealing_never_enters_bundle(tmp_path, monkeypatch):
    # B158 + Incremento 2: una mutación de un output DESPUÉS del recibo (EVIDENCIA) y ANTES del sellado NO entra al
    # bundle. El sellado relee desde el fd certificado revalidando el digest → BundleValidationError ANTES del CAS de
    # CURRENT → el commit NO cruza → ROLLBACK (la autoridad CURRENT jamás se envenena; nunca se sella lo mutado).
    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    real_cert = mcp._certify_receipt

    def cert_then_mutate(chain, lock, inputs, outs, quar, ctx):
        real_cert(chain, lock, inputs, outs, quar, ctx)  # recibo (evidencia; certifica los outputs)
        p = camp / "campaign_pool_FAD_family.csv"  # …y ENTONCES un tercero muta el output ya certificado
        os.chmod(p, 0o600)
        with open(p, "ab") as fh:
            fh.write(b"9,9,9\n")

    monkeypatch.setattr(mcp, "_certify_receipt", cert_then_mutate)
    monkeypatch.chdir(tmp_path)
    # el CAS de CURRENT NO cruza (la mutación se detecta pre-CAS en el sellado) ⇒ rollback; jamás se sella lo mutado.
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    monkeypatch.undo()
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(cb.BundleError):  # el bundle NO se publicó con los bytes mutados (RED en bddfe15)
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_b164_manifest_provenance_carries_module_hashes_and_journal_heads(tmp_path, monkeypatch):
    # B164: el manifiesto sella procedencia COMPLETA — hash REAL de campaign_bundle.py (y demás módulos) y las
    # cabezas terminales de journal, no evidencia reconstruida ni ausente.
    import hashlib

    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)
    camp = tmp_path / "reports" / "campaign"
    monkeypatch.chdir(tmp_path)
    assert mcp.merge() == 0
    monkeypatch.undo()
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        _, manifest = cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)
    prov = manifest["provenance"]
    real = hashlib.sha256(open(cb.__file__, "rb").read()).hexdigest()
    assert prov["code_sha_campaign_bundle"] == real, "provenance no lleva el hash real de campaign_bundle.py"
    assert set(prov.keys()) == set(cb._REQUIRED_PROVENANCE)
    assert isinstance(prov["journal_heads"], dict)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_b203_b210_fabricated_identity_rejected(monkeypatch, tmp_path):
    # B203/B210: env vars fabricadas Y un .vp_envs/<64hex>/READY.json falso NO bastan — sys.prefix del proceso no es
    # el entorno sellado, y un env_id no-hex se rechaza. El intérprete real no cambia por un dir plantado.
    monkeypatch.setenv("VP_ENV_ID", "a" * 64)
    monkeypatch.setenv("VP_ENV_PROFILE", "model")
    assert mcp._governed_run_identity() is None  # sys.prefix real (ante) no es .vp_envs/<env_id>
    # plantar un dir falso .vp_envs/model/<64hex>/READY.json no cambia sys.prefix del proceso
    fake = tmp_path / ".vp_envs" / "model" / ("a" * 64)
    fake.mkdir(parents=True)
    (fake / "READY.json").write_text('{"env_id":"' + "a" * 64 + '","profile":"model"}')
    assert mcp._governed_run_identity() is None  # el dir falso no se convierte en sys.prefix
    monkeypatch.setenv("VP_ENV_ID", "ZZZ" + "a" * 61)  # no hexadecimal
    assert mcp._governed_run_identity() is None


def _run_isolated(code, timeout=6.0):
    """Ejecuta `code` en un SUBPROCESO killable (un open bloqueante sobre un FIFO no se interrumpe dentro del mismo
    proceso). Devuelve (terminó_a_tiempo, stdout+stderr)."""
    p = subprocess.Popen(
        [sys.executable, "-c", code], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        out, _ = p.communicate(timeout=timeout)
        return True, out
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        return False, "<TIMEOUT>"


_B219_CAPTURE_SPECIAL = """
import os, sys, tempfile
sys.path.insert(0, os.getcwd())
import tools.merge_campaign_pools as mcp
d = tempfile.mkdtemp(dir="/tmp"); dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
name = "cap_special"
if {kind!r} == "fifo":
    os.mkfifo(os.path.join(d, name))
else:
    import socket
    s = socket.socket(socket.AF_UNIX); s.bind(os.path.join(d, name))
r = mcp._capture_object(dfd, name)     # VIEJO: O_RDONLY sobre un FIFO CUELGA; NUEVO: no cuelga, no es 'regular'
print("KIND:" + getattr(r, "kind", "LEGACY_" + type(r).__name__))
print("DONE")
"""


@pytest.mark.parametrize("kind,expected", [("fifo", ("special",)), ("socket", ("special", "error"))])
def test_b219_capture_object_special_no_hang(kind, expected):
    # B219: _capture_object() sobre un objeto especial NO debe COLGAR (apertura no bloqueante) — es la ruta de
    # compensación CAS que un tercero del mismo UID podría sustituir por un FIFO en `temp_name`.
    completed, out = _run_isolated(_B219_CAPTURE_SPECIAL.replace("{kind!r}", repr(kind)))
    assert completed, f"B219: _capture_object COLGÓ sobre un {kind}"
    assert any(f"KIND:{e}" in out for e in expected), out


def test_b219_capture_object_discriminates(tmp_path):
    # B219: resultado ESTRUCTURADO — regular/absent/special no se confunden (nada de None ambiguo); un especial
    # jamás coincide con un regular capturado (no se puede desplazar con éxito).
    d = tmp_path / "cap"
    d.mkdir()
    dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("reg", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=dfd)
        os.write(fd, b"hi")
        os.close(fd)
        os.mkfifo("fifo", dir_fd=dfd)
        assert mcp._capture_object(dfd, "reg").kind == "regular"
        assert mcp._capture_object(dfd, "nope").kind == "absent"
        assert mcp._capture_object(dfd, "fifo").kind == "special"
        cap = mcp._capture_object(dfd, "reg")
        assert mcp._object_matches(dfd, "reg", cap) is True
        assert mcp._object_matches(dfd, "fifo", cap) is False  # un especial nunca coincide con el regular capturado
    finally:
        os.close(dfd)


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_read_bytes_abs_special_no_hang(kind):
    # B218: read_bytes_abs sobre un FIFO/socket (contrato/módulo/READY sustituido) NO cuelga; rechaza por tipo/OS.
    import socket
    import tempfile
    import threading

    d = tempfile.mkdtemp(dir="/tmp")  # ruta corta: un socket AF_UNIX topa el límite de sun_path
    target = os.path.join(d, "special")
    sock = None
    if kind == "fifo":
        os.mkfifo(target)
    else:
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(target)
    holder: dict = {}

    def run():
        try:
            gr.read_bytes_abs(target)
            holder["ok"] = True
        except (gr.GovernedOpenError, OSError) as exc:
            holder["rejected"] = type(exc).__name__

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(4)
    try:
        assert not t.is_alive(), f"read_bytes_abs COLGÓ sobre un {kind}"
        assert holder.get("rejected"), f"read_bytes_abs debe rechazar un {kind}"
    finally:
        if sock is not None:
            sock.close()


def test_read_bytes_path_rejects_ancestor_symlink(tmp_path):
    # B218: opened_regular_noblock_path camina cada dir con O_NOFOLLOW → un ancestro symlink revienta (no lo sigue).
    real = tmp_path / "real" / "sub"
    real.mkdir(parents=True)
    (real / "f").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    rfd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert gr.read_bytes_path(rfd, "real/sub/f") == b"x"  # ruta real funciona
        with pytest.raises((OSError, gr.GovernedOpenError)):  # ancestro symlink → NotADirectoryError/ELOOP
            gr.read_bytes_path(rfd, "link/sub/f")
    finally:
        os.close(rfd)


# ------------------------- Incremento 2: la autoridad del commit es el CommitCertificate de CURRENT -------------------------


def test_inc2_seal_failure_before_cas_rolls_back(tmp_path, monkeypatch):
    # Incremento 2 (frontera): un fallo del sellado del bundle ANTES del CAS de CURRENT NO es un Issue post-commit —
    # el commit NO cruza y el merge hace ROLLBACK. RED sobre 02f9d6c (allí el recibo ya "commiteó" ⇒ CommittedStateError).
    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)

    def boom(fd, digest, what):
        raise cb.BundleValidationError(f"seal failed pre-CAS: {what}")

    monkeypatch.setattr(mcp, "_seal_bytes_from_fd", boom)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    camp = tmp_path / "reports" / "campaign"
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(cb.BundleError):  # CURRENT nunca cruzó → no hay bundle publicado
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_inc2_real_merge_crosses_via_certificate(tmp_path, monkeypatch):
    # una corrida real cruza el commit vía CURRENT (CommitCertificate); CURRENT apunta a un bundle válido.
    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert mcp.merge() == 0
    camp = tmp_path / "reports" / "campaign"
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        bundle_id, manifest = cb.open_current_bundle(cfd)  # CURRENT es autoridad válida
        assert isinstance(bundle_id, str) and len(bundle_id) == 64
    finally:
        os.close(cfd)


def _durable_cert(bid: str = "a" * 64):
    import tools.campaign_bundle as cb

    # B226: un cert REAL durable liga al contrato CSV pineado (lo que produce _build_certificate); usar el sha pineado
    # para que la validación semántica lo acepte (los demás campos ya son semánticamente válidos).
    return cb.CommitCertificate(
        bundle_id=bid, previous_bundle_id=None, campaign_id="c", pointer_digest="b" * 64, pointer_inode=(1, 2),
        manifest_digest=bid, bundle_inode=(3, 4), csv_contract_sha256=cb._CSV_CONTRACT_SHA256, provenance_digest="e" * 64,
        durability_state="durable",
    )  # fmt: skip


def test_inc2_certificate_is_immutable():
    # el CommitCertificate es FROZEN: ningún consumidor puede mutar la evidencia del commit.
    import dataclasses

    import tools.campaign_bundle as cb

    cert = cb.CommitCertificate(
        bundle_id="a" * 64, previous_bundle_id=None, campaign_id="c", pointer_digest="b" * 64,
        pointer_inode=(1, 2), manifest_digest="a" * 64, bundle_inode=(3, 4), csv_contract_sha256="d" * 64,
        provenance_digest="e" * 64, durability_state="durable",
    )  # fmt: skip
    assert cert.authority_crossed is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        cert.bundle_id = "z" * 64  # inmutable
    with pytest.raises(dataclasses.FrozenInstanceError):
        cert.authority_crossed = False


def test_inc2_txcontext_frontier_invariants():
    # B222: SÓLO un CommitCertificate REAL y DURABLE declara el cruce; un objeto con authority_crossed (duck typing)
    # es RECHAZADO; forward-only; incompleto exige cert.
    import types

    import tools.campaign_bundle as cb

    c = mcp._TxContext()
    c.transition(mcp._S_PROJECTIONS_DURABLE)
    c.transition(mcp._S_RECEIPT_CERTIFIED)
    assert not c.commit_reached  # el recibo NO es autoridad
    c.transition(mcp._S_CURRENT_CAS_STARTED)
    for forged in (object(), None, types.SimpleNamespace(authority_crossed=True)):  # duck typing RECHAZADO
        with pytest.raises(mcp.RollbackError):
            c.mark_current_certified(forged)
    nondurable = dataclasses_replace_durability(_durable_cert(), "cas_crossed")  # cert real pero NO durable → rechazado
    with pytest.raises(mcp.RollbackError):
        c.mark_current_certified(nondurable)
    bad_hash = dataclasses_replace_bundle_id(_durable_cert(), "zz")  # manifest_digest != bundle_id / no 64-hex
    with pytest.raises(mcp.RollbackError):
        c.mark_current_certified(bad_hash)
    assert isinstance(_durable_cert(), cb.CommitCertificate)
    c.mark_current_certified(_durable_cert())  # cert real durable → cruza
    assert c.commit_reached and c.certificate is not None
    with pytest.raises(mcp.RollbackError):
        c.transition(mcp._S_RECEIPT_CERTIFIED)  # forward-only
    c.mark_closed()
    assert c.commit_reached


def dataclasses_replace_durability(cert, durability):
    import dataclasses

    return dataclasses.replace(cert, durability_state=durability)


def dataclasses_replace_bundle_id(cert, bid):
    import dataclasses

    return dataclasses.replace(cert, bundle_id=bid)


def test_inc2_committed_incomplete_requires_crossed_evidence():
    # B221/B222: COMMITTED_INCOMPLETE EXIGE el CommitCertificate REAL de la autoridad cruzada; jamás sólo 'CAS iniciado'.
    c = mcp._TxContext()
    c.transition(mcp._S_PROJECTIONS_DURABLE)
    c.transition(mcp._S_RECEIPT_CERTIFIED)
    with pytest.raises(mcp.RollbackError):  # aún no se inició el CAS
        c.mark_committed_incomplete(_durable_cert())
    c.transition(mcp._S_CURRENT_CAS_STARTED)
    with pytest.raises(mcp.RollbackError):  # sin cert real → 'CAS iniciado' NO basta como evidencia de cruce
        c.mark_committed_incomplete(object())
    c.mark_committed_incomplete(_durable_cert())
    assert c.commit_reached  # el CAS cruzó (cert durable) incompleto → sigue siendo commit cruzado (no rollback)


def test_inc2_committed_state_error_carries_crossed_evidence():
    # B222: CommittedStateError lleva el CommitCertificate REAL (.certificate), no un atributo booleano de clase;
    # AuthorityIndeterminateError lleva retry_safe=False.
    import tools.campaign_bundle as cb

    cert = _durable_cert()
    e = cb.CommittedStateError("x", certificate=cert)
    assert e.certificate is cert and e.certificate.durability_state == "durable"
    assert not hasattr(cb.CommittedStateError, "authority_crossed")  # ya no es un bool de clase forjable
    aie = cb.AuthorityIndeterminateError("y", expected_new="a" * 64, expected_previous=None, observed_current="foreign", failure_point="test")  # fmt: skip
    assert aie.retry_safe is False


def test_inc2_postcommit_failure_keeps_current_authority(tmp_path, monkeypatch):
    # Ronda adversarial (Incremento 2): tras cruzar CURRENT, un fallo post-commit → CommittedStateError, PERO CURRENT
    # sigue siendo la autoridad VÁLIDA — JAMÁS se hace rollback tras el certificado (invariante #13).
    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)
    real_fsync = os.fsync
    armed = {"x": False}

    def counting(fd):
        import stat as _s

        if armed["x"] and _s.S_ISDIR(os.fstat(fd).st_mode):
            armed["x"] = False
            raise OSError("postcommit dir fsync")
        return real_fsync(fd)

    orig = mcp._publish_bundle

    def wrapped(*a):
        cert = orig(*a)  # CURRENT cruzó y se certificó
        armed["x"] = True
        return cert

    monkeypatch.setattr(mcp.os, "fsync", counting)
    monkeypatch.setattr(mcp, "_publish_bundle", wrapped)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(mcp.CommittedStateError):
        mcp.merge()
    monkeypatch.undo()
    camp = tmp_path / "reports" / "campaign"
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        bid, _manifest = cb.open_current_bundle(cfd)  # CURRENT sigue siendo autoridad válida (no hubo rollback)
        assert len(bid) == 64
    finally:
        os.close(cfd)


def test_inc2_no_forged_receipt_authority(tmp_path, monkeypatch):
    # Ronda adversarial: un recibo válido NO basta para declarar commit. Si el CAS de CURRENT no cruza (bundle falla
    # pre-CAS), el recibo existe pero el commit NO cruzó → rollback; CURRENT nunca aparece.
    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)

    def boom(fd, digest, what):
        raise cb.BundleValidationError("pre-CAS")

    monkeypatch.setattr(mcp, "_seal_bytes_from_fd", boom)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((mcp.RollbackError, mcp.RollbackIncompleteError)):
        mcp.merge()
    monkeypatch.undo()
    camp = tmp_path / "reports" / "campaign"
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(cb.BundleError):  # el recibo por sí solo NO crea autoridad CURRENT
            cb.open_current_bundle(cfd)
    finally:
        os.close(cfd)


def test_b221_compensation_failure_current_crossed_no_rollback(tmp_path, monkeypatch):
    # B221 (FLAGSHIP): exchange CRUZADO + certificación falla + compensación falla → la reconciliación fd-bound ve
    # CURRENT==nuevo bundle válido → COMMITTED (cruzado), JAMÁS rollback de proyecciones. RED sobre e1e7dd5 (allí
    # "sin authority_crossed" ⇒ pre-CAS ⇒ rollback, dejando CURRENT→nuevo pero proyecciones→viejas: autoridad partida).
    import pandas as pd

    import tools.campaign_bundle as cb

    _write_all_8(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert mcp.merge() == 0  # 1er commit: CURRENT + outputs
    camp = tmp_path / "reports" / "campaign"
    for f in os.listdir(camp):  # 2º merge con bundle DISTINTO (run_id distinto) → EXCHANGE
        if f.startswith("aq_pool_"):
            p = camp / f
            df = pd.read_csv(p, dtype={"run_id": str})
            df["run_id"] = "v2"
            df.to_csv(p, index=False)
    # inyecta: certificación falla + compensación falla → CURRENT queda como el NUEVO bundle (compensación no lo deshace)
    monkeypatch.setattr(cb, "_certify_current", lambda *a, **k: (_ for _ in ()).throw(cb.BundleValidationError("inject certify")))  # fmt: skip
    monkeypatch.setattr(cb, "_compensate", lambda *a, **k: (_ for _ in ()).throw(cb.BundleRollbackIncompleteError("inject compensate")))  # fmt: skip
    with pytest.raises(mcp.CommittedStateError):  # CURRENT cruzó (reconciliado) → NO rollback como pre-CAS
        mcp.merge()
    monkeypatch.undo()
    cfd = os.open(str(camp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        bid, _manifest = cb.open_current_bundle(cfd)  # CURRENT sigue siendo la autoridad NUEVA válida (cruzó)
        assert len(bid) == 64
    finally:
        os.close(cfd)


def test_b226_certificate_semantic_fields_rejected():
    # B226: un CommitCertificate REAL con campos con forma valida pero SEMANTICAMENTE invalidos es RECHAZADO por
    # _validate_commit_certificate (previous no-SHA, campaign_id object/vacio, inodes invalidos, contrato CSV ajeno).
    import tools.campaign_bundle as cb

    def cert(**over):
        base = dict(
            bundle_id="a" * 64, previous_bundle_id=None, campaign_id="c", pointer_digest="b" * 64, pointer_inode=(1, 2),
            manifest_digest="a" * 64, bundle_inode=(3, 4), csv_contract_sha256=cb._CSV_CONTRACT_SHA256,
            provenance_digest="e" * 64, durability_state="durable",
        )  # fmt: skip
        base.update(over)
        return cb.CommitCertificate(**base)

    mcp._validate_commit_certificate(cert())  # el valido pasa
    for bad in (
        {"previous_bundle_id": "NOT-A-SHA"},
        {"campaign_id": object()},
        {"campaign_id": ""},
        {"pointer_inode": (-1, 2)},
        {"bundle_inode": ("x", 2)},
        {"csv_contract_sha256": "d" * 64},
        {"manifest_digest": "f" * 64},
    ):
        with pytest.raises(mcp.RollbackError):
            mcp._validate_commit_certificate(cert(**bad))
