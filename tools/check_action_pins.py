#!/usr/bin/env python
"""Gate de pins de GitHub Actions (P0R.5, R8R6R/B49 + R8R6R2/B53). Registro POSITIVO
`security/github_actions.json`: toda acción de terceros DEBE estar declarada con su SHA EXACTO de 40 hex,
su comentario de versión y runtime `node24`. El gate:
- carga el registro con RECHAZO DE CLAVES DUPLICADAS, `type(schema_version) is int`, y claves superiores
  EXACTAS (`schema_version`/`note`/`actions`);
- escanea `*.yml` Y `*.yaml`, salta solo acciones locales (`./…`), y FALLA ante cualquier línea `uses:` que
  no parsee como `acción@<sha40>` o local (nada de tags flotantes ni SHA cortos silenciosos);
- exige BIYECCIÓN acción-usada ⇔ acción-registrada (sin huérfanas ni no autorizadas);
- verifica SHA exacto + comentario de versión + runtime node24 por entrada.
Con `--online` verifica además, vía GitHub API, que cada SHA existe, corresponde a la versión documentada y
su `action.yml` declara `runs.using: node24` (job requerido de CI, fail-closed).

    python -m tools.check_action_pins            # gate offline
    python -m tools.check_action_pins --online   # + verificación remota de SHA/versión/runtime
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "security" / "github_actions.json"
# acción@ref con comentario opcional; y detector de CUALQUIER línea `uses:` (para rechazar las no parseables)
_USES = re.compile(r"uses:\s*([^\s@#]+)@([^\s#]+)(?:\s*#\s*(\S+))?")
_USES_LINE = re.compile(r"(?:^|\s|-)uses:\s*(\S.*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Tripwire de defensa en profundidad: SHA Node 20 deprecados (el registro positivo YA los rechazaría).
_NODE20_SHAS = {
    "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "fa0a91b85d4f404e444e00e005971372dc801d16",
    "7474bc4690e29a8392af63c5b98e7449536d5c3a",
}
_REG_TOP = {"schema_version", "note", "actions"}
_ENTRY_KEYS = {"sha", "version", "runtime"}


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


def check(root: Path = ROOT) -> list[str]:
    reg = load_registry()  # el registro autoritativo vive SIEMPRE en el repo real
    probs: list[str] = []
    used: set[str] = set()
    for f in _workflows(root):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            lm = _USES_LINE.search(line)
            if not lm:
                continue
            where = f"{f.name}:{i}"
            value = lm.group(1).strip()
            if value.startswith("./") or value.startswith("."):
                continue  # acción local del repo
            m = _USES.search(line)
            if not m:
                probs.append(f"{where} línea `uses:` no parseable como acción@<sha40> ({value!r})")
                continue
            action, ref, comment = m.group(1), m.group(2), m.group(3)
            if action.startswith("./") or action.startswith("."):
                continue
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


def verify_remote(registry: Path = REGISTRY) -> list[str]:
    """B53: contra la GitHub API — cada SHA existe, la versión documentada apunta a ese SHA y su action.yml
    declara `runs.using: node24`. Fail-closed (un fallo de red o de verificación es un problema)."""
    import base64

    reg = load_registry(registry)
    probs: list[str] = []
    for action, e in reg.items():
        sha, ver = e["sha"], e["version"]
        if _gh(f"repos/{action}/commits/{sha}", "--jq", ".sha") is None:
            probs.append(f"{action}@{sha}: el commit no existe o es inaccesible (remoto)")
            continue
        ref = _gh(f"repos/{action}/git/ref/tags/{ver}", "--jq", ".object.sha")
        resolved = (ref or "").strip()
        # el tag puede ser anotado (apunta a un objeto tag); resuélvelo al commit
        if resolved and resolved != sha:
            commit = _gh(f"repos/{action}/git/tags/{resolved}", "--jq", ".object.sha")
            if commit:
                resolved = commit.strip()
        if resolved != sha:
            probs.append(f"{action}: la versión {ver} apunta a {resolved!r} != SHA registrado {sha}")
        content = _gh(f"repos/{action}/contents/action.yml?ref={sha}", "--jq", ".content")
        if content is None:
            probs.append(f"{action}@{sha}: sin action.yml legible (remoto)")
            continue
        try:
            text = base64.b64decode(content).decode("utf-8", "replace")
        except ValueError, UnicodeDecodeError:
            probs.append(f"{action}@{sha}: action.yml no decodificable")
            continue
        if not re.search(r"using:\s*['\"]?node24['\"]?", text):
            probs.append(f"{action}@{sha}: action.yml no declara runs.using: node24")
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
    n = sum(1 for f in _workflows(ROOT) for line in f.read_text().splitlines() if _USES.search(line))
    tail = " + SHA/versión/runtime remoto verificados" if online else ""
    print(f"✓ {n} usos de Actions, biyección con {len(reg)} acciones registradas (node24, SHA+versión){tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
