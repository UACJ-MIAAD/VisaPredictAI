#!/usr/bin/env python
"""Contrato end-to-end del bundle/CURRENT (P0R.5 · Incremento 2), para el job CI `campaign-bundle-contract`.

Construye una autoridad CURRENT REAL en un workspace DESECHABLE y verifica la frontera de commit por SUBPROCESO
(nunca importa la maquinaria online — se mantiene fuera del inventario del gate de aperturas):

1. Merge #1 (sin autoridad previa) → CURRENT válido; `validate-current` rc 0, previous_bundle_id = null.
2. Merge #2 (CAMPAIGN_ID distinto → bundle distinto) → EXCHANGE sobre la autoridad previa; `validate-current` rc 0,
   previous_bundle_id = el bundle #1.
3. NEGATIVO OBLIGATORIO: se corrompe el puntero CURRENT → `validate-current` DEBE fallar (rc != 0).

Emite un RECIBO JSON canónico ligado al SHA de git (git_sha + los dos bundle_id + resultados). Sale != 0 ante
cualquier anomalía. Uso: `python -m tools.campaign_bundle_contract_smoke [--receipt PATH]`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rewrite_run_id(camp: str, value: str) -> None:
    """Reescribe la columna run_id de las 8 mitades a `value` (para elegir el bundle_id: run_id distinto → contenido
    distinto → bundle distinto). Preserva las demás columnas."""
    for f in os.listdir(camp):
        if f.startswith("aq_pool_"):
            p = os.path.join(camp, f)
            df = pd.read_csv(p, dtype={"run_id": str})
            df["run_id"] = value
            df.to_csv(p, index=False)


def _run_merge(workspace: str, campaign_id: str | None) -> int:
    env = {**os.environ, "PYTHONPATH": ROOT}
    if campaign_id is not None:
        env["CAMPAIGN_ID"] = campaign_id
    r = subprocess.run(
        [sys.executable, "-m", "tools.merge_campaign_pools"], cwd=workspace, env=env, capture_output=True, text=True
    )
    if r.returncode != 0:
        sys.stderr.write(f"merge falló (campaign={campaign_id}): {r.stderr}\n")
    return r.returncode


def _validate(workspace: str) -> tuple[int, dict]:
    env = {**os.environ, "PYTHONPATH": ROOT}
    r = subprocess.run(
        [sys.executable, "-m", "tools.campaign_bundle", "validate-current", "reports/campaign"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,  # fmt: skip
    )
    try:
        report = json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        report = {"status": "unparseable", "raw": r.stdout[:200]}
    return r.returncode, report


_TYPE_CONTRACT_PROBE = (
    "import tools.campaign_bundle as cb\n"
    "c = cb.CommitCertificate(bundle_id='a'*64, previous_bundle_id=None, campaign_id='x', pointer_digest='b'*64,"
    " pointer_inode=(1,2), manifest_digest='a'*64, bundle_inode=(3,4), csv_contract_sha256='d'*64,"
    " provenance_digest='e'*64, durability_state='durable')\n"
    "e = cb.CommittedStateError('m', certificate=c)\n"
    "a = cb.AuthorityIndeterminateError('m', expected_new='a'*64, expected_previous=None, observed_current='f',"
    " failure_point='t')\n"
    "assert e.certificate is c, 'CommittedStateError debe llevar el certificado'\n"
    "assert a.retry_safe is False, 'AuthorityIndeterminateError.retry_safe debe ser False'\n"
    "assert not hasattr(cb.CommittedStateError, 'authority_crossed'), 'sin bool authority_crossed de clase'\n"
    "print('TYPES_OK')\n"
)

# B228: probe de COMPORTAMIENTO REAL (no un contrato de tipos). Inyecta el escenario decisivo por FALLA REAL —
# CAS cruza a B sobre A, la certificación falla y la compensación TAMBIÉN falla → la reconciliación fd-bound observa
# CURRENT ya cruzado a B y eleva CommittedStateError CON un certificado durable real; NO hay rollback (CURRENT sigue en
# B, jamás restaurado a A). Corre por SUBPROCESO para que el módulo del smoke no importe la maquinaria online.
_COMPENSATION_FAILURE_PROBE = r"""
import hashlib, json, os, shutil, tempfile
import tools.campaign_bundle as cb
R = os.path.dirname(os.path.dirname(os.path.abspath(cb.__file__)))
COLS = json.load(open(os.path.join(R, "security", "campaign_bundle_contract.json")))["columns"]
HDR = ",".join(COLS); H = hashlib.sha256(b"x").hexdigest()
INPUTS = [f"aq_pool_{k}_{t}_{b}.csv" for k in ("nongbm","gbm") for t in ("FAD","DFF") for b in ("family","employment")]
CAMP = [f"campaign_pool_{t}_{b}.csv" for t in ("FAD","DFF") for b in ("family","employment")]
EVAL = ["model_comparison_FAD21.csv","model_comparison_EB_FAD21.csv","model_comparison_DFF21.csv","model_comparison_EB_DFF21.csv"]
def _csv(tag): return (HDR + "\n" + ",".join([str(tag)] + ["0"]*(len(COLS)-1)) + "\n").encode()
def outs(sfx=""):
    o = [{"label":"campaign","name":n,"bytes":_csv("c"+n+sfx),"rows":1,"cols":len(COLS)} for n in CAMP]
    return o + [{"label":"eval","name":n,"bytes":_csv("e"+n+sfx),"rows":1,"cols":len(COLS)} for n in EVAL]
def ins(): return [{"name":n,"bytes":b"col\n"+n.encode()+b"\n"} for n in INPUTS]
def prov():
    base = {"mode":"test","git_head":None,"git_tree":None,"git_dirty":None,"env_id":None,
        "code_sha_merge_campaign_pools":H,"code_sha_campaign_bundle":H,"code_sha_atomic_fs":H,
        "code_sha_governed_read":H,"code_sha_execution_contract":None,"csv_contract_sha256":cb._CSV_CONTRACT_SHA256,
        "journal_heads":{},"python":"3.14.2","platform":"darwin","profile":None,"variant":None}
    return {k: base[k] for k in cb._REQUIRED_PROVENANCE}
d = tempfile.mkdtemp(prefix="cbcinj.", dir="/tmp"); cfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
try:
    a = cb.build_and_commit(cfd, "tx.a", "campA", outs(), ins(), prov())            # CURRENT = A
    cb._certify_current = lambda *A, **K: (_ for _ in ()).throw(cb.BundleValidationError("inject-certify-fail"))
    cb._compensate = lambda *A, **K: (_ for _ in ()).throw(cb.BundleError("inject-compensate-fail"))
    raised = None
    with cb.prepare_bundle(cfd, "tx.b", "campA", outs("z"), ins(), prov()) as prep:  # B != A (txid+contenido)
        b = prep.bundle_id
        try:
            cb.commit_current(prep)
        except cb.CommittedStateError as e:
            raised = e
    assert raised is not None, "esperaba CommittedStateError (CAS cruzado + compensacion fallida)"
    cert = raised.certificate
    assert isinstance(cert, cb.CommitCertificate) and cert.durability_state == "durable", "cert real durable"
    assert cert.bundle_id == b and cert.previous_bundle_id == a, "el cert liga B sobre A"
    cur = cb._read_current(cfd)[0]
    assert cur["bundle_id"] == b and cur["previous_bundle_id"] == a, "CURRENT quedo cruzado a B (sin rollback)"
    print("COMPENSATION_FAILURE_OK")
finally:
    os.close(cfd); shutil.rmtree(d, ignore_errors=True)
"""


def _probe_ok(program: str, token: str) -> bool:
    r = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ROOT},
        capture_output=True,
        text=True,  # fmt: skip
    )
    if r.returncode != 0 or token not in r.stdout:
        sys.stderr.write(f"probe {token} falló (rc={r.returncode}): {r.stderr[:400]}\n")
        return False
    return True


def _compensation_failure_ok() -> bool:
    """B228: ejerce el escenario decisivo por FALLA REAL inyectada (no un contrato de tipos): CAS cruzado + certificación
    falla + compensación falla → `CommittedStateError` con certificado durable real y CURRENT cruzado, SIN rollback."""
    return _probe_ok(_COMPENSATION_FAILURE_PROBE, "COMPENSATION_FAILURE_OK")


def _type_contract_ok() -> bool:
    """Contrato de TIPOS que la clasificación por-tipo del merge exige: CommittedStateError lleva el certificado,
    AuthorityIndeterminateError lleva retry_safe=False y no hay bool `authority_crossed` de clase."""
    return _probe_ok(_TYPE_CONTRACT_PROBE, "TYPES_OK")


def _git_sha() -> str:
    try:
        r = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except OSError:
        return "unknown"


def run_contract() -> tuple[bool, dict]:
    ws = tempfile.mkdtemp(prefix="cbc.", dir="/tmp")
    steps: list[dict] = []
    ok = True
    try:
        camp = os.path.join(ws, "reports", "campaign")
        os.makedirs(camp)
        os.makedirs(os.path.join(ws, "reports", "eval"))
        halves = [f for f in os.listdir(os.path.join(ROOT, "reports", "campaign")) if f.startswith("aq_pool_")]
        if len(halves) != 8:
            return False, {"error": f"esperaba 8 mitades reales, hay {len(halves)}"}
        for f in halves:
            shutil.copy(os.path.join(ROOT, "reports", "campaign", f), camp)

        _rewrite_run_id(camp, "cbcA")  # run_id == CAMPAIGN_ID (sin mezcla de campañas)
        if _run_merge(ws, "cbcA") != 0:
            return False, {"error": "merge #1 falló"}
        rc1, rep1 = _validate(ws)
        b1 = rep1.get("bundle_id")
        step1_ok = rc1 == 0 and rep1.get("status") == "valid" and rep1.get("previous_bundle_id") is None
        steps.append({"step": "merge1_no_prev", "rc": rc1, "ok": step1_ok, "bundle_id": b1})
        ok = ok and step1_ok

        _rewrite_run_id(camp, "cbcB")  # bundle DISTINTO → EXCHANGE sobre la autoridad previa
        if _run_merge(ws, "cbcB") != 0:
            return False, {"error": "merge #2 (exchange) falló", "steps": steps}
        rc2, rep2 = _validate(ws)
        b2 = rep2.get("bundle_id")
        step2_ok = rc2 == 0 and rep2.get("status") == "valid" and rep2.get("previous_bundle_id") == b1 and b2 != b1
        steps.append({"step": "merge2_exchange", "rc": rc2, "ok": step2_ok, "bundle_id": b2, "prev": rep2.get("previous_bundle_id")})  # fmt: skip
        ok = ok and step2_ok

        pointer = os.path.join(camp, ".merge-CURRENT")  # NEGATIVO obligatorio: corromper el puntero → DEBE fallar
        os.chmod(pointer, 0o600)
        with open(pointer, "w", encoding="utf-8") as fh:
            fh.write("corrupted-not-json")
        rc3, rep3 = _validate(ws)
        step3_ok = rc3 != 0 and rep3.get("status") != "valid"
        steps.append({"step": "negative_corrupt_pointer", "rc": rc3, "ok": step3_ok})
        ok = ok and step3_ok

        step4_ok = _compensation_failure_ok()  # B228: CAS cruzado + certificación falla + compensación falla (REAL)
        steps.append({"step": "negative_compensation_failure_no_rollback", "ok": step4_ok})
        ok = ok and step4_ok

        step5_ok = _type_contract_ok()  # contrato de tipos que la clasificación por-tipo exige
        steps.append({"step": "type_contract", "ok": step5_ok})
        ok = ok and step5_ok
        return ok, {"git_sha": _git_sha(), "steps": steps}
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--receipt", default=None, help="ruta para escribir el recibo JSON del contrato")
    args = ap.parse_args()
    ok, report = run_contract()
    report["contract_ok"] = ok
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    report["receipt_sha256"] = hashlib.sha256(payload).hexdigest()
    text = json.dumps(report, indent=2, sort_keys=True)
    sys.stdout.write(text + "\n")
    if args.receipt:
        with open(args.receipt, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
