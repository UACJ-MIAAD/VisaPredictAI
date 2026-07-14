#!/usr/bin/env python
"""Smoke + recibo gobernado del entorno dvc-tool (P0R.5, R5/C5). Construye el entorno content-addressed,
ejecuta DVC AISLADO por la interfaz única y emite un recibo que LIGA la corrida a la PROCEDENCIA git
(head fuente vs checkout vs base), al lockset, al entorno y a las versiones OBSERVADAS de
dvc/dvc-s3/diskcache, con un SBOM a NIVEL DE ENTORNO (no del lock) cuyo sha se ancla al recibo.

  python -m tools.dvc_tool_smoke --receipt dvc-tool-receipt.json [--sbom sbom-dvc-tool.env.json]

`smoke_ok` exige: entorno válido, `pip check`, versiones == contrato, cache guard limpio, `dvc dag` y
`dvc status` con returncode 0. El recibo se escribe ATÓMICAMENTE (staging→fsync→rename) tras TODOS los
checks. `dag_hash` es CANÓNICO (aristas+nodos ordenados) ⇒ estable cross-plataforma.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from tools import lock_contracts as lc
from tools import python_env as pe

ROOT = lc.ROOT
_DOT_EDGE = re.compile(r'"([^"]+)"\s*->\s*"([^"]+)"')
_DOT_NODE = re.compile(r'^\s*"([^"]+)"\s*;?\s*$', re.MULTILINE)


def _sha256_path(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _canonical_dag_hash(dot: str) -> str:
    """Hash del DAG independiente del layout: conjunto ORDENADO de aristas y nodos (el `dvc dag --dot`
    varía en orden/layout entre plataformas; esto lo canonicaliza)."""
    edges = sorted(f"{a}->{b}" for a, b in _DOT_EDGE.findall(dot))
    nodes = sorted(set(_DOT_NODE.findall(dot)) | {n for e in _DOT_EDGE.findall(dot) for n in e})
    payload = json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _env_sbom(inventory: list[str]) -> dict:
    """SBOM CycloneDX mínimo a NIVEL DE ENTORNO (inventario real sellado, no el lock)."""
    comps = []
    for line in inventory:
        if "==" in line:
            name, ver = line.split("==", 1)
            comps.append({"type": "library", "name": name, "version": ver, "purl": f"pkg:pypi/{name.lower()}@{ver}"})
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": sorted(comps, key=lambda c: c["name"].lower()),
    }


def _atomic_write(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def build_receipt(sbom_path: Path | None) -> dict:
    from tools import dvc_cache_guard

    profile = "dvc-tool"
    env_path = pe.build(profile)  # transaccional; valida contrato + pip check + inventario + file hashes
    ready = json.loads((env_path / "READY.json").read_text())
    lock_rel = pe.lock_rel_for(profile)

    expected = {**lc.DVC_TOOL_DIRECT, "diskcache": lc.DVC_TOOL_DISKCACHE}
    observed = {}
    for line in ready["inventory"]:
        if "==" in line:
            n, v = line.split("==", 1)
            if pe._canon(n) in expected:
                observed[pe._canon(n)] = v
    version_ok = all(observed.get(k) == v for k, v in expected.items())

    guard_probs = dvc_cache_guard.check(ROOT)
    dag = pe.run(profile, ["dvc", "dag", "--dot"], capture=True)
    status = pe.run(profile, ["dvc", "status", "--json"], capture=True)

    # SBOM a nivel de entorno (inventario real) + sha anclado
    sbom = _env_sbom(ready["inventory"])
    sbom_json = json.dumps(sbom, indent=2, sort_keys=True) + "\n"
    sbom_sha = "sha256:" + hashlib.sha256(sbom_json.encode()).hexdigest()
    if sbom_path is not None:
        _atomic_write(sbom_path, sbom_json)

    smoke_ok = bool(
        version_ok and ready["pip_check"] == "ok" and not guard_probs and dag.returncode == 0 and status.returncode == 0
    )
    return {
        "schema_version": 2,
        "profile": profile,
        **pe.provenance(),
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
        "dag_returncode": dag.returncode,
        "dag_hash": _canonical_dag_hash(dag.stdout or ""),
        "dvc_status_returncode": status.returncode,
        "inventory_digest": ready["inventory_digest"],
        "n_packages": ready["n_packages"],
        "sbom_component_count": len(sbom["components"]),
        "sbom_sha256": sbom_sha,
        "smoke_ok": smoke_ok,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--sbom")
    ns = ap.parse_args(argv[1:])
    receipt = build_receipt(Path(ns.sbom) if ns.sbom else None)
    _atomic_write(Path(ns.receipt), json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {k: receipt[k] for k in ("profile", "env_id", "source_head_sha", "version_ok", "smoke_ok", "sbom_sha256")},
            indent=2,
        )
    )
    if not receipt["smoke_ok"]:
        print("✗ dvc-tool smoke FALLÓ", file=__import__("sys").stderr)
        return 1
    print(f"✓ dvc-tool smoke OK · recibo → {ns.receipt}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv))
