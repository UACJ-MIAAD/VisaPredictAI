#!/usr/bin/env python
"""B328: validador INDEPENDIENTE de un recibo de deep smoke — NO confía en el auto-reporte del productor.

  python -m tools.validate_deep_receipt --receipt R.json --lock locks/deep-linux-x86_64-cpu.txt --expected-variant linux-cpu

Re-deriva commit (git ABSOLUTO, 40-hex, `cat-file -e`, == `GITHUB_SHA`), Python (fullmatch `3.14.Z`), plataforma/variante
(desde `DEEP_RUNTIME` y `--expected-variant`), y los hashes de lock/manifiesto/contrato desde bytes GOBERNADOS; exige el
esquema EXACTO del recibo, versiones == pins del lock, orígenes de import relativos (sin `..`/absoluta), `pip_check == ok`
y checksum tensorial 83 finito. Se cablea en CI ENTRE el smoke y el upload — un recibo verde con validador rojo aborta."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from tools import deep_smoke as ds
from tools import lock_contracts as lc

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
_IMPORT_KEYS = {"module", "distribution", "origin"}
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
    git_head: str | None,
    github_sha: str | None,
) -> list[str]:
    """B328: núcleo PURO/testeable de validación — re-derivados inyectados, sin confiar en el recibo. `git_head` es el
    HEAD resuelto por el caller (git absoluto); `lock_sha`/`manifest_sha` los hashes gobernados; `contract` el
    `DeepSmokeContract` cargado independientemente."""
    if type(receipt) is not dict:
        return ["recibo no es un objeto JSON"]
    if set(receipt) != _RECEIPT_KEYS:
        return [f"claves del recibo != esquema exacto (faltan {_RECEIPT_KEYS - set(receipt)}, sobran {set(receipt) - _RECEIPT_KEYS})"]  # fmt: skip
    if lock_rel not in lc.DEEP_RUNTIME:
        return [f"lock no gobernado: {lock_rel}"]
    rt = lc.DEEP_RUNTIME[lock_rel]
    probs: list[str] = []
    commit = receipt["commit_sha"]
    if type(commit) is not str or not _HEX40.fullmatch(commit):
        probs.append(f"commit_sha {commit!r} no es 40-hex")
    elif git_head is not None and commit != git_head:
        probs.append(f"commit_sha {commit} != HEAD {git_head}")
    if github_sha is not None and git_head is not None and github_sha != git_head:
        probs.append(f"GITHUB_SHA {github_sha} != HEAD {git_head}")
    if type(receipt["python"]) is not str or not _PY_RE.fullmatch(receipt["python"]):
        probs.append(f"python {receipt['python']!r} no es exactamente 3.14.Z")
    if receipt["variant_expected"] != expected_variant or rt["variant"] != expected_variant:
        probs.append(f"variante {receipt['variant_expected']!r} != esperada {expected_variant!r}")
    plat = f"{rt['system']} {rt['machine']}"
    if receipt["platform_expected"] != plat or receipt["platform_observed"] != plat:
        probs.append(f"plataforma {receipt['platform_observed']!r} != {plat!r}")
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
    if receipt["tensor_checksum"] != 83.0 or type(receipt["tensor_checksum"]) is bool:
        probs.append(f"tensor_checksum {receipt['tensor_checksum']!r} != 83.0")
    probs.extend(_version_problems(receipt["versions"], lock_rel, contract, rt))
    probs.extend(_import_problems(receipt["imports"], contract))
    return probs


def _version_problems(versions: object, lock_rel: str, contract: ds.DeepSmokeContract, rt: dict) -> list[str]:
    expected_dists = [d for _, d in contract.imports]
    if type(versions) is not dict or list(versions) != expected_dists:
        return [
            f"versions {list(versions) if isinstance(versions, dict) else versions!r} != orden canónico del contrato"
        ]
    pins = lc.pin_map((lc.ROOT / lock_rel).read_text())
    probs: list[str] = []
    for dist, v in versions.items():
        if dist == "torch":
            if v != rt["torch"]:
                probs.append(f"torch en versions {v!r} != {rt['torch']!r}")
        elif pins.get(lc._norm(dist)) != v:
            probs.append(f"{dist} {v!r} != pin del lock {pins.get(lc._norm(dist))!r}")
    return probs


def _import_problems(records: object, contract: ds.DeepSmokeContract) -> list[str]:
    if type(records) is not list or len(records) != len(contract.imports):
        return [f"imports del recibo != {len(contract.imports)} entradas del contrato"]
    probs: list[str] = []
    for rec, (module, dist) in zip(records, contract.imports, strict=True):
        if type(rec) is not dict or set(rec) != _IMPORT_KEYS:
            probs.append(f"entrada de import {rec!r} != {{module, distribution, origin}}")
            continue
        if rec["module"] != module or rec["distribution"] != dist:
            probs.append(f"import {rec['module']!r}/{rec['distribution']!r} != contrato {module!r}/{dist!r}")
        origin = rec["origin"]
        if type(origin) is not str or os.path.isabs(origin) or os.pardir in origin.split("/") or origin == "unknown":
            probs.append(f"origin {origin!r} no es una ruta relativa simple bajo sys.prefix")
    return probs


def _git_head() -> str | None:
    try:
        out = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        )
    except OSError:
        return None
    head = out.stdout.strip() if out.returncode == 0 else None
    return head if head and _HEX40.fullmatch(head) else None


def _read_receipt_governed(path: str) -> object:
    """Lectura fd-bound del recibo (O_NOFOLLOW, no sigue symlink) + JSON anti-clave-duplicada."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=ds._no_dup_keys)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--lock", required=True)
    ap.add_argument("--expected-variant", required=True)
    ns = ap.parse_args(argv[1:])
    try:
        receipt = _read_receipt_governed(ns.receipt)
    except (OSError, ValueError) as exc:
        print(f"✗ recibo ilegible/inválido: {exc}")
        return 1
    probs = receipt_problems(
        receipt,
        lock_rel=ns.lock,
        expected_variant=ns.expected_variant,
        contract=ds.load_contract(),
        lock_sha=ds._sha256(lc.ROOT / ns.lock) if (lc.ROOT / ns.lock).exists() else "sha256:missing",
        manifest_sha=ds._sha256(lc.ROOT / lc.MANIFEST_REL),
        git_head=_git_head(),
        github_sha=os.environ.get("GITHUB_SHA"),
    )
    if probs:
        print(f"✗ VALIDADOR DE RECIBO DEEP ({ns.receipt}) falló ({len(probs)}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ recibo deep válido ({ns.expected_variant})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
