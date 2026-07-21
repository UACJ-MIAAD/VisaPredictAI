#!/usr/bin/env python
"""Contrato ESTÁTICO ÚNICO de la matriz de locks (P0R.4R, ronda 10).

Fuente única de verdad de la estructura de `locks/` + `requirements/` + el manifiesto
`locks/lockset.json`. Lo consumen TANTO el promotor (`tools/promote_lockset.py`, antes de
promover el staging) como el auditor (`tools/audit_python_supply_chain.py`, antes de reconciliar)
y el generador (`tools/make_locks.sh`, sobre el staging). NO debe existir una segunda
implementación del contrato. Stdlib puro, sin dependencias externas.

Responsabilidades (valida y BLOQUEA ante):
  1. los 11 locks y las 8 fuentes gobernadas exactas (incluye make_locks.sh/promote_lockset.py/
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
    # dvc-tool: herramienta CLI gobernada y AISLADA (no dependencia de producto); hasheada.
    ("locks/dvc-tool-macos-arm64.txt", "dvc-tool", True),
    ("locks/dvc-tool-linux-x86_64.txt", "dvc-tool", True),
)
LOCK_NAMES = tuple(rel.split("/", 1)[1] for rel, _p, _h in LOCK_SPECS)
DEEP_LOCKS = tuple(rel for rel, p, _h in LOCK_SPECS if p == "deep")
DVC_TOOL_LOCKS = tuple(rel for rel, p, _h in LOCK_SPECS if p == "dvc-tool")
PROFILES = ("runtime", "dev", "model", "deep", "dvc-tool")

# Fuentes GOBERNADAS: su hash entra al manifiesto. Incluye los 3 scripts del contrato de locks.
SOURCES: tuple[str, ...] = (
    "pyproject.toml",
    "requirements/deep.in",
    "requirements/deep-linux-cpu.in",
    "requirements/deep-linux-cu126.in",
    "requirements/dvc.in",
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
    "setuptools": "83.0.0",
}
# Fuente ÚNICA de la versión pública de torch. P0R.4R3: 2.12.1 -> 2.13.0 (torch 2.12.1 exigía
# setuptools<82, incompatible con el fix setuptools 83; torch 2.13.0 permite setuptools>=77.0.3).
TORCH_PUBLIC = "2.13.0"
DEEP_TORCH: dict[str, str] = {
    "locks/deep-macos-arm64.txt": TORCH_PUBLIC,
    "locks/deep-linux-x86_64-cpu.txt": f"{TORCH_PUBLIC}+cpu",
    "locks/deep-linux-x86_64-cu126.txt": f"{TORCH_PUBLIC}+cu126",
}
# Consultas de versión LOCAL -> PÚBLICA para el auditor (derivadas de los torch con sufijo local).
LOCAL_VERSION_QUERIES: dict[tuple[str, str], str] = {
    (rel, "torch"): ver.split("+", 1)[0] for rel, ver in DEEP_TORCH.items() if "+" in ver
}

# Perfil dvc-tool: cierre DIRECTO de requirements/dvc.in. dvc-s3 se pinea directo (capacidad S3).
DVC_TOOL_DIRECT: dict[str, str] = {"dvc": "3.67.1", "dvc-s3": "3.3.0"}
# diskcache arrastrado por dvc-data; PYSEC-2026-2447 SIN fix — aceptado SOLO en dvc-tool. Se fija
# la versión exacta para que un bump silencioso (que cerrara/moviera el aviso) rompa el contrato.
DVC_TOOL_DISKCACHE = "5.6.3"
# Paquetes que JAMÁS deben aparecer en un lock de PRODUCTO (runtime/dev/model/deep): entran solo
# vía dvc y su aviso está acotado a dvc-tool. Su aparición fuera bloquea.
DVC_TOOL_EXCLUSIVE = ("dvc", "dvc-s3", "dvc-data", "dvc-objects", "diskcache")

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
TOOLCHAIN: dict[str, str] = {"pip": "26.1.2", "setuptools": "83.0.0", "wheel": "0.47.0", "uv": "0.11.28"}
PLATFORM_EXPECTED = "Darwin arm64"  # plataforma de referencia EXACTA del manifiesto

# Expectativas de ejecución por lock deep, DERIVADAS del contrato (no de la matriz del workflow):
# el smoke las consume para que la matriz no se autoconfirme (B3). torch se toma de DEEP_TORCH (no
# se repite el literal). Fuente única.
DEEP_RUNTIME: dict[str, dict[str, str]] = {
    "locks/deep-macos-arm64.txt": {
        "variant": "macos-arm64",
        "system": "Darwin",
        "machine": "arm64",
        "torch": DEEP_TORCH["locks/deep-macos-arm64.txt"],
    },
    "locks/deep-linux-x86_64-cpu.txt": {
        "variant": "linux-cpu",
        "system": "Linux",
        "machine": "x86_64",
        "torch": DEEP_TORCH["locks/deep-linux-x86_64-cpu.txt"],
    },
    "locks/deep-linux-x86_64-cu126.txt": {
        "variant": "linux-cu126",
        "system": "Linux",
        "machine": "x86_64",
        "torch": DEEP_TORCH["locks/deep-linux-x86_64-cu126.txt"],
    },
}

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]*\])?==([^\s\\;]+)")
_CREDS = re.compile(r"://[^/\s]*@")
_ALLOWED_OPT = ("--hash=", "--index-url", "--extra-index-url")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")  # hash ESTRICTO (sha256: vacío no vale)
_PY_RE = re.compile(r"^3\.14\.\d+$")  # Python COMPLETO 3.14.Z


def load_json_no_dupes(path: Path):
    """json.load rechazando claves duplicadas (una llave repetida en lockset.json es tampering)."""

    def _no_dupes(pairs):
        seen: dict = {}
        for k, v in pairs:
            if k in seen:
                raise ValueError(f"clave JSON duplicada: {k!r}")
            seen[k] = v
        return seen

    return json.loads(path.read_text(), object_pairs_hook=_no_dupes)


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _reject_symlink(p: Path, root: Path, label: str) -> list[str]:
    """Rechaza symlinks y rutas que resuelven FUERA de la raíz (evita escapes de árbol)."""
    probs: list[str] = []
    if p.is_symlink():
        probs.append(f"{label}: es un symlink ({p})")
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError, OSError:
        probs.append(f"{label}: resuelve fuera de la raíz ({p})")
    return probs


def validate_generator(gen: dict) -> list[str]:
    """Objeto generator ESTRICTO: 6 claves exactas, Python 3.14.Z, plataforma exacta, toolchain fijo.
    El promotor lo llama ANTES del primer rename (no tras promover nueve archivos)."""
    if not isinstance(gen, dict) or set(gen) != {"python", "platform", "pip", "setuptools", "wheel", "uv"}:
        return [f"generator: claves {sorted(gen) if isinstance(gen, dict) else gen!r} != esperado"]
    probs: list[str] = []
    if not _PY_RE.match(str(gen["python"])):
        probs.append(f"generator.python {gen['python']!r} no es 3.14.Z")
    if gen["platform"] != PLATFORM_EXPECTED:
        probs.append(f"generator.platform {gen['platform']!r} != {PLATFORM_EXPECTED!r}")
    for k, v in TOOLCHAIN.items():
        if gen.get(k) != v:
            probs.append(f"generator.{k} {gen.get(k)!r} != {v}")
    return probs


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
        if hashed and any(not _HASH_RE.match(h) for h in e["hashes"]):
            probs.append(f"[{rel}] hash no es sha256:<64 hex> en {e['name']}")
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
        probs += _reject_symlink(p, root, f"[{rel}]")
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
    # deep.in DEBE existir y contener EXACTAMENTE DEEP_DIRECT + torch==TORCH_PUBLIC (sin pins extra)
    deep_in = root / "requirements/deep.in"
    if not deep_in.exists():
        probs.append("requirements/deep.in ausente")
    else:
        expected = {**DEEP_DIRECT, "torch": TORCH_PUBLIC}
        try:
            pins = pin_map(deep_in.read_text())
        except ValueError as exc:
            probs.append(f"[requirements/deep.in] {exc}")
            pins = {}
        if set(pins) != set(expected):
            probs.append(f"[requirements/deep.in] pins {sorted(set(pins) ^ set(expected))} != conjunto gobernado")
        for pkg, ver in expected.items():
            if pins.get(pkg) is not None and pins.get(pkg) != ver:
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


def validate_dvc_tool(root: Path = ROOT, locks_dir: Path | None = None) -> list[str]:
    """dvc.in fija EXACTO {dvc, dvc-s3}; ambos locks dvc-tool fijan DVC_TOOL_DIRECT + diskcache==5.6.3;
    versiones públicas comunes idénticas macOS/Linux."""
    probs: list[str] = []
    dvc_in = root / "requirements/dvc.in"
    if not dvc_in.exists():
        probs.append("requirements/dvc.in ausente")
    else:
        try:
            pins = pin_map(dvc_in.read_text())
        except ValueError as exc:
            probs.append(f"[requirements/dvc.in] {exc}")
            pins = {}
        if set(pins) != set(DVC_TOOL_DIRECT):
            probs.append(f"[requirements/dvc.in] pins {sorted(set(pins) ^ set(DVC_TOOL_DIRECT))} != {{dvc, dvc-s3}}")
        for pkg, ver in DVC_TOOL_DIRECT.items():
            if pins.get(pkg) is not None and pins.get(pkg) != ver:
                probs.append(f"[requirements/dvc.in] {pkg}: {pins.get(pkg)} != {ver}")
    maps: dict[str, dict[str, str]] = {}
    for rel in DVC_TOOL_LOCKS:
        p = _lock_path(rel, root, locks_dir)
        if not p.exists():
            probs.append(f"dvc-tool lock ausente: {rel}")
            continue
        try:
            maps[rel] = pin_map(p.read_text())
        except ValueError as exc:
            probs.append(f"[{rel}] {exc}")
            continue
        for pkg, ver in DVC_TOOL_DIRECT.items():
            if maps[rel].get(pkg) != ver:
                probs.append(f"[{rel}] {pkg}: {maps[rel].get(pkg)} != {ver} (dvc.in)")
        if maps[rel].get("diskcache") != DVC_TOOL_DISKCACHE:
            probs.append(
                f"[{rel}] diskcache: {maps[rel].get('diskcache')} != {DVC_TOOL_DISKCACHE} (PYSEC-2026-2447 acotado)"
            )
    if len(maps) == len(DVC_TOOL_LOCKS):
        common = set.intersection(*[set(m) for m in maps.values()])
        for pkg in sorted(common):
            publics = {rel: maps[rel][pkg].split("+", 1)[0] for rel in DVC_TOOL_LOCKS}
            if len(set(publics.values())) != 1:
                probs.append(f"divergencia de versión pública de {pkg} entre dvc-tool: {publics}")
    return probs


def validate_no_dvc_in_product(root: Path = ROOT, locks_dir: Path | None = None) -> list[str]:
    """Ningún lock de PRODUCTO (runtime/dev/model/deep) puede contener paquetes exclusivos de dvc-tool
    (dvc/dvc-s3/dvc-data/dvc-objects/diskcache): su aviso está acotado a dvc-tool."""
    probs: list[str] = []
    for rel, profile, _h in LOCK_SPECS:
        if profile == "dvc-tool":
            continue
        p = _lock_path(rel, root, locks_dir)
        if not p.exists():
            continue
        try:
            m = pin_map(p.read_text())
        except ValueError:
            continue
        for pkg in DVC_TOOL_EXCLUSIVE:
            if pkg in m:
                probs.append(f"[{rel}] contiene {pkg}=={m[pkg]} — exclusivo de dvc-tool, prohibido en {profile}")
    return probs


def validate_manifest(manifest: dict, root: Path = ROOT) -> list[str]:
    """Esquema estricto + recálculo de hashes de locks y fuentes + conteos + Python/plataforma/toolchain."""
    probs: list[str] = []
    if set(manifest) != {"schema_version", "generator", "sources", "locks"}:
        probs.append(f"manifiesto: claves {sorted(manifest)} != esperado")
        return probs
    if manifest.get("schema_version") != 1:
        probs.append(f"manifiesto: schema_version {manifest.get('schema_version')} != 1")
    probs += [f"manifiesto.{p}" for p in validate_generator(manifest.get("generator", {}))]
    # fuentes: exactamente las 7, hash recalculado
    if set(manifest.get("sources", {})) != set(SOURCES):
        probs.append(f"manifiesto.sources {sorted(manifest.get('sources', {}))} != {sorted(SOURCES)}")
    else:
        for s in SOURCES:
            actual = _sha256((root / s).read_bytes()) if (root / s).exists() else "MISSING"
            if manifest["sources"][s] != actual:
                probs.append(f"manifiesto.sources[{s}] hash != real")
    # locks: exactamente los 11, hash + conteo de pins recalculados
    exp_keys = {f"locks/{n}" for n in LOCK_NAMES}
    if set(manifest.get("locks", {})) != exp_keys:
        probs.append(f"manifiesto.locks {sorted(manifest.get('locks', {}))} != los 11")
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
    """Los 11 locks + wrappers + deep-direct + cross-platform. NO valida el manifiesto (ver validate_manifest).
    Con locks_dir se validan los locks de un directorio de STAGING (planos, sin prefijo locks/)."""
    probs: list[str] = []
    # conjunto EXACTO de .txt (ni faltantes ni adicionales), en el repo o en el staging
    txt_dir = locks_dir if locks_dir is not None else (root / "locks")
    present_txt = {p.name for p in txt_dir.glob("*.txt")} if txt_dir.exists() else set()
    if present_txt != set(LOCK_NAMES):
        probs.append(
            f"conjunto de .txt {sorted(present_txt)} != los esperados (dif {sorted(present_txt ^ set(LOCK_NAMES))})"
        )
    for rel, _profile, hashed in LOCK_SPECS:
        p = _lock_path(rel, root, locks_dir)
        if not p.exists():
            probs.append(f"lock ausente: {rel}")
            continue
        probs += _reject_symlink(p, root if locks_dir is None else locks_dir, f"[{rel}]")
        probs += validate_lock_text(rel, p.read_text(), hashed)
    probs += validate_wrappers(root)
    probs += validate_deep_direct(root, locks_dir)
    probs += validate_deep_cross_platform(root, locks_dir)
    probs += validate_dvc_tool(root, locks_dir)
    probs += validate_no_dvc_in_product(root, locks_dir)
    return probs


def validate_staging(staged: Path, root: Path = ROOT) -> list[str]:
    """Contrato estático sobre un STAGING (11 locks planos), SIN manifiesto — lo escribe el promotor después."""
    return validate_files(root, locks_dir=staged)


def validate_all(root: Path = ROOT, *, manifest: dict | None = None, require_manifest: bool = True) -> list[str]:
    """Contrato completo. Si require_manifest, lee y valida locks/lockset.json."""
    probs = validate_files(root)
    # symlinks en las fuentes gobernadas (los locks/wrappers ya se chequean en validate_files)
    for s in SOURCES:
        sp = root / s
        if sp.exists():
            probs += _reject_symlink(sp, root, f"fuente {s}")
    if manifest is None and require_manifest:
        mp = root / MANIFEST_REL
        if not mp.exists():
            probs.append(f"manifiesto ausente: {MANIFEST_REL}")
            return probs
        probs += _reject_symlink(mp, root, "manifiesto")
        try:
            manifest = load_json_no_dupes(mp)  # rechaza claves duplicadas
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
    print(f"✓ Contrato de locks OK: {len(LOCK_NAMES)} locks + {len(SOURCES)} fuentes + manifiesto coherentes")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
