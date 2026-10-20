"""Preflight de los dos intérpretes de sync_all; no prepara ni publica artefactos.

publish usa el perfil model (lectores E3/CRPS de la reconciliación). prepare usa el
entorno tracking con MLflow. Los entornos son distintos: MLflow requiere pandas<3.
Se importan los lectores reales, no sólo sus nombres en un inventario de paquetes.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


def preflight(profile: str) -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "experiments"))
    sys.path.insert(0, str(root))
    if profile == "publish":
        for name in ("tools.campaign_txn", "vp_model.champion", "score_e3_campaign"):
            importlib.import_module(name)
    elif profile == "prepare":
        importlib.import_module("sync_mlflow")
    else:
        raise ValueError(f"perfil desconocido: {profile}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("publish", "prepare"), required=True)
    args = parser.parse_args()
    try:
        preflight(args.profile)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        print(
            f"preflight {args.profile}: ERROR {type(exc).__name__}: {exc}; "
            f"intérprete={sys.executable}; no se prepara ni publica nada",
            file=sys.stderr,
        )
        return 12
    print(f"preflight {args.profile}: OK ({sys.executable})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
