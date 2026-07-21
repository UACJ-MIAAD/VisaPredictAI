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

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # B286-C: raíz en sys.path para importar `tools.governance_snapshot` en forma script
REGISTRY = ROOT / "security" / "github_actions.json"
WARNINGS = ROOT / "security" / "upstream_warnings.json"
_WARN_TOP = {"schema_version", "note", "warnings"}
_WARN_KEYS = {"id", "action", "sha", "detail", "review"}
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
# B287: gramática CERRADA del registro. Nombre de acción = owner/repo[/path…], componentes no vacíos sin whitespace/@/
# `..`/slash-inicial-final; note string no vacío tras strip y acotado; máximos.
_NAME_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# RC-4: la versión debe ser una RELEASE EXACTA `vN.N.N` — NUNCA un tag mayor/menor móvil (`v5`/`v5.1`). Un tag mayor puede
# reapuntar upstream (v5 pasó de v5.0.1 a v5.1.0) y la verificación online lo detectaría como drift; anclando al tag exacto
# `vN.N.N` esa clase de fallo es imposible. Una acción que legítimamente necesite un tag menos preciso debe modelarse por
# entrada (no con tolerancia global aquí).
_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_MAX_ACTIONS = 64
_MAX_NOTE_LEN = 2000
_MAX_NAME_LEN = 200


def _valid_action_name(name: object) -> bool:
    """B287: `owner/repo` u `owner/repo/path…`; componentes no vacíos, sin whitespace/@/`.`/`..`/slash-inicial-final."""
    if not isinstance(name, str) or not name or len(name) > _MAX_NAME_LEN or name != name.strip():
        return False
    parts = name.split("/")
    return len(parts) >= 2 and all(p not in ("", ".", "..") and _NAME_COMPONENT_RE.fullmatch(p) for p in parts)


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


def load_registry(registry: Path = REGISTRY, *, text: str | None = None) -> dict[str, dict[str, str]]:
    # B286-C: con `text` (bytes ya leídos por una `GovernanceSnapshot` sellada) se parsea esa fuente y NO se abre la ruta;
    # por defecto lee `registry.read_text()` (compat con tests/tooling).
    raw = registry.read_text() if text is None else text
    doc = json.loads(raw, object_pairs_hook=_no_dup)  # B53: rechaza claves duplicadas
    if set(doc) != _REG_TOP:
        raise SystemExit(f"check_action_pins: claves superiores {sorted(doc)} != {sorted(_REG_TOP)}")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:  # B53: True no es 1
        raise SystemExit("check_action_pins: schema_version no es int == 1")
    # B287: note = string no vacío tras strip y acotado (un `note` bool/null/lista se colaba)
    if not isinstance(doc["note"], str) or not doc["note"].strip() or len(doc["note"]) > _MAX_NOTE_LEN:
        raise SystemExit("check_action_pins: note no es string no vacío y acotado")
    if not isinstance(doc["actions"], dict) or not doc["actions"]:
        raise SystemExit("check_action_pins: `actions` no es objeto no vacío")
    if len(doc["actions"]) > _MAX_ACTIONS:  # B287: cota de entradas
        raise SystemExit(f"check_action_pins: > {_MAX_ACTIONS} acciones")
    for name, e in doc["actions"].items():
        if not _valid_action_name(
            name
        ):  # B287: gramática del nombre (owner/repo[/path]); antes se aceptaba "" o whitespace
            raise SystemExit(f"check_action_pins: nombre de acción inválido {name!r}")
        if (
            not isinstance(e, dict)
            or set(e) != _ENTRY_KEYS
            or not (isinstance(e["sha"], str) and _SHA_RE.fullmatch(e["sha"]))
            or not (
                isinstance(e["version"], str) and _VERSION_RE.fullmatch(e["version"])
            )  # B287: versión vN[.N[.N]] (un "   " se colaba)
            or e["runtime"] != "node24"
        ):
            raise SystemExit(f"check_action_pins: entrada de registro inválida para {name!r}")
    return doc["actions"]


def _workflows(root: Path) -> list[Path]:
    wf = root / ".github" / "workflows"
    return sorted([*wf.glob("*.yml"), *wf.glob("*.yaml")])


def _iter_uses_nodes(node) -> list[tuple[str | None, int]]:
    """B63: (valor_str_o_None, línea 0-index del NODO valor) por cada clave EXACTA `uses` en el árbol de
    nodos YAML — así el comentario de versión se lee de la LÍNEA EXACTA de esa ocurrencia (no de un dict
    global action@sha que enmascara la primera aparición). Recorre pasos y reusable-workflows."""
    found: list[tuple[str | None, int]] = []
    if isinstance(node, yaml.MappingNode):
        for k_node, v_node in node.value:
            if isinstance(k_node, yaml.ScalarNode) and k_node.value == "uses":
                val = v_node.value if isinstance(v_node, yaml.ScalarNode) else None
                found.append((val, v_node.start_mark.line))
            else:
                found.extend(_iter_uses_nodes(v_node))
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            found.extend(_iter_uses_nodes(item))
    return found


def _comment_on_line(line: str) -> str | None:
    """Comentario de versión de UNA línea `uses: …@<sha> # <ver>` (tolerante a espacios)."""
    m = _USES_COMMENT.search(line)
    return m.group(3) if m else None


def validate_upstream_warnings(
    reg: dict[str, dict[str, str]],
    path: Path = WARNINGS,
    today: datetime.date | None = None,
    *,
    text: str | None = None,
) -> list[str]:
    """B84: registro machine-readable de warnings upstream ACEPTADOS (deuda con fecha de revisión), no
    comentarios de workflow. Fail-closed: esquema exacto, id único, la acción debe existir en el registro
    positivo con el MISMO SHA (un bump de la acción invalida la aceptación y fuerza re-evaluar) y
    `review >= hoy` — la aceptación EXPIRA y pone el gate en rojo hasta re-evaluar o retirar la entrada.
    Archivo ausente = cero warnings aceptados (válido). B286-C: con `text` (bytes gobernados por snapshot) NO abre la
    ruta; `text=''` significa AUSENTE (cero warnings)."""
    if text is None and not path.exists():
        return []
    if text == "":
        return []
    probs: list[str] = []
    try:
        doc = json.loads(path.read_text() if text is None else text, object_pairs_hook=_no_dup)
    except (json.JSONDecodeError, SystemExit) as exc:
        return [f"upstream_warnings: JSON inválido o con clave duplicada ({exc})"]
    if not isinstance(doc, dict) or set(doc) != _WARN_TOP:
        return [f"upstream_warnings: claves superiores != {sorted(_WARN_TOP)}"]
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        probs.append("upstream_warnings: schema_version no es int == 1")
    if not isinstance(doc["note"], str) or not doc["note"]:
        probs.append("upstream_warnings: note no-string o vacía")
    if not isinstance(doc["warnings"], list):
        return probs + ["upstream_warnings: `warnings` no es lista"]
    today = today or datetime.date.today()
    seen: set[str] = set()
    for w in doc["warnings"]:
        if not isinstance(w, dict) or set(w) != _WARN_KEYS:
            probs.append(f"upstream_warnings: entrada con claves != {sorted(_WARN_KEYS)}: {w!r}")
            continue
        if not all(isinstance(w[k], str) and w[k] for k in _WARN_KEYS):
            probs.append(f"upstream_warnings: {w.get('id')!r} con campos no-string o vacíos")
            continue
        if w["id"] in seen:
            probs.append(f"upstream_warnings: id duplicado {w['id']!r}")
            continue
        seen.add(w["id"])
        entry = reg.get(w["action"])
        if entry is None:
            probs.append(f"upstream_warnings: {w['id']}: acción {w['action']!r} NO está en el registro positivo")
        elif entry["sha"] != w["sha"]:
            probs.append(
                f"upstream_warnings: {w['id']}: SHA {w['sha']} != registro {entry['sha']} "
                "(la acción cambió — re-evaluar el warning aceptado)"
            )
        try:
            review = datetime.date.fromisoformat(w["review"])
        except ValueError:
            probs.append(f"upstream_warnings: {w['id']}: review {w['review']!r} no es fecha ISO YYYY-MM-DD")
            continue
        if review < today:
            probs.append(f"upstream_warnings: {w['id']}: aceptación VENCIDA ({w['review']}) — re-evaluar o retirar")
    return probs


def check(root: Path = ROOT, *, registry_text: str | None = None, warnings_text: str | None = None) -> list[str]:
    # B286-C: con `registry_text`/`warnings_text` (bytes gobernados por UNA snapshot sellada) la decisión no abre esas
    # rutas; por defecto lee los ficheros reales (compat con los tests que llaman `check()`/`check(tmp_path)`).
    reg = load_registry(text=registry_text)  # el registro autoritativo vive SIEMPRE en el repo real
    probs: list[str] = []
    used: set[str] = set()
    for f in _workflows(root):
        text = f.read_text()
        try:
            yaml.load(text, Loader=_StrictLoader)  # B56: fail-closed en YAML inválido / clave dup
            node = yaml.compose(text, Loader=yaml.SafeLoader)  # B63: árbol de nodos con nº de línea
        except yaml.YAMLError as exc:
            probs.append(f"{f.name}: YAML inválido o con clave duplicada ({exc})")
            continue
        lines = text.splitlines()
        for value, lineno in _iter_uses_nodes(node) if node is not None else []:
            where = f"{f.name}:{lineno + 1}"
            if value is None:
                probs.append(f"{where}: `uses` con valor no-string")
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
            # B63: comentario de version leído de la LÍNEA de ESTA ocurrencia (no un dict global).
            comment = _comment_on_line(lines[lineno]) if lineno < len(lines) else None
            if comment != entry["version"]:
                probs.append(f"{where} {action} comentario {comment!r} != versión esperada {entry['version']!r}")
    # B53: BIYECCIÓN — ninguna entrada del registro puede quedar huérfana (registrada pero sin usar).
    for orphan in sorted(set(reg) - used):
        probs.append(f"registro: {orphan} está autorizado pero NO se usa en ningún workflow (huérfano)")
    # B84: los warnings upstream aceptados viven en security/upstream_warnings.json con fecha de revisión.
    probs.extend(validate_upstream_warnings(reg, text=warnings_text))
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
    # B286-C: los inputs de gobernanza (github_actions.json + upstream_warnings.json) se leen por UNA observación
    # gobernada sellada (O_NOFOLLOW + modo/uid/nlink), `reverify()` antes de decidir. El corpus de workflows sigue su
    # escaneo (es el sujeto validado, no un input de política). La verificación --online (red) queda fuera del snapshot.
    from tools.governance_snapshot import GovernanceSnapshot, GovernanceSnapshotError, TrackedQuery

    _reg_rel, _warn_rel = "security/github_actions.json", "security/upstream_warnings.json"
    try:
        with GovernanceSnapshot(str(ROOT)) as snap:
            registry_text = snap.read(_reg_rel, category="contract").data.decode("utf-8")
            warnings_text = (
                snap.read(_warn_rel, category="contract").data.decode("utf-8")
                if snap.tracked(TrackedQuery("exact", _warn_rel))
                else ""  # ausente = cero warnings aceptados
            )
            probs = check(registry_text=registry_text, warnings_text=warnings_text)
            snap.reverify()
    except (GovernanceSnapshotError, OSError) as exc:
        print(f"✗ check-action-pins fail-closed: {exc}")
        return 1
    if online:
        probs = probs + verify_remote()
    if probs:
        print("✗ CHECK ACTION-PINS (registro positivo Node 24" + (" + remoto" if online else "") + "):")
        for p in probs:
            print(f"  - {p}")
        return 1
    reg = load_registry(text=registry_text)  # reusa los bytes gobernados (sin segunda apertura)
    n = sum(len(_iter_uses_nodes(yaml.compose(f.read_text(), Loader=yaml.SafeLoader))) for f in _workflows(ROOT))
    tail = " + SHA/versión/runtime remoto verificados" if online else ""
    print(f"✓ {n} usos de Actions, biyección con {len(reg)} acciones registradas (node24, SHA+versión){tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
