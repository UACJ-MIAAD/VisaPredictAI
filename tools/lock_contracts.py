#!/usr/bin/env python
"""Contrato ESTÁTICO ÚNICO de la matriz de locks (P0R.4R, ronda 10).

Fuente única de verdad de la estructura de `locks/` + `requirements/` + el manifiesto
`locks/lockset.json`. Lo consumen TANTO el promotor (`tools/promote_lockset.py`, antes de
promover el staging) como el auditor (`tools/audit_python_supply_chain.py`, antes de reconciliar)
y el generador (`tools/make_locks.sh`, sobre el staging). NO debe existir una segunda
implementación del contrato. Stdlib puro, sin dependencias externas.

Responsabilidades (valida y BLOQUEA ante):
  1. los 9 locks y las 7 fuentes gobernadas exactas (incluye make_locks.sh/promote_lockset.py/
     lock_contracts.py: alterar el generador cambia el hash de fuentes del manifiesto);
  2. pins duplicados dentro de un lock;
  3. un pin de un lock HASHEADO sin al menos un sha256;
  4. rutas temporales, URLs con credenciales u opciones desconocidas;
  5. wrappers CPU/CUDA alterados (línea por línea);
  6. índices incorrectos por plataforma;
  7. versiones directas que divergen de requirements/deep.in;
  8. divergencia de versión PÚBLICA de un paquete común entre los 3 locks deep;
  9. esquema estricto de lockset.json;
 10. hash de lock o de fuente incorrecto (recálculo);
 11. conteo de pins incorrecto;
 12. Python completo (X.Y.Z, X.Y==3.14), plataforma y toolchain ausentes/incorrectos en el manifiesto.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_REL = "locks/lockset.json"

# (rel, perfil, hasheado). runtime/dev linux + los 3 deep llevan hashes (se instalan con
# --require-hashes); los 3 macOS base + model-cpu-linux NO (model-cpu-linux se consume como -c).
LOCK_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("locks/runtime.txt", "runtime", False),
    ("locks/runtime-linux-x86_64.txt", "runtime", True),
    ("locks/dev.txt", "dev", False),
    ("locks/dev-linux-x86_64.txt", "dev", True),
    ("locks/model-cpu.txt", "model", False),
    ("locks/model-cpu-linux-x86_64.txt", "model", False),
    ("locks/deep-macos-arm64.txt", "deep", True),
    ("locks/deep-linux-x86_64-cpu.txt", "deep", True),
    ("locks/deep-linux-x86_64-cu126.txt", "deep", True),
)
LOCK_NAMES = tuple(rel.split("/", 1)[1] for rel, _p, _h in LOCK_SPECS)
DEEP_LOCKS = tuple(rel for rel, p, _h in LOCK_SPECS if p == "deep")
PROFILES = ("runtime", "dev", "model", "deep")

# Fuentes GOBERNADAS: su hash entra al manifiesto. Incluye los 3 scripts del contrato de locks.
SOURCES: tuple[str, ...] = (
    "pyproject.toml",
    "requirements/deep.in",
    "requirements/deep-linux-cpu.in",
    "requirements/deep-linux-cu126.in",
    "tools/make_locks.sh",
    "tools/promote_lockset.py",
    "tools/lock_contracts.py",
)

# Cierre DIRECTO de requirements/deep.in (torch va aparte, por variante de plataforma).
DEEP_DIRECT: dict[str, str] = {
    "neuralforecast": "3.1.9",
    "optuna": "4.9.0",
    "ray": "2.56.0",
    "mlflow": "3.14.0",
    "pyarrow": "24.0.0",
    "numpy": "2.4.6",
    "pandas": "2.3.3",
    "scipy": "1.17.1",
    "pytorch-lightning": "2.5.6",
    "chronos-forecasting": "2.3.1",
    "transformers": "5.13.1",
    "pillow": "12.3.0",
    "setuptools": "81.0.0",
}
DEEP_TORCH: dict[str, str] = {
    "locks/deep-macos-arm64.txt": "2.12.1",
    "locks/deep-linux-x86_64-cpu.txt": "2.12.1+cpu",
    "locks/deep-linux-x86_64-cu126.txt": "2.12.1+cu126",
}
# Consultas de versión LOCAL -> PÚBLICA para el auditor (derivadas de los torch con sufijo local).
LOCAL_VERSION_QUERIES: dict[tuple[str, str], str] = {
    (rel, "torch"): ver.split("+", 1)[0] for rel, ver in DEEP_TORCH.items() if "+" in ver
}

_PYPI = "--index-url https://pypi.org/simple"
DEEP_INDEX: dict[str, list[str]] = {
    "locks/deep-macos-arm64.txt": [_PYPI],
    "locks/deep-linux-x86_64-cpu.txt": [_PYPI, "--extra-index-url https://download.pytorch.org/whl/cpu"],
    "locks/deep-linux-x86_64-cu126.txt": [_PYPI, "--extra-index-url https://download.pytorch.org/whl/cu126"],
}
# Wrappers: líneas NO-comentario exactas (orden incluido).
WRAPPERS: dict[str, list[str]] = {
    "requirements/deep-linux-cpu.in": ["--extra-index-url https://download.pytorch.org/whl/cpu", "-r deep.in"],
    "requirements/deep-linux-cu126.in": ["--extra-index-url https://download.pytorch.org/whl/cu126", "-r deep.in"],
}
TOOLCHAIN: dict[str, str] = {"pip": "26.1.2", "setuptools": "81.0.0", "wheel": "0.47.0", "uv": "0.11.28"}
PY_SERIES = "3.14"  # el manifiesto debe registrar el Python COMPLETO X.Y.Z con X.Y == PY_SERIES

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]*\])?==([^\s\\;]+)")
_CREDS = re.compile(r"://[^/\s]*@")
_ALLOWED_OPT = ("--hash=", "--index-url", "--extra-index-url")


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def parse_lock(text: str) -> tuple[list[dict], list[str], list[str]]:
    """-> (entries, index_options, problems). entries: {name, version, hashes:[...]}."""
    entries: list[dict] = []
    index_opts: list[str] = []
    problems: list[str] = []
    cur: dict | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("--hash="):
            if cur is None:
                problems.append("--hash sin pin previo")
            else:
                cur["hashes"].append(s.split("--hash=", 1)[1].rstrip(" \\"))
            continue
        if s.startswith(("--index-url", "--extra-index-url")):
            index_opts.append(s.rstrip(" \\"))
            continue
        if s.startswith("-") and not s.startswith(_ALLOWED_OPT):
            problems.append(f"opción desconocida: {s[:60]!r}")
            continue
        m = _PIN.match(s)
        if not m:
            problems.append(f"línea no reconocida: {s[:60]!r}")
            continue
        cur = {"name": _norm(m.group(1)), "version": m.group(2), "hashes": []}
        entries.append(cur)
    return entries, index_opts, problems


def pin_map(text: str) -> dict[str, str]:
    """paquete -> versión, LANZANDO ValueError ante pin duplicado (resp. 2)."""
    entries, _idx, probs = parse_lock(text)
    if probs:
        raise ValueError("; ".join(probs))
    seen: dict[str, str] = {}
    for e in entries:
        if e["name"] in seen:
            raise ValueError(f"pin duplicado: {e['name']}")
        seen[e["name"]] = e["version"]
    return seen


def validate_lock_text(rel: str, text: str, hashed: bool) -> list[str]:
    probs: list[str] = []
    if "/var/folders" in text or "/tmp/" in text or "vp_locks_staged" in text:
        probs.append(f"[{rel}] ruta temporal en el lock")
    if _CREDS.search(text):
        probs.append(f"[{rel}] URL con credenciales")
    entries, index_opts, parse_probs = parse_lock(text)
    probs += [f"[{rel}] {p}" for p in parse_probs]
    seen: set[str] = set()
    for e in entries:
        if e["name"] in seen:
            probs.append(f"[{rel}] pin duplicado: {e['name']}")
        seen.add(e["name"])
        if hashed and not e["hashes"]:
            probs.append(f"[{rel}] pin sin hash: {e['name']}=={e['version']}")
        if hashed and any(not h.startswith("sha256:") for h in e["hashes"]):
            probs.append(f"[{rel}] hash no-sha256 en {e['name']}")
    if rel in DEEP_INDEX:
        if index_opts != DEEP_INDEX[rel]:
            probs.append(f"[{rel}] índices {index_opts} != esperado {DEEP_INDEX[rel]}")
    elif index_opts:
        probs.append(f"[{rel}] no debería declarar índices, tiene {index_opts}")
    return probs


def validate_wrappers(root: Path = ROOT) -> list[str]:
    probs: list[str] = []
    for rel, expected in WRAPPERS.items():
        p = root / rel
        if not p.exists():
            probs.append(f"wrapper ausente: {rel}")
            continue
        lines = [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if lines != expected:
            probs.append(f"[{rel}] wrapper {lines} != esperado {expected}")
    return probs


def _lock_path(rel: str, root: Path, locks_dir: Path | None) -> Path:
    """Ubica un lock: en el repo (root/rel) o, si se valida un staging, en locks_dir/<nombre plano>."""
    return (locks_dir / rel.split("/", 1)[1]) if locks_dir is not None else (root / rel)


def validate_deep_direct(root: Path = ROOT, locks_dir: Path | None = None) -> list[str]:
    """Cada lock deep fija EXACTO el cierre directo de deep.in (+ torch por variante)."""
    probs: list[str] = []
    # deep.in coincide con DEEP_DIRECT + torch==2.12.1 (siempre desde el repo)
    deep_in = root / "requirements/deep.in"
    if deep_in.exists():
        try:
            pins = pin_map(deep_in.read_text())
        except ValueError as exc:
            probs.append(f"[requirements/deep.in] {exc}")
            pins = {}
        for pkg, ver in {**DEEP_DIRECT, "torch": "2.12.1"}.items():
            if pins.get(pkg) != ver:
                probs.append(f"[requirements/deep.in] {pkg}: {pins.get(pkg)} != {ver}")
    for rel in DEEP_LOCKS:
        p = _lock_path(rel, root, locks_dir)
        if not p.exists():
            probs.append(f"deep lock ausente: {rel}")
            continue
        try:
            pins = pin_map(p.read_text())
        except ValueError as exc:
            probs.append(f"[{rel}] {exc}")
            continue
        for pkg, ver in DEEP_DIRECT.items():
            if pins.get(pkg) != ver:
                probs.append(f"[{rel}] {pkg}: {pins.get(pkg)} != {ver} (deep.in)")
        if pins.get("torch") != DEEP_TORCH[rel]:
            probs.append(f"[{rel}] torch: {pins.get('torch')} != {DEEP_TORCH[rel]}")
    return probs


def validate_deep_cross_platform(root: Path = ROOT, locks_dir: Path | None = None) -> list[str]:
    """Un paquete presente en los 3 locks deep debe tener la MISMA versión PÚBLICA (sin sufijo local)."""
    probs: list[str] = []
    maps: dict[str, dict[str, str]] = {}
    for rel in DEEP_LOCKS:
        p = _lock_path(rel, root, locks_dir)
        if not p.exists():
            return [f"deep lock ausente: {rel}"]
        try:
            maps[rel] = pin_map(p.read_text())
        except ValueError as exc:
            return [f"[{rel}] {exc}"]
    common = set.intersection(*[set(m) for m in maps.values()])
    for pkg in sorted(common):
        publics = {rel: maps[rel][pkg].split("+", 1)[0] for rel in DEEP_LOCKS}
        if len(set(publics.values())) != 1:
            probs.append(f"divergencia de versión pública de {pkg} entre deep: {publics}")
    return probs


def validate_manifest(manifest: dict, root: Path = ROOT) -> list[str]:
    """Esquema estricto + recálculo de hashes de locks y fuentes + conteos + Python/plataforma/toolchain."""
    probs: list[str] = []
    if set(manifest) != {"schema_version", "generator", "sources", "locks"}:
        probs.append(f"manifiesto: claves {sorted(manifest)} != esperado")
        return probs
    if manifest.get("schema_version") != 1:
        probs.append(f"manifiesto: schema_version {manifest.get('schema_version')} != 1")
    gen = manifest.get("generator", {})
    if set(gen) != {"python", "platform", "pip", "setuptools", "wheel", "uv"}:
        probs.append(f"manifiesto.generator: claves {sorted(gen)} != esperado")
    else:
        py = str(gen["python"]).split(".")
        if len(py) < 3 or ".".join(py[:2]) != PY_SERIES:
            probs.append(f"manifiesto.generator.python {gen['python']!r} no es X.Y.Z con X.Y=={PY_SERIES}")
        if not str(gen["platform"]).strip():
            probs.append("manifiesto.generator.platform vacío")
        for k, v in TOOLCHAIN.items():
            if gen.get(k) != v:
                probs.append(f"manifiesto.generator.{k} {gen.get(k)!r} != {v}")
    # fuentes: exactamente las 7, hash recalculado
    if set(manifest.get("sources", {})) != set(SOURCES):
        probs.append(f"manifiesto.sources {sorted(manifest.get('sources', {}))} != {sorted(SOURCES)}")
    else:
        for s in SOURCES:
            actual = _sha256((root / s).read_bytes()) if (root / s).exists() else "MISSING"
            if manifest["sources"][s] != actual:
                probs.append(f"manifiesto.sources[{s}] hash != real")
    # locks: exactamente los 9, hash + conteo de pins recalculados
    exp_keys = {f"locks/{n}" for n in LOCK_NAMES}
    if set(manifest.get("locks", {})) != exp_keys:
        probs.append(f"manifiesto.locks {sorted(manifest.get('locks', {}))} != los 9")
    else:
        for rel in exp_keys:
            p = root / rel
            if not p.exists():
                probs.append(f"manifiesto.locks[{rel}] archivo ausente")
                continue
            entry = manifest["locks"][rel]
            if set(entry) != {"sha256", "pins"}:
                probs.append(f"manifiesto.locks[{rel}] claves {sorted(entry)} != {{sha256,pins}}")
                continue
            if entry["sha256"] != _sha256(p.read_bytes()):
                probs.append(f"manifiesto.locks[{rel}] sha256 != real")
            try:
                n = len(pin_map(p.read_text()))
            except ValueError as exc:
                probs.append(f"manifiesto.locks[{rel}] {exc}")
                continue
            if entry["pins"] != n:
                probs.append(f"manifiesto.locks[{rel}] pins {entry['pins']} != {n} real")
    return probs


def validate_files(root: Path = ROOT, locks_dir: Path | None = None) -> list[str]:
    """Los 9 locks + wrappers + deep-direct + cross-platform. NO valida el manifiesto (ver validate_manifest).
    Con locks_dir se validan los locks de un directorio de STAGING (planos, sin prefijo locks/)."""
    probs: list[str] = []
    for rel, _profile, hashed in LOCK_SPECS:
        p = _lock_path(rel, root, locks_dir)
        if not p.exists():
            probs.append(f"lock ausente: {rel}")
            continue
        probs += validate_lock_text(rel, p.read_text(), hashed)
    probs += validate_wrappers(root)
    probs += validate_deep_direct(root, locks_dir)
    probs += validate_deep_cross_platform(root, locks_dir)
    return probs


def validate_staging(staged: Path, root: Path = ROOT) -> list[str]:
    """Contrato estático sobre un STAGING (9 locks planos), SIN manifiesto — lo escribe el promotor después."""
    return validate_files(root, locks_dir=staged)


def validate_all(root: Path = ROOT, *, manifest: dict | None = None, require_manifest: bool = True) -> list[str]:
    """Contrato completo. Si require_manifest, lee y valida locks/lockset.json."""
    probs = validate_files(root)
    if manifest is None and require_manifest:
        mp = root / MANIFEST_REL
        if not mp.exists():
            probs.append(f"manifiesto ausente: {MANIFEST_REL}")
            return probs
        try:
            manifest = json.loads(mp.read_text())
        except ValueError as exc:
            probs.append(f"manifiesto ilegible: {exc}")
            return probs
    if manifest is not None:
        probs += validate_manifest(manifest, root)
    return probs


def main() -> int:
    probs = validate_all()
    if probs:
        print(f"✗ CONTRATO DE LOCKS incoherente ({len(probs)}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ Contrato de locks OK: 9 locks + {len(SOURCES)} fuentes + manifiesto coherentes")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
