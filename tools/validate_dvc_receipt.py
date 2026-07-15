#!/usr/bin/env python
"""Validador ESTRICTO de recibos dvc-tool (P0R.5, B17). CI ya no solo mira `smoke_ok`: cruza el recibo
contra el estado del repo y (en modo `--cross`) la igualdad del DAG canónico entre plataformas.

  python -m tools.validate_dvc_receipt --receipt r.json          # un recibo contra el repo actual
  python -m tools.validate_dvc_receipt --cross r_linux r_macos    # coherencia cross-plataforma

Exige: esquema exacto; `smoke_ok`; árbol limpio (`git_dirty=false`); `source_head_sha` presente (y ==
GITHUB_PR_HEAD_SHA si está); `dvc_status_returncode`/`dag_returncode`==0; `site_cache_confined`;
`version_ok`; `sbom_sha256` no-null; `sbom_component_count==n_packages`; y que `lock_sha256`/
`lockset_sha256`/`dvc_in_sha256` casen los ficheros ACTUALES del repo. Fail-closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from tools import lock_contracts as lc

ROOT = lc.ROOT
_KEYS = {
    "schema_version",
    "profile",
    "source_head_sha",
    "checkout_sha",
    "checkout_tree_sha",
    "base_sha",
    "git_dirty",
    "github_run_id",
    "github_run_attempt",
    "env_id",
    "python",
    "platform",
    "lock",
    "lock_sha256",
    "lockset_sha256",
    "dvc_in_sha256",
    "expected",
    "observed",
    "version_ok",
    "pip_check",
    "cache_guard",
    "site_cache_dir",
    "site_cache_confined",
    "dag_returncode",
    "dag_hash",
    "dvc_status_returncode",
    "inventory_digest",
    "n_packages",
    "sbom_component_count",
    "sbom_sha256",
    "smoke_ok",
}


def _sha(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _no_dup(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"clave duplicada {k}")
        d[k] = v
    return d


def validate(path: Path) -> list[str]:
    probs: list[str] = []
    try:
        r = json.loads(path.read_text(), object_pairs_hook=_no_dup)
    except (OSError, ValueError) as exc:
        return [f"{path.name}: ilegible/duplicado ({exc})"]
    if set(r) != _KEYS:
        probs.append(f"{path.name}: esquema {sorted(set(r) ^ _KEYS)} distinto del exacto")
        return probs  # sin esquema exacto no seguimos
    if r["schema_version"] != 2:
        probs.append(f"{path.name}: schema_version != 2")
    for flag in ("smoke_ok", "version_ok", "site_cache_confined"):
        if r[flag] is not True:
            probs.append(f"{path.name}: {flag} != true")
    if r["git_dirty"] is not False:
        probs.append(f"{path.name}: git_dirty != false")
    if not r["source_head_sha"]:
        probs.append(f"{path.name}: source_head_sha vacío")
    env_head = os.environ.get("GITHUB_PR_HEAD_SHA")
    if env_head and r["source_head_sha"] != env_head:
        probs.append(f"{path.name}: source_head_sha {r['source_head_sha']} != GITHUB_PR_HEAD_SHA {env_head}")
    for rc in ("dag_returncode", "dvc_status_returncode"):
        if r[rc] != 0:
            probs.append(f"{path.name}: {rc} != 0")
    if not r["sbom_sha256"]:
        probs.append(f"{path.name}: sbom_sha256 null")
    if r["sbom_component_count"] != r["n_packages"]:
        probs.append(f"{path.name}: sbom_component_count {r['sbom_component_count']} != n_packages {r['n_packages']}")
    if r["cache_guard"] != "ok":
        probs.append(f"{path.name}: cache_guard != ok")
    # hashes contra el repo ACTUAL
    for field, target in (
        ("lock_sha256", ROOT / r["lock"]),
        ("lockset_sha256", ROOT / lc.MANIFEST_REL),
        ("dvc_in_sha256", ROOT / "requirements/dvc.in"),
    ):
        if target.exists() and r[field] != _sha(target):
            probs.append(f"{path.name}: {field} no casa el fichero actual {target.name}")
    return probs


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt")
    ap.add_argument("--cross", nargs=2, metavar=("R1", "R2"))
    ns = ap.parse_args(argv[1:])
    probs: list[str] = []
    targets = []
    if ns.receipt:
        targets.append(Path(ns.receipt))
    if ns.cross:
        targets += [Path(ns.cross[0]), Path(ns.cross[1])]
    if not targets:
        ap.error("da --receipt o --cross")
    for t in targets:
        probs += validate(t)
    if ns.cross and not probs:
        h1 = json.loads(Path(ns.cross[0]).read_text())["dag_hash"]
        h2 = json.loads(Path(ns.cross[1]).read_text())["dag_hash"]
        if h1 != h2:
            probs.append(f"dag_hash difiere entre plataformas: {h1} != {h2}")
    if probs:
        print("✗ RECIBO(S) dvc-tool inválido(s):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ recibo(s) dvc-tool válido(s): {[t.name for t in targets]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
