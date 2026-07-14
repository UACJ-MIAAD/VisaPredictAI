#!/usr/bin/env python
"""Smoke REAL de un lock deep instalado (P0R.4R). Se ejecuta SOLO en el job `deep-lock-install`
de CI (los deps deep están instalados en ese runner), NO en el CI base.

  python -m tools.deep_smoke --lock locks/deep-linux-x86_64-cpu.txt --torch 2.12.1+cpu \
      --variant linux-cpu --receipt deep-receipt-linux-cpu.json

Verifica: Python 3.14.x; versión EXACTA de cada dist del stack contra el pin del lock; torch con la
variante esperada; que todo el stack IMPORTA; y una operación tensorial FINITA y DETERMINISTA.
Emite un receipt JSON (entorno + versiones + checksum del tensor). Falla (exit 1) ante cualquier
discrepancia. Reusa el parser único del contrato (tools/lock_contracts.pin_map)."""

from __future__ import annotations

import argparse
import importlib
import json
import platform
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


def run(lock: Path, expected_torch: str, variant: str) -> tuple[list[str], dict]:
    probs: list[str] = []
    if not platform.python_version().startswith("3.14."):
        probs.append(f"Python {platform.python_version()} no es 3.14.x")
    pins = lc.pin_map(lock.read_text())
    versions: dict[str, str] = {}
    for dist in STACK.values():
        v = version(dist)
        versions[dist] = v
        pinned = pins.get(lc._norm(dist))
        # torch en el lock lleva sufijo local (+cpu/+cu126); se compara aparte contra expected_torch.
        if dist == "torch":
            continue
        if pinned != v:
            probs.append(f"{dist} instalado {v} != lock {pinned}")
    for import_name in STACK:
        importlib.import_module(import_name)  # un import fallido del stack deep aborta ruidosamente
    import torch  # el stack ya se importó arriba; aquí se usa para el tensor

    if torch.__version__ != expected_torch:
        probs.append(f"torch {torch.__version__} != esperado {expected_torch}")
    torch.manual_seed(0)
    t = torch.arange(6, dtype=torch.float32).reshape(2, 3)
    prod = t @ t.T
    if not bool(torch.isfinite(prod).all()):
        probs.append("operación tensorial produjo valores no finitos")
    checksum = round(float(prod.sum().item()), 4)
    # t=[[0,1,2],[3,4,5]]; t@t.T=[[5,14],[14,50]]; suma=83 (determinista en cualquier plataforma).
    if checksum != 83.0:
        probs.append(f"checksum tensorial {checksum} != 83.0 (no determinista)")
    receipt = {
        "variant": variant,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "torch": torch.__version__,
        "versions": versions,
        "tensor_checksum": checksum,
        "lock": str(lock),
    }
    return probs, receipt


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", required=True)
    ap.add_argument("--torch", required=True, help="versión torch esperada (p. ej. 2.12.1+cpu)")
    ap.add_argument("--variant", required=True)
    ap.add_argument("--receipt", required=True)
    ns = ap.parse_args(argv[1:])
    probs, receipt = run(Path(ns.lock), ns.torch, ns.variant)
    if probs:
        print(f"✗ DEEP SMOKE ({ns.variant}) falló ({len(probs)}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    Path(ns.receipt).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(
        f"✓ deep smoke OK ({ns.variant}): torch {receipt['torch']} · {len(receipt['versions'])} dists · tensor finito"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
