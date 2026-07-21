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

from tools import governed_import_identity as gi
from tools import lock_contracts as lc

# Autoridad INDEPENDIENTE del inventario deep — SEPARADA de este archivo (B323).
_CONTRACT_REL = "security/deep_smoke_contract.json"
_PY_RE = re.compile(r"3\.14\.\d+")  # B328: Python exacto (fullmatch), no `startswith`
_HEX40 = re.compile(
    r"[0-9a-f]{40}"
)  # B328: commit real de 40 hex (usado en evaluate; B336: HEAD lo gobierna la snapshot)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")  # B335: forma canónica del origin_sha256 gobernado


def _sha256(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{_CONTRACT_REL}: clave JSON duplicada")
    return dict(pairs)


_CONTRACT_MAX_ENTRIES = 64  # cota explícita del número de imports (B335/RC-3)
_CONTRACT_MAX_PROVIDERS = 16  # cota explícita de providers por módulo


def _parse_contract_bytes(raw: bytes) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """B322/B326/RC-3: parser+validador ÚNICO del contrato desde BYTES canónicos (ESQUEMA 2) — anti-clave-duplicada,
    esquema CERRADO, orden canónico por módulo, nombres PEP-503, módulos/distribuciones PRIMARIAS únicas, y una lista de
    `providers` por módulo (no vacía, PEP-503, ordenada, única, con la distribución primaria DENTRO). Modela que un módulo
    de import puede estar provisto por VARIAS distribuciones (p.ej. `mlflow` ← mlflow / mlflow-skinny / mlflow-tracing).
    Devuelve la tupla INMUTABLE `((module, distribution, (providers…)), …)`; toda desviación LEVANTA `ValueError`."""
    obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_keys)
    if not (isinstance(obj, dict) and set(obj) == {"schema_version", "imports"}):
        raise ValueError(f"{_CONTRACT_REL}: claves top != {{schema_version, imports}}")
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 2:  # bool no cuela (is not int)
        raise ValueError(f"{_CONTRACT_REL}: schema_version debe ser 2 (int exacto)")
    raw_entries = obj["imports"]
    if not isinstance(raw_entries, list) or not (0 < len(raw_entries) <= _CONTRACT_MAX_ENTRIES):
        raise ValueError(f"{_CONTRACT_REL}: imports debe ser lista no vacía de a lo sumo {_CONTRACT_MAX_ENTRIES}")
    entries: list[tuple[str, str, tuple[str, ...]]] = []
    for e in raw_entries:
        if not (isinstance(e, dict) and set(e) == {"module", "distribution", "providers"}):
            raise ValueError(f"{_CONTRACT_REL}: entrada != {{module, distribution, providers}}: {e!r}")
        m, d, provs = e["module"], e["distribution"], e["providers"]
        if type(m) is not str or type(d) is not str or not m or not d:
            raise ValueError(f"{_CONTRACT_REL}: module/distribution deben ser str no vacíos: {e!r}")
        if lc._norm(d) != d:  # PEP-503 canónico (una distribución no normalizada podría duplicar por normalización)
            raise ValueError(f"{_CONTRACT_REL}: distribution {d!r} no está en forma PEP-503 ({lc._norm(d)})")
        if not isinstance(provs, list) or not (0 < len(provs) <= _CONTRACT_MAX_PROVIDERS):
            raise ValueError(f"{_CONTRACT_REL}: providers de {m!r} debe ser lista no vacía de a lo sumo {_CONTRACT_MAX_PROVIDERS}")  # fmt: skip
        for p in provs:
            if type(p) is not str or not p or lc._norm(p) != p:
                raise ValueError(f"{_CONTRACT_REL}: provider {p!r} de {m!r} debe ser str PEP-503 no vacío")
        if list(provs) != sorted(provs) or len(set(provs)) != len(provs):
            raise ValueError(f"{_CONTRACT_REL}: providers de {m!r} deben estar ordenados y ser únicos")
        if d not in provs:
            raise ValueError(f"{_CONTRACT_REL}: la distribución primaria {d!r} debe estar en providers de {m!r}")
        entries.append((m, d, tuple(provs)))
    modules = [m for m, _, _ in entries]
    dists = [d for _, d, _ in entries]
    if modules != sorted(modules):
        raise ValueError(f"{_CONTRACT_REL}: imports no está en orden canónico por módulo")
    if len(set(modules)) != len(modules) or len(set(dists)) != len(dists):
        raise ValueError(f"{_CONTRACT_REL}: módulos/distribuciones primarias deben ser únicos")
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class DeepSmokeContract:
    """B326: autoridad de inventario deep INMUTABLE y AUTO-CONSISTENTE. Sólo la producen `load_contract()` (desde bytes
    gobernados) o `for_test()` (desde bytes canónicos re-validados). `__post_init__` CRUZA contenido↔hash↔imports, de modo
    que un caller NO puede forjar una lista vacía con un sha real ni un sha arbitrario: cualquier objeto construido es
    consistente con sus bytes canónicos. `evaluate()` exige `type(x) is DeepSmokeContract` (ni lista+sha sueltos, ni
    subclase)."""

    entries: tuple[tuple[str, str, tuple[str, ...]], ...]  # (module, primary_distribution, providers) — RC-3
    canonical_bytes: bytes
    sha256: str

    def __post_init__(self) -> None:
        if type(self.canonical_bytes) is not bytes or type(self.sha256) is not str:
            raise ValueError("DeepSmokeContract: tipos inválidos (canonical_bytes/sha256)")
        if self.sha256 != "sha256:" + hashlib.sha256(self.canonical_bytes).hexdigest():
            raise ValueError("DeepSmokeContract: sha256 no coincide con canonical_bytes")
        if _parse_contract_bytes(self.canonical_bytes) != self.entries:  # entries↔bytes cruzados (no forjable)
            raise ValueError("DeepSmokeContract: entries no coincide con canonical_bytes")

    @property
    def imports(self) -> tuple[tuple[str, str], ...]:
        """Vista `(module, primary_distribution)` — compatibilidad con los consumidores que no necesitan providers."""
        return tuple((m, d) for m, d, _ in self.entries)

    def providers_of(self, module: str) -> tuple[str, ...]:
        """Providers declarados para `module` (lista PEP-503 ordenada); la distribución primaria está incluida."""
        for m, _d, provs in self.entries:
            if m == module:
                return provs
        raise KeyError(module)

    @property
    def expected_providers(self) -> tuple[str, ...]:
        """Unión ORDENADA de TODAS las distribuciones que deben estar instaladas (todos los providers de todos los módulos).
        Con mlflow multi-provider, este conjunto es mayor que las distribuciones primarias."""
        return tuple(sorted({p for _m, _d, provs in self.entries for p in provs}))

    @classmethod
    def _from_bytes(cls, raw: bytes) -> DeepSmokeContract:
        return cls(entries=_parse_contract_bytes(raw), canonical_bytes=raw, sha256="sha256:" + hashlib.sha256(raw).hexdigest())  # fmt: skip

    @classmethod
    def for_test(cls, imports: tuple[tuple[str, str], ...], *, providers: dict[str, tuple[str, ...]] | None = None) -> DeepSmokeContract:  # fmt: skip
        """Para tests PUROS de `evaluate` (SOLO problemas, jamás recibo): serializa a bytes canónicos ESQUEMA 2 y RE-VALIDA.
        Por defecto `providers` de cada módulo es el singleton `[distribution]`; pásalo explícito para modelar multi-provider
        (mlflow). B331/B335: `for_test` NO alcanza la ruta de certificación — `certify_runtime` observa el entorno real y
        carga el contrato canónico por su cuenta; es el ÚNICO que emite recibo. Un contrato de fixture no puede certificar."""
        provs = providers or {}
        payload = {
            "schema_version": 2,
            "imports": [{"module": m, "distribution": d, "providers": list(provs.get(m, (d,)))} for m, d in imports],
        }
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        return cls._from_bytes(raw)


@dataclass(frozen=True, slots=True)
class DeepObservation:
    """B331: estado REAL observado del entorno deep instalado — lo produce SOLO `observe_runtime()`. `certify_runtime()`
    lo OBTIENE llamando a `observe_runtime` por su cuenta (jamás del caller) y construye el recibo con el contrato CANÓNICO;
    ninguna función que RECIBA un `DeepObservation` construye recibo, así que el caller no puede inyectar un inventario
    reducido/fabricado a la certificación (agujero de B326/B335 cerrado)."""

    py_version: str
    system: str
    machine: str
    installed: tuple[tuple[str, str], ...]  # inventario observado (orden canónico por distribución)
    torch_version: str
    pip_check_ok: bool
    checksum: float
    commit_sha: str
    # (module, distribution, providers, origin_rel, origin_sha256, origin_owners) — B332/RC-3
    import_records: tuple[tuple[str, str, tuple[str, ...], str, str, tuple[str, ...]], ...]
    identity_problems: tuple[str, ...]  # problemas de identidad/commit derivados del entorno REAL


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


def verified_commit(env_sha: str | None) -> tuple[str | None, str | None]:
    """B328/B336: commit REAL y verificable vía la ÚNICA observación git GOBERNADA (`GovernanceSnapshot.head_commit`:
    toplevel textual == ROOT, `rev-parse --verify HEAD^{commit}`, 40-hex de una línea, git absoluto gobernado con entorno
    allowlist e identidad revalidada). En CI `GITHUB_SHA` debe COINCIDIR con HEAD. Devuelve `(sha, problema)`; el smoke falla
    si el problema no es None (nunca emite un recibo con `unknown`). Ni aquí ni en el validador hay git ad hoc (B336)."""
    from tools.governance_snapshot import GovernanceSnapshot, GovernanceSnapshotError

    try:
        head = GovernanceSnapshot(str(lc.ROOT)).head_commit()
    except GovernanceSnapshotError as exc:
        return None, f"HEAD no gobernable ({exc})"
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
) -> tuple[list[str], dict]:
    """Lógica PURA de PROBLEMAS del smoke (sin importar el stack deep) — testeable con valores inyectados y un contrato de
    fixture. El inventario observado se exige EXACTAMENTE igual al del contrato (B322): ni omisión, ni extra, ni tipo
    inválido. B331: esta función NUNCA construye un recibo (el segundo elemento es SIEMPRE `{}`) — un contrato reducido de
    fixture jamás puede certificar; el ÚNICO emisor de recibo es `certify_runtime`, que observa el entorno y carga el
    contrato canónico por su cuenta."""
    if lock_rel not in lc.DEEP_RUNTIME:
        return [f"lock no gobernado: {lock_rel} (no está en DEEP_RUNTIME)"], {}
    if type(contract) is not DeepSmokeContract:  # B326: ni lista+sha sueltos ni subclase — sólo la fábrica gobernada
        return ["contrato deep inválido (se exige DeepSmokeContract de load_contract/for_test)"], {}
    rt = lc.DEEP_RUNTIME[lock_rel]
    probs: list[str] = [f"[contrato] {p}" for p in lc.validate_all(lc.ROOT)]
    if type(installed) is not dict or not all(type(k) is str and type(v) is str for k, v in installed.items()):
        return [*probs, "inventario observado inválido (se exige dict[str, str] exacto)"], {}
    expected_dists = list(contract.expected_providers)  # RC-3: TODOS los providers (incl. mlflow-skinny/-tracing)
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
    probs.extend(
        import_identity
    )  # B327/B332: identidad módulo↔distribución↔origen GOBERNADA (la calcula observe_runtime)
    # t=[[0,1,2],[3,4,5]]; t@t.T=[[5,14],[14,50]]; suma=83 (determinista en cualquier plataforma).
    if checksum != 83.0:
        probs.append(f"checksum tensorial {checksum} != 83.0 (no determinista)")
    return probs, {}  # B331: SIEMPRE recibo vacío — la certificación vive en certify_runtime


def _import_records_problems(records: tuple, contract: DeepSmokeContract) -> list[str]:
    """B335/RC-3: cruza los `import_records` de la observación EXACTAMENTE con el contrato ANTES de emitir recibo —
    longitud, orden, module/dist, `providers` EXACTOS del contrato, origen relativo simple (ni absoluto ni `..` ni
    `unknown`), `origin_sha256` con forma `sha256:<64hex>`, y `origin_owners` NO vacíos y SUBCONJUNTO de los providers.
    Defensa en profundidad: un registro degradado bloquea el recibo aunque la identidad gobernada haya fallado."""
    if len(records) != len(contract.entries):
        return [f"import_records {len(records)} != {len(contract.entries)} entradas del contrato"]
    probs: list[str] = []
    for rec, (cm, cd, cprovs) in zip(records, contract.entries, strict=True):
        if not (isinstance(rec, tuple) and len(rec) == 6):
            probs.append(f"import_record {rec!r} no tiene forma (module, dist, providers, origin, sha, owners)")
            continue
        m, d, provs, o, s, owners = rec
        if m != cm or d != cd:
            probs.append(f"import_record {m!r}/{d!r} != contrato {cm!r}/{cd!r}")
        if tuple(provs) != cprovs:
            probs.append(f"providers de {m!r} {tuple(provs)!r} != contrato {cprovs!r}")
        if type(o) is not str or not o or os.path.isabs(o) or o == "unknown" or os.pardir in o.split("/"):
            probs.append(f"origin {o!r} no es una ruta relativa simple bajo sys.prefix")
        if type(s) is not str or not _SHA256_RE.fullmatch(s):
            probs.append(f"origin_sha256 {s!r} no tiene forma sha256:<64hex>")
        if not owners or not set(owners) <= set(cprovs):
            probs.append(f"origin_owners de {m!r} {tuple(owners)!r} vacíos o no subconjunto de providers {cprovs!r}")
    return probs


def certify_runtime(lock_rel: str) -> tuple[list[str], dict]:
    """B331/B335: ÚNICO emisor de recibo deep. NO recibe observación del caller: OBSERVA el entorno real por su cuenta
    (`observe_runtime`) y carga el contrato CANÓNICO (`load_contract`), re-evalúa los problemas contra ESE contrato, cruza
    los `import_records` con el contrato (`_import_records_problems`), y SÓLO si no hay ninguno construye el recibo (única
    construcción del literal en todo el módulo). Como NINGUNA función que reciba un `DeepObservation` construye recibo, un
    caller no puede inyectar inventario/orígenes fabricados para certificar un stack incompleto (agujero de B335)."""
    contract = load_contract()  # AUTORIDAD CANÓNICA — nunca del caller (B331)
    obs_problems, observation = observe_runtime(lock_rel)  # entorno REAL — nunca del caller (B335)
    if observation is None:
        return (obs_problems or ["observe_runtime no produjo observación (fail-closed)"]), {}
    installed = dict(observation.installed)
    probs, _ = evaluate(
        lock_rel,
        py_version=observation.py_version,
        system=observation.system,
        machine=observation.machine,
        installed=installed,
        torch_version=observation.torch_version,
        pip_check_ok=observation.pip_check_ok,
        checksum=observation.checksum,
        contract=contract,
        commit_sha=observation.commit_sha,
        import_identity=list(observation.identity_problems),
    )
    probs.extend(_import_records_problems(observation.import_records, contract))  # B335: cruce EXACTO con el contrato
    if probs:  # el recibo SÓLO se emite si NADA falló
        return probs, {}
    rt = lc.DEEP_RUNTIME[lock_rel]
    expected_dists = list(contract.expected_providers)  # RC-3: TODAS las distribuciones (incl. mlflow-skinny/-tracing)
    receipt = {
        "commit_sha": observation.commit_sha,
        "lock": lock_rel,
        "lock_sha256": _sha256(lc.ROOT / lock_rel),
        "manifest_sha256": _sha256(lc.ROOT / lc.MANIFEST_REL),
        "deep_smoke_contract_sha256": contract.sha256,
        "variant_expected": rt["variant"],
        "platform_expected": f"{rt['system']} {rt['machine']}",
        "platform_observed": f"{observation.system} {observation.machine}",
        "python": observation.py_version,
        "torch_expected": rt["torch"],
        "torch_observed": observation.torch_version,
        "pip_check": "ok" if observation.pip_check_ok else "fail",
        "versions": {d: installed[d] for d in expected_dists},  # orden CANÓNICO (todos los providers)
        "imports": [
            {
                "module": m,
                "distribution": d,
                "providers": list(provs),
                "origin": o,
                "origin_sha256": s,
                "origin_owners": list(owners),
            }
            for m, d, provs, o, s, owners in observation.import_records
        ],  # B332/RC-3: origen relativo a sys.prefix + sha256 gobernado + providers + dueños del RECORD del origen
        "tensor_checksum": observation.checksum,
    }
    return [], receipt


def observe_runtime(lock_rel: str) -> tuple[list[str], DeepObservation | None]:
    """B331: recoge el estado REAL del entorno deep instalado en un `DeepObservation`. El contrato SÓLO se usa para saber
    QUÉ observar (certify lo RECARGA como autoridad). B316: imports ESTÁTICOS, sin fábrica dinámica. B323: una comprobación
    RUNTIME (no `assert`) exige que el stack importado == módulos del contrato. B332: cada import se liga a su distribución
    y origen por DESCRIPTOR gobernado (`governed_import_identity`)."""
    contract = load_contract()
    # B327/RC-3: inventario OBSERVADO sin last-wins (dist-info normalizada duplicada = problema); se observan TODAS las
    # distribuciones providers (mlflow multi-provider incluido). Una que NO esté instalada queda fuera y la caza la
    # igualdad de conjuntos de evaluate() (contra contract.expected_providers).
    dist_inv, id_probs = _distribution_inventory()
    installed = {p: dist_inv[lc._norm(p)] for p in contract.expected_providers if lc._norm(p) in dist_inv}
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

    # B332: liga CADA módulo importado a su distribución + origen REAL por DESCRIPTOR gobernado (openat/O_NOFOLLOW, hash
    # desde el fd, pertenencia a Distribution.files) — un `__spec__.origin` forjado/inexistente ya NO pasa por strings.
    from importlib.metadata import PackageNotFoundError, distribution, packages_distributions

    by_name = {m.__name__.split(".")[0]: m for m in _imported}
    pkg_dists = packages_distributions()
    records: list[tuple[str, str, tuple[str, ...], str, str, tuple[str, ...]]] = []
    for module, dist, provs in contract.entries:
        mod = by_name.get(module)
        spec = mod.__spec__ if mod is not None else None
        origin = spec.origin if spec is not None else None
        # RC-3: RECORD de CADA provider (None si la distribución no declara ficheros) para probar pertenencia del origen.
        provider_files: dict[str, list[str] | None] = {}
        for p in provs:
            try:
                provider_files[p] = [str(f.locate()) for f in (distribution(p).files or [])]
            except PackageNotFoundError:
                provider_files[p] = None
        iprobs, ident = gi.governed_identity(
            module,
            providers=list(provs),
            primary=dist,
            origin=origin,
            providing=list(pkg_dists.get(module, [])),
            provider_files=provider_files,
            sys_prefix=sys.prefix,
        )
        id_probs.extend(iprobs)
        if ident is not None:
            records.append((ident.module, ident.distribution, ident.providers, ident.origin, ident.origin_sha256, ident.origin_owners))  # fmt: skip
        else:
            records.append((module, dist, provs, "unknown", "unknown", ()))

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
    observation = DeepObservation(
        py_version=platform.python_version(),
        system=platform.system(),
        machine=platform.machine(),
        installed=tuple(sorted(installed.items())),
        torch_version=torch.__version__,
        pip_check_ok=pip_ok,
        checksum=checksum,
        commit_sha=commit or "unknown",
        import_records=tuple(records),
        identity_problems=tuple(id_probs),
    )
    return id_probs, observation


def run(lock_rel: str) -> tuple[list[str], dict]:
    """B331/B335: delega EXCLUSIVAMENTE a `certify_runtime`, que observa el entorno real y certifica contra el contrato
    canónico. No existe ruta pública que acepte una observación suministrada por el caller."""
    return certify_runtime(lock_rel)


def write_receipt_governed(name: str, data: dict) -> None:
    """B328/B334: escritura GOBERNADA del recibo. Delega a `governed_receipt_io.write_receipt`: NOMBRE SIMPLE en el
    directorio autorizado (CWD) abierto como fd de directorio, leaf `O_CREAT|O_EXCL|O_NOFOLLOW` 0600 relativo a ese fd (sin
    cadena de ancestros que un symlink pueda desviar), fstat + write-all + fsync de fichero y directorio."""
    from tools.governed_receipt_io import write_receipt

    write_receipt(name, data)


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
