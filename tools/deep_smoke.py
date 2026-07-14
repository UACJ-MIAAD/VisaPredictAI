#!/usr/bin/env python
"""Smoke REAL de un lock deep instalado (P0R.4R / P0R.4R2). Se ejecuta SOLO en el job
`deep-lock-install` de CI (los deps deep están instalados en ese runner), NO en el CI base.

  python -m tools.deep_smoke --lock locks/deep-linux-x86_64-cpu.txt --receipt deep-receipt-linux-cpu.json

La expectativa (variante/plataforma/torch) NO viene del workflow: se DERIVA del contrato único
(lock_contracts.DEEP_RUNTIME), para que la matriz de CI no pueda autoconfirmarse. Verifica:
contrato de lockset OK; Python 3.14.x; plataforma observada == esperada; versión EXACTA de cada
dist del stack contra el pin del lock; torch con la variante esperada; que todo el stack IMPORTA;
`pip check` exit 0; y un tensor finito determinista (83.0). Emite un receipt LIGADO al lock
(sha256 del lock + del manifiesto + commit + esperado-vs-observado + pip_check) SOLO si todo pasa."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from tools import lock_contracts as lc

# import-name -> distribution-name (para importlib.metadata.version y el pin del lock)
STACK: dict[str, str] = {
    "torch": "torch",
    "neuralforecast": "neuralforecast",
    "chronos": "chronos-forecasting",
    "mlflow": "mlflow",
    "optuna": "optuna",
    "ray": "ray",
    "pandas": "pandas",
    "transformers": "transformers",
}


def _sha256(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _commit_sha() -> str:
    env = os.environ.get("GITHUB_SHA")
    if env:
        return env
    out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return out.stdout.strip() if out.returncode == 0 else "unknown"


def evaluate(
    lock_rel: str,
    *,
    py_version: str,
    system: str,
    machine: str,
    installed: dict[str, str],
    torch_version: str,
    pip_check_ok: bool,
    checksum: float,
) -> tuple[list[str], dict]:
    """Lógica PURA del smoke (sin importar el stack deep) — testeable con valores inyectados."""
    if lock_rel not in lc.DEEP_RUNTIME:
        return [f"lock no gobernado: {lock_rel} (no está en DEEP_RUNTIME)"], {}
    rt = lc.DEEP_RUNTIME[lock_rel]
    probs: list[str] = [f"[contrato] {p}" for p in lc.validate_all(lc.ROOT)]
    if not py_version.startswith("3.14."):
        probs.append(f"Python {py_version} no es 3.14.x")
    if system != rt["system"] or machine != rt["machine"]:
        probs.append(f"plataforma {system} {machine} != esperada {rt['system']} {rt['machine']}")
    pins = lc.pin_map((lc.ROOT / lock_rel).read_text())
    for dist, v in installed.items():
        if dist == "torch":  # torch lleva sufijo local; se compara contra rt["torch"] aparte
            continue
        if pins.get(lc._norm(dist)) != v:
            probs.append(f"{dist} instalado {v} != lock {pins.get(lc._norm(dist))}")
    if torch_version != rt["torch"]:
        probs.append(f"torch {torch_version} != esperado {rt['torch']}")
    if not pip_check_ok:
        probs.append("pip check rojo")
    # t=[[0,1,2],[3,4,5]]; t@t.T=[[5,14],[14,50]]; suma=83 (determinista en cualquier plataforma).
    if checksum != 83.0:
        probs.append(f"checksum tensorial {checksum} != 83.0 (no determinista)")
    receipt = {
        "commit_sha": _commit_sha(),
        "lock": lock_rel,
        "lock_sha256": _sha256(lc.ROOT / lock_rel),
        "manifest_sha256": _sha256(lc.ROOT / lc.MANIFEST_REL),
        "variant_expected": rt["variant"],
        "platform_expected": f"{rt['system']} {rt['machine']}",
        "platform_observed": f"{system} {machine}",
        "python": py_version,
        "torch_expected": rt["torch"],
        "torch_observed": torch_version,
        "pip_check": "ok" if pip_check_ok else "fail",
        "versions": installed,
        "tensor_checksum": checksum,
    }
    return probs, receipt


def run(lock_rel: str) -> tuple[list[str], dict]:
    """Recoge el estado REAL del entorno deep instalado y delega a evaluate()."""
    installed = {dist: version(dist) for dist in STACK.values()}
    for import_name in STACK:
        importlib.import_module(import_name)  # un import fallido del stack deep aborta ruidosamente
    import torch

    torch.manual_seed(0)
    prod = torch.arange(6, dtype=torch.float32).reshape(2, 3) @ torch.arange(6, dtype=torch.float32).reshape(2, 3).T
    finite = bool(torch.isfinite(prod).all())
    checksum = round(float(prod.sum().item()), 4) if finite else float("nan")
    pip_ok = (
        subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, check=False).returncode
        == 0
    )
    return evaluate(
        lock_rel,
        py_version=platform.python_version(),
        system=platform.system(),
        machine=platform.machine(),
        installed=installed,
        torch_version=torch.__version__,
        pip_check_ok=pip_ok,
        checksum=checksum,
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", required=True)
    ap.add_argument("--receipt", required=True)
    ns = ap.parse_args(argv[1:])
    probs, receipt = run(ns.lock)
    if probs:
        print(f"✗ DEEP SMOKE ({ns.lock}) falló ({len(probs)}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    Path(ns.receipt).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(
        f"✓ deep smoke OK ({receipt['variant_expected']}): torch {receipt['torch_observed']} · pip check ok · tensor 83.0"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
