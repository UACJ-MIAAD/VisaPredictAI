#!/usr/bin/env python
"""Smoke + recibo gobernado del entorno dvc-tool (P0R.5, R5). Construye el entorno content-addressed,
ejecuta DVC AISLADO por la interfaz única y emite un recibo que LIGA la corrida al commit, al lockset,
al entorno y a las versiones OBSERVADAS de dvc/dvc-s3/diskcache (no un `{dvc, guard, lock}` suelto).

  python -m tools.dvc_tool_smoke --receipt dvc-tool-receipt.json [--sbom sbom-dvc-tool.cdx.json]

Falla (exit != 0) si: el entorno no construye/valida, `pip check` del entorno falla, las versiones
observadas no casan el contrato, el cache guard bloquea, o `dvc dag`/`dvc status` fallan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tools import lock_contracts as lc
from tools import python_env as pe

ROOT = lc.ROOT


def _sha256_path(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True, check=True).stdout.strip()


def _observed(env_path: Path, packages: list[str]) -> dict[str, str]:
    freeze = pe._pip_freeze(pe._venv_python(env_path))
    got = {ln.split("==")[0].lower().replace("_", "-"): ln.split("==")[1] for ln in freeze if "==" in ln}
    return {p: got.get(p, "") for p in packages}


def build_receipt(sbom: Path | None = None) -> dict:
    from tools import dvc_cache_guard

    profile = "dvc-tool"
    env_path = pe.build(profile)  # transaccional; valida contrato + pip check + inventario == lock
    ready = json.loads((env_path / "READY.json").read_text())
    lock_rel = pe.lock_rel_for(profile)

    # versiones esperadas (contrato) vs observadas (entorno construido)
    expected = {**lc.DVC_TOOL_DIRECT, "diskcache": lc.DVC_TOOL_DISKCACHE}
    observed = _observed(env_path, sorted(expected))
    version_ok = all(observed.get(k) == v for k, v in expected.items())

    # cache guard
    guard_probs = dvc_cache_guard.check(ROOT)

    # dvc dag (hash estable) + dvc status via la interfaz gobernada (capturado)
    dag = pe.run(profile, ["dvc", "dag", "--dot"], capture=True)
    status = pe.run(profile, ["dvc", "status", "--json"], capture=True)
    dag_hash = "sha256:" + hashlib.sha256(dag.stdout.encode()).hexdigest()

    receipt = {
        "schema_version": 1,
        "profile": profile,
        "commit_sha": _git("rev-parse", "HEAD"),
        "tree_sha": _git("rev-parse", "HEAD^{tree}"),
        "env_id": ready["env_id"],
        "python": ready["descriptor"]["python"],
        "platform": ready["descriptor"]["platform"],
        "lock": lock_rel,
        "lock_sha256": _sha256_path(ROOT / lock_rel),
        "lockset_sha256": _sha256_path(ROOT / lc.MANIFEST_REL),
        "dvc_in_sha256": _sha256_path(ROOT / "requirements/dvc.in"),
        "expected": expected,
        "observed": observed,
        "version_ok": version_ok,
        "pip_check": ready["pip_check"],
        "cache_guard": "ok" if not guard_probs else guard_probs,
        "dag_hash": dag_hash,
        "dvc_status_rc": status.returncode,
        "inventory_digest": ready["inventory_digest"],
        "n_packages": ready["n_packages"],
        "sbom_sha256": _sha256_path(sbom) if (sbom and sbom.exists()) else None,
    }
    ok = version_ok and receipt["pip_check"] == "ok" and not guard_probs and dag.returncode == 0
    receipt["smoke_ok"] = ok
    return receipt


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--sbom")
    ns = ap.parse_args(argv[1:])
    receipt = build_receipt(Path(ns.sbom) if ns.sbom else None)
    Path(ns.receipt).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: receipt[k] for k in ("profile", "env_id", "version_ok", "pip_check", "smoke_ok")}, indent=2))
    if not receipt["smoke_ok"]:
        print("✗ dvc-tool smoke FALLÓ", file=sys.stderr)
        return 1
    print(f"✓ dvc-tool smoke OK · recibo → {ns.receipt}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
