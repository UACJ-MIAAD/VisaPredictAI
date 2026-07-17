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
