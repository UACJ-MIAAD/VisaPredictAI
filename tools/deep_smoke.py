#!/usr/bin/env python
"""Smoke REAL de un lock deep instalado (P0R.4R / P0R.4R2). Se ejecuta SOLO en el job
`deep-lock-install` de CI (los deps deep están instalados en ese runner), NO en el CI base.

  python -m tools.deep_smoke --lock locks/deep-linux-x86_64-cpu.txt --receipt deep-receipt-linux-cpu.json

La expectativa (variante/plataforma/torch) NO viene del workflow: se DERIVA del contrato único
(lock_contracts.DEEP_RUNTIME). El inventario del stack (módulo↔distribución) proviene de un contrato
INDEPENDIENTE gobernado (`security/deep_smoke_contract.json`), NO de este archivo, para que mutar
`deep_smoke.py` no pueda auto-confirmar un inventario incompleto (B322/B323). Verifica: contrato de
lockset OK; Python 3.14.x; plataforma observada == esperada; inventario observado EXACTAMENTE igual al
contrato (ni omisión ni extra); versión EXACTA de cada dist contra el pin del lock; torch con la variante
esperada; que todo el stack IMPORTA (imports estáticos, sin fábrica dinámica); `pip check` exit 0; y un
tensor finito determinista (83.0). Emite un receipt LIGADO al lock Y al contrato (sha256 de ambos) SOLO
si todo pasa."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path

from tools import lock_contracts as lc

# Autoridad INDEPENDIENTE del inventario deep — SEPARADA de este archivo (B323).
_CONTRACT_REL = "security/deep_smoke_contract.json"
_GIT_ABS = "/usr/bin/git"  # B328: git ABSOLUTO gobernado — NUNCA por PATH
_PY_RE = re.compile(r"3\.14\.\d+")  # B328: Python exacto (fullmatch), no `startswith`
_HEX40 = re.compile(r"[0-9a-f]{40}")  # B328: commit real de 40 hex


def _sha256(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{_CONTRACT_REL}: clave JSON duplicada")
    return dict(pairs)


def _parse_contract_bytes(raw: bytes) -> tuple[tuple[str, str], ...]:
    """B322/B326: parser+validador ÚNICO del contrato desde BYTES canónicos — anti-clave-duplicada, esquema CERRADO,
    orden canónico por módulo, nombres PEP-503, módulos/distribuciones únicos. Devuelve la tupla INMUTABLE
    `((module, distribution), …)`; toda desviación LEVANTA `ValueError` (no hay recibo)."""
    obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_keys)
    if not (isinstance(obj, dict) and set(obj) == {"schema_version", "imports"}):
        raise ValueError(f"{_CONTRACT_REL}: claves top != {{schema_version, imports}}")
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:  # bool no cuela (is not int)
        raise ValueError(f"{_CONTRACT_REL}: schema_version debe ser 1 (int exacto)")
    entries = obj["imports"]
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{_CONTRACT_REL}: imports debe ser lista no vacía")
    imports: list[tuple[str, str]] = []
    for e in entries:
        if not (isinstance(e, dict) and set(e) == {"module", "distribution"}):
            raise ValueError(f"{_CONTRACT_REL}: entrada != {{module, distribution}}: {e!r}")
        m, d = e["module"], e["distribution"]
        if type(m) is not str or type(d) is not str or not m or not d:
            raise ValueError(f"{_CONTRACT_REL}: module/distribution deben ser str no vacíos: {e!r}")
        if lc._norm(d) != d:  # PEP-503 canónico (una distribución no normalizada podría duplicar por normalización)
            raise ValueError(f"{_CONTRACT_REL}: distribution {d!r} no está en forma PEP-503 ({lc._norm(d)})")
        imports.append((m, d))
    modules = [m for m, _ in imports]
    dists = [d for _, d in imports]
    if modules != sorted(modules):
        raise ValueError(f"{_CONTRACT_REL}: imports no está en orden canónico por módulo")
    if len(set(modules)) != len(modules) or len(set(dists)) != len(dists):
        raise ValueError(f"{_CONTRACT_REL}: módulos/distribuciones deben ser únicos")
    return tuple(imports)


@dataclass(frozen=True, slots=True)
class DeepSmokeContract:
    """B326: autoridad de inventario deep INMUTABLE y AUTO-CONSISTENTE. Sólo la producen `load_contract()` (desde bytes
    gobernados) o `for_test()` (desde bytes canónicos re-validados). `__post_init__` CRUZA contenido↔hash↔imports, de modo
    que un caller NO puede forjar una lista vacía con un sha real ni un sha arbitrario: cualquier objeto construido es
    consistente con sus bytes canónicos. `evaluate()` exige `type(x) is DeepSmokeContract` (ni lista+sha sueltos, ni
    subclase)."""

    imports: tuple[tuple[str, str], ...]
    canonical_bytes: bytes
    sha256: str

    def __post_init__(self) -> None:
        if type(self.canonical_bytes) is not bytes or type(self.sha256) is not str:
            raise ValueError("DeepSmokeContract: tipos inválidos (canonical_bytes/sha256)")
        if self.sha256 != "sha256:" + hashlib.sha256(self.canonical_bytes).hexdigest():
            raise ValueError("DeepSmokeContract: sha256 no coincide con canonical_bytes")
        if _parse_contract_bytes(self.canonical_bytes) != self.imports:  # imports↔bytes cruzados (no forjable)
            raise ValueError("DeepSmokeContract: imports no coincide con canonical_bytes")

    @classmethod
    def _from_bytes(cls, raw: bytes) -> DeepSmokeContract:
        return cls(imports=_parse_contract_bytes(raw), canonical_bytes=raw, sha256="sha256:" + hashlib.sha256(raw).hexdigest())  # fmt: skip

    @classmethod
    def for_test(cls, imports: tuple[tuple[str, str], ...]) -> DeepSmokeContract:
        """Para tests PUROS: serializa `imports` a bytes canónicos y RE-VALIDA el mismo esquema (orden/PEP-503/únicos)."""
        payload = {"schema_version": 1, "imports": [{"module": m, "distribution": d} for m, d in imports]}
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        return cls._from_bytes(raw)


def load_contract() -> DeepSmokeContract:
    """B322/B323/B326: lee el contrato INDEPENDIENTE de forma GOBERNADA (sin symlink, modo/uid/nlink exactos, snapshot
    pre-post) vía `GovernanceSnapshot` y lo emite como `DeepSmokeContract` auto-consistente. Toda desviación → `ValueError`."""
    from tools.governance_snapshot import GovernanceSnapshot

    with GovernanceSnapshot(str(lc.ROOT)) as snap:
        raw = snap.read(_CONTRACT_REL, category="contract").data
    return DeepSmokeContract._from_bytes(raw)


def _distribution_inventory() -> tuple[dict[str, str], list[str]]:
    """B327: inventario de distribuciones instaladas SIN last-wins — un nombre normalizado DUPLICADO (dos dist-info) es un
    PROBLEMA, no una sobrescritura silenciosa. Devuelve `({norm_name: version}, problemas)`."""
    inv: dict[str, str] = {}
    probs: list[str] = []
    for d in distributions():
        name = d.name
        if not name:
            continue
        norm = lc._norm(name)
        if norm in inv:
            probs.append(f"distribución con nombre normalizado DUPLICADO: {norm}")
        inv[norm] = d.version
    return inv, probs


def identity_problems(
    module: str, distribution: str, *, origin: str | None, providing: list[str], sys_prefix: str, root: str
) -> list[str]:
    """B327: identidad de un import del stack (PURA/testeable) — el módulo debe tener un `origin` REAL bajo `sys_prefix`
    (no un namespace/preinyectado sin origin, no un shadow bajo ROOT) y la distribución que lo PROVEE
    (`importlib.metadata.packages_distributions`) debe ser EXACTAMENTE la esperada."""
    probs: list[str] = []
    if origin is None:
        probs.append(f"{module}: sin origin certificable (namespace/preinyectado)")
    else:
        rp = os.path.realpath(origin)
        prefix = os.path.realpath(sys_prefix)
        rroot = os.path.realpath(root)
        if not (rp == prefix or rp.startswith(prefix + os.sep)):
            probs.append(f"{module}: origin {rp} fuera de sys.prefix")
        if rp == rroot or rp.startswith(rroot + os.sep):
            probs.append(f"{module}: origin bajo ROOT (shadow local)")
    provided = sorted({lc._norm(p) for p in providing})
    if provided != [distribution]:
        probs.append(f"{module}: packages_distributions {provided} != [{distribution}]")
    return probs


def _git(*args: str) -> str | None:
    """B328: git ABSOLUTO gobernado (`/usr/bin/git`), NUNCA por PATH. None si falla o no es un repo."""
    try:
        out = subprocess.run([_GIT_ABS, *args], capture_output=True, text=True, timeout=10, check=False)
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def verified_commit(env_sha: str | None) -> tuple[str | None, str | None]:
    """B328: commit REAL y verificable — `HEAD` resuelto por git ABSOLUTO, exactamente 40-hex, que EXISTE como commit
    (`cat-file -e HEAD^{commit}`); en CI `GITHUB_SHA` debe COINCIDIR con HEAD. Devuelve `(sha, problema)`; el smoke falla
    si el problema no es None (nunca emite un recibo con `unknown`)."""
    head = _git("rev-parse", "HEAD")
    if head is None or not _HEX40.fullmatch(head):
        return None, "commit HEAD no resuelto a 40-hex"
    if _git("cat-file", "-e", f"{head}^{{commit}}") is None:
        return None, f"commit {head} no existe como commit"
    if env_sha is not None and env_sha != head:
        return None, f"GITHUB_SHA {env_sha} != HEAD {head}"
    return head, None


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
    contract: DeepSmokeContract,
    commit_sha: str,
    import_identity: list[str] | tuple[str, ...] = (),
    import_records: list[dict] | tuple[dict, ...] = (),
) -> tuple[list[str], dict]:
    """Lógica PURA del smoke (sin importar el stack deep) — testeable con valores inyectados. El inventario observado se
    exige EXACTAMENTE igual al del contrato independiente (B322): ni omisión, ni extra, ni tipo inválido. La AUTORIDAD del
    inventario es un `DeepSmokeContract` auto-consistente emitido por `load_contract`/`for_test` (B326): un caller NO puede
    pasar una lista+sha sueltos ni forjados. El recibo SOLO se construye si `problems == []`."""
    if lock_rel not in lc.DEEP_RUNTIME:
        return [f"lock no gobernado: {lock_rel} (no está en DEEP_RUNTIME)"], {}
    if type(contract) is not DeepSmokeContract:  # B326: ni lista+sha sueltos ni subclase — sólo la fábrica gobernada
        return ["contrato deep inválido (se exige DeepSmokeContract de load_contract/for_test)"], {}
    rt = lc.DEEP_RUNTIME[lock_rel]
    probs: list[str] = [f"[contrato] {p}" for p in lc.validate_all(lc.ROOT)]
    if type(installed) is not dict or not all(type(k) is str and type(v) is str for k, v in installed.items()):
        return [*probs, "inventario observado inválido (se exige dict[str, str] exacto)"], {}
    expected_dists = [d for _, d in contract.imports]
    observed, expected = set(installed), set(expected_dists)
    if observed - expected:
        probs.append(f"inventario observado con EXTRA fuera del contrato: {sorted(observed - expected)}")
    if expected - observed:
        probs.append(f"inventario observado OMITE del contrato: {sorted(expected - observed)}")
    if not _PY_RE.fullmatch(py_version):  # B328: fullmatch exacto `3.14.Z` (no `3.14.evil` por `startswith`)
        probs.append(f"Python {py_version} no es exactamente 3.14.Z")
    if not _HEX40.fullmatch(commit_sha):  # B328: commit real 40-hex verificado (nunca `unknown` en el recibo)
        probs.append(f"commit {commit_sha!r} no es un sha de 40 hex verificado")
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
    probs.extend(import_identity)  # B327: identidad módulo↔distribución↔origen (calculada por run() en el entorno real)
    # t=[[0,1,2],[3,4,5]]; t@t.T=[[5,14],[14,50]]; suma=83 (determinista en cualquier plataforma).
    if checksum != 83.0:
        probs.append(f"checksum tensorial {checksum} != 83.0 (no determinista)")
    if probs:  # el recibo SÓLO se emite si nada falló
        return probs, {}
    receipt = {
        "commit_sha": commit_sha,
        "lock": lock_rel,
        "lock_sha256": _sha256(lc.ROOT / lock_rel),
        "manifest_sha256": _sha256(lc.ROOT / lc.MANIFEST_REL),
        "deep_smoke_contract_sha256": contract.sha256,
        "variant_expected": rt["variant"],
        "platform_expected": f"{rt['system']} {rt['machine']}",
        "platform_observed": f"{system} {machine}",
        "python": py_version,
        "torch_expected": rt["torch"],
        "torch_observed": torch_version,
        "pip_check": "ok" if pip_check_ok else "fail",
        "versions": {d: installed[d] for d in expected_dists},  # orden CANÓNICO del contrato
        "imports": list(import_records),  # B327: módulo/distribución/origen relativo a sys.prefix
        "tensor_checksum": checksum,
    }
    return probs, receipt


def run(lock_rel: str) -> tuple[list[str], dict]:
    """Recoge el estado REAL del entorno deep instalado y delega a evaluate(). El inventario esperado viene del contrato
    INDEPENDIENTE; el observado se construye consultando `version()` por cada distribución del contrato (una ausente se
    OMITE y la caza la igualdad de conjuntos de evaluate). B316: imports ESTÁTICOS, sin fábrica dinámica. B323: una
    comprobación RUNTIME (no `assert`; sobrevive `python -O`) exige que el stack importado == módulos del contrato."""
    contract = load_contract()
    # B327: inventario OBSERVADO sin last-wins (dist-info normalizada duplicada = problema); una dist del contrato NO
    # instalada queda fuera del dict y la caza la igualdad de conjuntos de evaluate().
    dist_inv, id_probs = _distribution_inventory()
    installed = {dist: dist_inv[lc._norm(dist)] for _, dist in contract.imports if lc._norm(dist) in dist_inv}
    import chronos as _chronos
    import mlflow as _mlflow
    import neuralforecast as _neuralforecast
    import optuna as _optuna
    import pandas as _pandas
    import ray as _ray
    import torch
    import transformers as _transformers

    _imported = (_chronos, _mlflow, _neuralforecast, _optuna, _pandas, _ray, torch, _transformers)
    imported_modules = frozenset(m.__name__.split(".")[0] for m in _imported)
    expected_modules = frozenset(module for module, _ in contract.imports)
    if imported_modules != expected_modules:  # NO es `assert` (sobrevive `python -O`)
        raise RuntimeError(f"stack importado {sorted(imported_modules)} != contrato {sorted(expected_modules)}")

    # B327: liga CADA módulo importado a su distribución + origen REAL (bajo sys.prefix, no ROOT/shadow/preinyectado).
    from importlib.metadata import packages_distributions

    by_name = {m.__name__.split(".")[0]: m for m in _imported}
    pkg_dists = packages_distributions()
    import_records: list[dict] = []
    for module, dist in contract.imports:
        mod = by_name.get(module)
        spec = mod.__spec__ if mod is not None else None
        origin = spec.origin if spec is not None else None
        id_probs.extend(
            identity_problems(
                module,
                dist,
                origin=origin,
                providing=list(pkg_dists.get(module, [])),
                sys_prefix=sys.prefix,
                root=str(lc.ROOT),
            )  # fmt: skip
        )
        rel = os.path.relpath(os.path.realpath(origin), sys.prefix) if origin else "unknown"
        import_records.append({"module": module, "distribution": dist, "origin": rel})

    torch.manual_seed(0)
    prod = torch.arange(6, dtype=torch.float32).reshape(2, 3) @ torch.arange(6, dtype=torch.float32).reshape(2, 3).T
    finite = bool(torch.isfinite(prod).all())
    checksum = round(float(prod.sum().item()), 4) if finite else float("nan")
    pip_ok = (
        subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, check=False).returncode
        == 0
    )
    commit, commit_prob = verified_commit(os.environ.get("GITHUB_SHA"))  # B328: HEAD real verificado (o falla)
    if commit_prob is not None:
        id_probs.append(commit_prob)
    return evaluate(
        lock_rel,
        py_version=platform.python_version(),
        system=platform.system(),
        machine=platform.machine(),
        installed=installed,
        torch_version=torch.__version__,
        pip_check_ok=pip_ok,
        checksum=checksum,
        contract=contract,
        commit_sha=commit or "unknown",
        import_identity=id_probs,
        import_records=import_records,
    )


def write_receipt_governed(path: str, data: dict) -> None:
    """B328: escritura GOBERNADA del recibo — ruta relativa SIMPLE (sin `..`/absoluta), `O_EXCL|O_NOFOLLOW` con 0600 (no
    sobrescribe ni sigue symlink), write-all, y `fsync` del fichero Y del directorio."""
    if os.path.isabs(path) or os.pardir in path.split(os.sep):
        raise ValueError(f"ruta de recibo inválida (se exige relativa simple): {path!r}")
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        mv = memoryview(payload)
        while mv:  # write-all
            mv = mv[os.write(fd, mv) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


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
    write_receipt_governed(ns.receipt, receipt)
    print(
        f"✓ deep smoke OK ({receipt['variant_expected']}): torch {receipt['torch_observed']} · pip check ok · tensor 83.0"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
