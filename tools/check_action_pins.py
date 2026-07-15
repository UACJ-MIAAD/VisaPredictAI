#!/usr/bin/env python
"""Gate de pins de GitHub Actions (P0R.5, R8R6R/B49 + R8R6R2/B53 + R8R6R3/B56/B57). Registro POSITIVO
`security/github_actions.json`: toda acción de terceros DEBE estar declarada con su SHA EXACTO de 40 hex,
su comentario de versión y runtime `node24`. El gate:
- carga el registro con RECHAZO DE CLAVES DUPLICADAS, `type(schema_version) is int`, y claves superiores
  EXACTAS (`schema_version`/`note`/`actions`);
- **parsea cada workflow como YAML ESTRUCTURAL** (loader que rechaza claves duplicadas) y recorre TODOS los
  mappings buscando la clave EXACTA `uses` — pasos normales y reusable-workflows a nivel de job, `.yml` y
  `.yaml`, con o sin espacio antes del `:` (B56: `uses :` es YAML válido y el escaneo por texto no lo veía),
  valores no-string, expresiones dinámicas `${{ }}` y YAML inválido (todo fail-closed);
- exige SHA de 40 hex + registro positivo (biyección acción-usada ⇔ acción-registrada) + comentario de
  versión (extraído del texto crudo, tolerante a espacios) + runtime node24.
Con `--online` verifica además, vía GitHub API, que cada SHA existe y el endpoint de commit lo confirma, que
la versión documentada apunta a ese SHA, y que su `action.yml`/`action.yaml` declara `runs.using: node24`
PARSEADO como YAML (B57: un `using: node24` en un comentario/descripción NO cuenta) — job requerido de CI,
fail-closed.

    python -m tools.check_action_pins            # gate offline
    python -m tools.check_action_pins --online   # + verificación remota de SHA/versión/runtime
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "security" / "github_actions.json"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# acción@ref ESTRUCTURAL (el valor ya viene del YAML parseado, sin comentario).
_USES_VALUE = re.compile(r"^([^@\s]+)@([^\s#]+)$")
# comentario de versión desde el TEXTO crudo, tolerante a espacios alrededor del `:` (defensa/legibilidad).
_USES_COMMENT = re.compile(r"uses\s*:\s*([^\s@#]+)@([0-9a-f]{40})\s*(?:#\s*(\S+))?")
# Tripwire de defensa en profundidad: SHA Node 20 deprecados (el registro positivo YA los rechazaría).
_NODE20_SHAS = {
    "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "fa0a91b85d4f404e444e00e005971372dc801d16",
    "7474bc4690e29a8392af63c5b98e7449536d5c3a",
}
_REG_TOP = {"schema_version", "note", "actions"}
_ENTRY_KEYS = {"sha", "version", "runtime"}


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader que RECHAZA claves de mapping duplicadas (B56: dos `uses:` en un step, una podría
    enmascarar a la otra)."""


def _no_dup_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(None, None, f"clave YAML duplicada: {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def _no_dup(pairs):
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise SystemExit(f"check_action_pins: clave duplicada en el registro: {k!r}")
        d[k] = v
    return d


def load_registry(registry: Path = REGISTRY) -> dict[str, dict[str, str]]:
    doc = json.loads(registry.read_text(), object_pairs_hook=_no_dup)  # B53: rechaza claves duplicadas
    if set(doc) != _REG_TOP:
        raise SystemExit(f"check_action_pins: claves superiores {sorted(doc)} != {sorted(_REG_TOP)}")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:  # B53: True no es 1
        raise SystemExit("check_action_pins: schema_version no es int == 1")
    if not isinstance(doc["actions"], dict) or not doc["actions"]:
        raise SystemExit("check_action_pins: `actions` no es objeto no vacío")
    for name, e in doc["actions"].items():
        if (
            not isinstance(e, dict)
            or set(e) != _ENTRY_KEYS
            or not (isinstance(e["sha"], str) and _SHA_RE.fullmatch(e["sha"]))
            or not (isinstance(e["version"], str) and e["version"])
            or e["runtime"] != "node24"
        ):
            raise SystemExit(f"check_action_pins: entrada de registro inválida para {name!r}")
    return doc["actions"]


def _workflows(root: Path) -> list[Path]:
    wf = root / ".github" / "workflows"
    return sorted([*wf.glob("*.yml"), *wf.glob("*.yaml")])


def _iter_uses(obj) -> list:
    """Todos los valores bajo una clave EXACTA `uses` en cualquier mapping (recursivo): pasos y
    reusable-workflows a nivel de job. No recurre dentro del valor de `uses`."""
    found: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "uses":
                found.append(v)
            else:
                found.extend(_iter_uses(v))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_iter_uses(item))
    return found


def _uses_comments(text: str) -> dict[str, str | None]:
    """action@sha → comentario de versión, desde el TEXTO crudo (tolerante a espacios alrededor del `:`)."""
    out: dict[str, str | None] = {}
    for m in _USES_COMMENT.finditer(text):
        out[f"{m.group(1)}@{m.group(2)}"] = m.group(3)
    return out


def check(root: Path = ROOT) -> list[str]:
    reg = load_registry()  # el registro autoritativo vive SIEMPRE en el repo real
    probs: list[str] = []
    used: set[str] = set()
    for f in _workflows(root):
        text = f.read_text()
        try:
            doc = yaml.load(text, Loader=_StrictLoader)  # B56: fail-closed en YAML inválido / clave dup
        except yaml.YAMLError as exc:
            probs.append(f"{f.name}: YAML inválido o con clave duplicada ({exc})")
            continue
        comments = _uses_comments(text)
        for value in _iter_uses(doc):
            where = f.name
            if not isinstance(value, str):
                probs.append(f"{where}: `uses` con valor no-string ({value!r})")
                continue
            v = value.strip()
            if v.startswith("./") or v.startswith("."):
                continue  # acción local del repo
            if "${{" in v:
                probs.append(f"{where}: `uses` con expresión dinámica no permitida ({v!r})")
                continue
            m = _USES_VALUE.fullmatch(v)
            if not m:
                probs.append(f"{where}: `uses` no parseable como acción@ref ({v!r})")
                continue
            action, ref = m.group(1), m.group(2)
            used.add(action)
            if not _SHA_RE.fullmatch(ref):
                probs.append(f"{where} {action} no fijado a SHA de 40 hex (ref {ref!r} — tag flotante)")
                continue
            if ref in _NODE20_SHAS:
                probs.append(f"{where} {action} usa un SHA Node 20 DEPRECADO ({ref})")
                continue
            entry = reg.get(action)
            if entry is None:
                probs.append(f"{where} {action} acción NO autorizada en security/github_actions.json")
                continue
            if ref != entry["sha"]:
                probs.append(f"{where} {action} SHA {ref} != autorizado {entry['sha']}")
                continue
            if entry["runtime"] != "node24":
                probs.append(f"{where} {action} runtime {entry['runtime']!r} != node24")
            comment = comments.get(f"{action}@{ref}")
            if comment != entry["version"]:
                probs.append(f"{where} {action} comentario {comment!r} != versión esperada {entry['version']!r}")
    # B53: BIYECCIÓN — ninguna entrada del registro puede quedar huérfana (registrada pero sin usar).
    for orphan in sorted(set(reg) - used):
        probs.append(f"registro: {orphan} está autorizado pero NO se usa en ningún workflow (huérfano)")
    return probs


def _gh(*args: str) -> str | None:
    try:
        r = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=30)
    except OSError, subprocess.TimeoutExpired:
        return None
    return r.stdout if r.returncode == 0 else None


def _runs_using(action_yml_text: str) -> str | None:
    """B57: `runs.using` PARSEADO estructuralmente (no una búsqueda textual: un `using: node24` en un
    comentario o en la descripción no cuenta). Devuelve el string o None si no es derivable."""
    try:
        doc = yaml.load(action_yml_text, Loader=_StrictLoader)
    except yaml.YAMLError:
        return None
    runs = doc.get("runs") if isinstance(doc, dict) else None
    using = runs.get("using") if isinstance(runs, dict) else None
    return using if isinstance(using, str) else None


def verify_remote(registry: Path = REGISTRY) -> list[str]:
    """B53/B57: contra la GitHub API — cada SHA existe y el endpoint de commit lo confirma, la versión
    documentada apunta a ese SHA (resolviendo tags anotados), y su `action.yml`/`action.yaml` declara
    `runs.using == "node24"` PARSEADO como YAML. Fail-closed (un fallo de red o de verificación es un
    problema)."""
    import base64

    reg = load_registry(registry)
    probs: list[str] = []
    for action, e in reg.items():
        sha, ver = e["sha"], e["version"]
        commit = _gh(f"repos/{action}/commits/{sha}", "--jq", ".sha")
        if commit is None:
            probs.append(f"{action}@{sha}: el commit no existe o es inaccesible (remoto)")
            continue
        if commit.strip().strip('"') != sha:  # B57: el endpoint de commit debe devolver el MISMO sha
            probs.append(f"{action}@{sha}: el endpoint de commit devolvió {commit.strip()!r} != {sha}")
            continue
        ref = _gh(f"repos/{action}/git/ref/tags/{ver}", "--jq", ".object.sha")
        resolved = (ref or "").strip().strip('"')
        # el tag puede ser anotado (apunta a un objeto tag); resuélvelo al commit (una vez, acotado)
        if resolved and resolved != sha:
            commit2 = _gh(f"repos/{action}/git/tags/{resolved}", "--jq", ".object.sha")
            if commit2:
                resolved = commit2.strip().strip('"')
        if resolved != sha:
            probs.append(f"{action}: la versión {ver} apunta a {resolved!r} != SHA registrado {sha}")
        content = _gh(f"repos/{action}/contents/action.yml?ref={sha}", "--jq", ".content")
        if content is None:
            content = _gh(f"repos/{action}/contents/action.yaml?ref={sha}", "--jq", ".content")
        if content is None:
            probs.append(f"{action}@{sha}: sin action.yml/action.yaml legible (remoto)")
            continue
        try:
            # base64 estricto tras quitar el formateo de líneas que inserta la API de GitHub
            decoded = base64.b64decode("".join(content.strip().strip('"').split()), validate=True)
            text = decoded.decode("utf-8")
        except ValueError, UnicodeDecodeError:
            probs.append(f"{action}@{sha}: action.yml no decodificable (base64 estricto)")
            continue
        using = _runs_using(text)
        if using != "node24":
            probs.append(f"{action}@{sha}: runs.using={using!r} != 'node24' (estructural)")
    return probs


def main() -> int:
    online = "--online" in sys.argv[1:]
    probs = check()
    if online:
        probs = probs + verify_remote()
    if probs:
        print("✗ CHECK ACTION-PINS (registro positivo Node 24" + (" + remoto" if online else "") + "):")
        for p in probs:
            print(f"  - {p}")
        return 1
    reg = load_registry()
    n = sum(len(_iter_uses(yaml.load(f.read_text(), Loader=_StrictLoader))) for f in _workflows(ROOT))
    tail = " + SHA/versión/runtime remoto verificados" if online else ""
    print(f"✓ {n} usos de Actions, biyección con {len(reg)} acciones registradas (node24, SHA+versión){tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
