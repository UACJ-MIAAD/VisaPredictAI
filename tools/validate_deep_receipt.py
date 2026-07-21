#!/usr/bin/env python
"""B328/B333/B334: validador REALMENTE INDEPENDIENTE de un recibo de deep smoke — NO confía en el auto-reporte del productor.

  python -m tools.validate_deep_receipt --receipt R.json --lock locks/deep-linux-x86_64-cpu.txt --expected-variant linux-cpu

Re-deriva todo y REOBSERVA el entorno real:
- commit: git ABSOLUTO, `HEAD` 40-hex, `cat-file -e`, `== GITHUB_SHA`. **B333: si `HEAD` no se resuelve es ROJO** (antes,
  con `git_head=None`, un commit/Python/orígenes fabricados se ACEPTABAN — fail-open).
- Python/plataforma: comparados contra `platform.python_version/system/machine` REOBSERVADOS (no sólo contra la expectativa).
- lock/manifiesto/contrato: hashes desde BYTES GOBERNADOS (`GovernanceSnapshot`), pins parseados de esos MISMOS bytes (sin
  `Path.read_text()/.exists()` en la decisión).
- inventario: REIMPORTADO/REOBSERVADO (`deep_smoke.observe_runtime`) y cruzado módulo/dist/versión/origen/`origin_sha256`.
- esquema EXACTO, tipos exactos, `bool` no numérico, `pip_check == ok`, checksum tensorial 83 finito.

Lectura del recibo: **B334** vía `governed_receipt_io.read_receipt_bytes` (NOMBRE SIMPLE en directorio autorizado, leaf
`O_NOFOLLOW` relativo a un fd de directorio — ningún ANCESTRO symlink lo desvía). Se cablea en CI ENTRE el smoke y el
upload; un recibo verde con validador rojo aborta. Toda falla de open/close/fsync/git/import/plataforma es ROJA (nunca skip)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

from tools import deep_smoke as ds
from tools import governed_receipt_io as grio
from tools import lock_contracts as lc
from tools.governance_snapshot import GovernanceSnapshotError

_RECEIPT_KEYS = {
    "commit_sha",
    "lock",
    "lock_sha256",
    "manifest_sha256",
    "deep_smoke_contract_sha256",
    "variant_expected",
    "platform_expected",
    "platform_observed",
    "python",
    "torch_expected",
    "torch_observed",
    "pip_check",
    "versions",
    "imports",
    "tensor_checksum",
}
_IMPORT_KEYS = {"module", "distribution", "origin", "origin_sha256"}  # B332: el origen lleva su sha256 gobernado
_HEX40 = re.compile(r"[0-9a-f]{40}")
_PY_RE = re.compile(r"3\.14\.\d+")


def receipt_problems(
    receipt: object,
    *,
    lock_rel: str,
    expected_variant: str,
    contract: ds.DeepSmokeContract,
    lock_sha: str,
    manifest_sha: str,
    pins: dict[str, str],
    git_head: str | None,
    github_sha: str | None,
    real_python: str,
    real_system: str,
    real_machine: str,
    observed: dict[str, dict[str, str | None]],
) -> list[str]:
    """B328/B333: núcleo PURO/testeable — todo re-derivado o REOBSERVADO se inyecta; jamás se confía en el recibo. `git_head`
    es el HEAD resuelto por git (None ⇒ ROJO, B333); `pins` proviene de los MISMOS bytes gobernados del lock; `real_*` es la
    plataforma reobservada; `observed` es el inventario reobservado `{module: {distribution, version, origin, origin_sha256}}`."""
    if type(receipt) is not dict:
        return ["recibo no es un objeto JSON"]
    if set(receipt) != _RECEIPT_KEYS:
        return [f"claves del recibo != esquema exacto (faltan {_RECEIPT_KEYS - set(receipt)}, sobran {set(receipt) - _RECEIPT_KEYS})"]  # fmt: skip
    if lock_rel not in lc.DEEP_RUNTIME:
        return [f"lock no gobernado: {lock_rel}"]
    rt = lc.DEEP_RUNTIME[lock_rel]
    probs: list[str] = []
    if git_head is None:  # B333: sin HEAD resuelto NO se puede validar la procedencia → ROJO (antes se aceptaba)
        probs.append("HEAD no resuelto por git — la procedencia no puede validarse (fail-closed B333)")
    commit = receipt["commit_sha"]
    if type(commit) is not str or not _HEX40.fullmatch(commit):
        probs.append(f"commit_sha {commit!r} no es 40-hex")
    elif git_head is not None and commit != git_head:
        probs.append(f"commit_sha {commit} != HEAD {git_head}")
    if github_sha is not None and (git_head is None or github_sha != git_head):
        probs.append(f"GITHUB_SHA {github_sha} no coincide con HEAD {git_head!r}")
    if type(receipt["python"]) is not str or not _PY_RE.fullmatch(receipt["python"]):
        probs.append(f"python {receipt['python']!r} no es exactamente 3.14.Z")
    elif receipt["python"] != real_python:  # B333/§10.3: contra la plataforma REOBSERVADA, no sólo contra el patrón
        probs.append(f"python {receipt['python']!r} != reobservado {real_python!r}")
    if receipt["variant_expected"] != expected_variant or rt["variant"] != expected_variant:
        probs.append(f"variante {receipt['variant_expected']!r} != esperada {expected_variant!r}")
    plat_expected = f"{rt['system']} {rt['machine']}"
    plat_real = f"{real_system} {real_machine}"
    if receipt["platform_expected"] != plat_expected:
        probs.append(f"platform_expected {receipt['platform_expected']!r} != {plat_expected!r}")
    if receipt["platform_observed"] != plat_real:  # contra la plataforma REOBSERVADA
        probs.append(f"platform_observed {receipt['platform_observed']!r} != reobservado {plat_real!r}")
    if receipt["lock"] != lock_rel:
        probs.append(f"lock del recibo {receipt['lock']!r} != {lock_rel!r}")
    if receipt["lock_sha256"] != lock_sha:
        probs.append("lock_sha256 no coincide con los bytes gobernados del lock")
    if receipt["manifest_sha256"] != manifest_sha:
        probs.append("manifest_sha256 no coincide con los bytes gobernados del manifiesto")
    if receipt["deep_smoke_contract_sha256"] != contract.sha256:
        probs.append("deep_smoke_contract_sha256 no coincide con el contrato gobernado")
    if receipt["torch_expected"] != rt["torch"] or receipt["torch_observed"] != rt["torch"]:
        probs.append(f"torch {receipt['torch_observed']!r} != esperado {rt['torch']!r}")
    if receipt["pip_check"] != "ok":
        probs.append(f"pip_check {receipt['pip_check']!r} != ok")
    if type(receipt["tensor_checksum"]) is bool or receipt["tensor_checksum"] != 83.0:
        probs.append(f"tensor_checksum {receipt['tensor_checksum']!r} != 83.0")
    probs.extend(_version_problems(receipt["versions"], contract, rt, pins, observed))
    probs.extend(_import_problems(receipt["imports"], contract, observed))
    return probs


def _version_problems(
    versions: object, contract: ds.DeepSmokeContract, rt: dict, pins: dict[str, str], observed: dict[str, dict]
) -> list[str]:
    expected_dists = [d for _, d in contract.imports]
    if type(versions) is not dict or list(versions) != expected_dists:
        return [
            f"versions {list(versions) if isinstance(versions, dict) else versions!r} != orden canónico del contrato"
        ]
    obs_ver = {rec["distribution"]: rec["version"] for rec in observed.values()}  # dist → versión reobservada
    probs: list[str] = []
    for dist, v in versions.items():
        if type(v) is not str:
            probs.append(f"versión de {dist} no es str: {v!r}")
            continue
        if dist == "torch":
            if v != rt["torch"]:
                probs.append(f"torch en versions {v!r} != {rt['torch']!r}")
        elif pins.get(lc._norm(dist)) != v:
            probs.append(f"{dist} {v!r} != pin del lock {pins.get(lc._norm(dist))!r}")
        if obs_ver.get(dist) != v:  # §10.6: cruce contra el inventario REOBSERVADO
            probs.append(f"{dist} {v!r} != versión reobservada {obs_ver.get(dist)!r}")
    return probs


def _import_problems(records: object, contract: ds.DeepSmokeContract, observed: dict[str, dict]) -> list[str]:
    if type(records) is not list or len(records) != len(contract.imports):
        return [f"imports del recibo != {len(contract.imports)} entradas del contrato"]
    probs: list[str] = []
    for rec, (module, dist) in zip(records, contract.imports, strict=True):
        if type(rec) is not dict or set(rec) != _IMPORT_KEYS:
            probs.append(f"entrada de import {rec!r} != {sorted(_IMPORT_KEYS)}")
            continue
        if rec["module"] != module or rec["distribution"] != dist:
            probs.append(f"import {rec['module']!r}/{rec['distribution']!r} != contrato {module!r}/{dist!r}")
        origin = rec["origin"]
        if type(origin) is not str or os.path.isabs(origin) or os.pardir in origin.split("/") or origin == "unknown":
            probs.append(f"origin {origin!r} no es una ruta relativa simple bajo sys.prefix")
        obs = observed.get(module)  # §10.6: cruce contra la identidad REOBSERVADA por descriptor
        if obs is None:
            probs.append(f"import {module!r} ausente del inventario reobservado")
            continue
        if origin != obs["origin"]:
            probs.append(f"origin de {module} {origin!r} != reobservado {obs['origin']!r}")
        if rec["origin_sha256"] != obs["origin_sha256"] or type(rec["origin_sha256"]) is not str:
            probs.append(f"origin_sha256 de {module} != reobservado (recibo manipulado o desincronizado)")
    return probs


def _git_head() -> str | None:
    """B336: HEAD por la ÚNICA observación git GOBERNADA (`GovernanceSnapshot.head_commit`: toplevel==ROOT,
    `rev-parse --verify HEAD^{commit}`, 40-hex de una línea, git absoluto gobernado). Sin git ad hoc. None si no gobernable
    (que `receipt_problems` trata como ROJO, B333)."""
    from tools.governance_snapshot import GovernanceSnapshot

    try:
        return GovernanceSnapshot(str(lc.ROOT)).head_commit()
    except GovernanceSnapshotError:
        return None


def _governed_bytes(rel: str) -> bytes:
    """Lee `rel` de forma GOBERNADA (sin symlink, modo/uid/nlink exactos, snapshot pre-post) — nunca `Path.read_text/.exists`."""
    from tools.governance_snapshot import GovernanceSnapshot

    with GovernanceSnapshot(str(lc.ROOT)) as snap:
        return snap.read(rel, category="source").data


def _reobserve(lock_rel: str) -> tuple[list[str], dict[str, dict[str, str | None]], str, str, str]:
    """§10.6/§10.8: REOBSERVA el entorno real (importa el stack, identidad por descriptor) vía `deep_smoke.observe_runtime`.
    Devuelve `(problemas, observed, real_python, real_system, real_machine)`. Cualquier falla es ROJA (nunca skip)."""
    obs_probs, observation = ds.observe_runtime(lock_rel)
    if observation is None:
        return ["reobservación no produjo observación (fail-closed)"], {}, "", "", ""
    inst = dict(observation.installed)
    observed = {
        module: {"distribution": dist, "version": inst.get(dist), "origin": origin, "origin_sha256": sha}
        for module, dist, origin, sha in observation.import_records
    }
    return list(obs_probs), observed, observation.py_version, observation.system, observation.machine


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--lock", required=True)
    ap.add_argument("--expected-variant", required=True)
    ns = ap.parse_args(argv[1:])
    try:  # B334: lectura gobernada por NOMBRE SIMPLE en el directorio autorizado (sin ancestro symlink)
        raw = grio.read_receipt_bytes(ns.receipt)
        receipt = json.loads(raw.decode("utf-8"), object_pairs_hook=ds._no_dup_keys)
    except (OSError, ValueError) as exc:
        print(f"✗ recibo ilegible/inválido: {exc}")
        return 1
    if ns.lock not in lc.DEEP_RUNTIME:
        print(f"✗ lock no gobernado: {ns.lock}")
        return 1
    try:  # §10.4/§10.5: hashes y pins desde BYTES GOBERNADOS; §10.8: toda falla es ROJA
        lock_bytes = _governed_bytes(ns.lock)
        manifest_bytes = _governed_bytes(lc.MANIFEST_REL)
        contract = ds.load_contract()
    except (OSError, ValueError, GovernanceSnapshotError) as exc:  # lectura gobernada/contrato falló → ROJO (§10.8)
        print(f"✗ lectura gobernada de lock/manifiesto/contrato falló: {exc}")
        return 1
    try:  # §10.6: REOBSERVAR el entorno real; §10.8: cualquier falla de import/plataforma es ROJA (una excepción exótica
        # no capturada PROPAGA y también aborta el proceso — nunca un skip verde)
        reobs_probs, observed, real_python, real_system, real_machine = _reobserve(ns.lock)
    except (ImportError, RuntimeError, OSError, ValueError, GovernanceSnapshotError) as exc:
        print(f"✗ reobservación del entorno falló: {exc}")
        return 1
    if reobs_probs:
        print(f"✗ reobservación con problemas ({len(reobs_probs)}):")
        for p in reobs_probs:
            print(f"  - {p}")
        return 1
    probs = receipt_problems(
        receipt,
        lock_rel=ns.lock,
        expected_variant=ns.expected_variant,
        contract=contract,
        lock_sha="sha256:" + hashlib.sha256(lock_bytes).hexdigest(),
        manifest_sha="sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        pins=lc.pin_map(lock_bytes.decode("utf-8")),  # §10.5: pins desde los MISMOS bytes gobernados
        git_head=_git_head(),
        github_sha=os.environ.get("GITHUB_SHA"),
        real_python=real_python,
        real_system=real_system,
        real_machine=real_machine,
        observed=observed,
    )
    if probs:
        print(f"✗ VALIDADOR DE RECIBO DEEP ({ns.receipt}) falló ({len(probs)}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ recibo deep válido y REOBSERVADO ({ns.expected_variant})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
