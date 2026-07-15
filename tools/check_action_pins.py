#!/usr/bin/env python
"""Gate de pins de GitHub Actions (P0R.5, R8R6R/B49). Un blocklist de tres SHA Node 20 era INSUFICIENTE:
aceptaba un SHA inventado de 40 hex, un comentario de versión falso y no escaneaba `.yaml`. Este gate usa
un REGISTRO POSITIVO (`security/github_actions.json`): toda acción de terceros DEBE estar declarada con su
SHA EXACTO, su comentario de versión y runtime `node24`. Escanea `*.yml` Y `*.yaml`; solo salta acciones
locales (`./…`). Rechaza acciones y SHA desconocidos, tags flotantes, comentario incoherente y no-node24.

    python -m tools.check_action_pins      # exit 1 ante cualquier pin no autorizado
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "security" / "github_actions.json"
_USES = re.compile(r"uses:\s*([^\s@#]+)@([^\s#]+)(?:\s*#\s*(\S+))?")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Tripwire de defensa en profundidad: SHA Node 20 deprecados (el registro positivo YA los rechazaría, pero
# este check emite un mensaje específico "Node 20").
_NODE20_SHAS = {
    "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "fa0a91b85d4f404e444e00e005971372dc801d16",
    "7474bc4690e29a8392af63c5b98e7449536d5c3a",
}


def load_registry(registry: Path = REGISTRY) -> dict[str, dict[str, str]]:
    doc = json.loads(registry.read_text())
    if doc.get("schema_version") != 1 or not isinstance(doc.get("actions"), dict):
        raise SystemExit(f"check_action_pins: {registry} inválido (schema_version/actions)")
    for name, e in doc["actions"].items():
        if (
            not isinstance(e, dict)
            or set(e) != {"sha", "version", "runtime"}
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
    for f in _workflows(root):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            m = _USES.search(line)
            if not m:
                continue
            action, ref, comment = m.group(1), m.group(2), m.group(3)
            if action.startswith("./") or action.startswith("."):
                continue  # acción local del repo
            where = f"{f.name}:{i} {action}"
            if not _SHA_RE.fullmatch(ref):
                probs.append(f"{where} no está fijado a un SHA de 40 hex (ref {ref!r} — tag flotante)")
                continue
            if ref in _NODE20_SHAS:
                probs.append(f"{where} usa un SHA Node 20 DEPRECADO ({ref}) — actualiza a Node 24")
                continue
            entry = reg.get(action)
            if entry is None:
                probs.append(f"{where} acción NO autorizada en security/github_actions.json")
                continue
            if ref != entry["sha"]:
                probs.append(f"{where} SHA {ref} != autorizado {entry['sha']}")
                continue
            if entry["runtime"] != "node24":
                probs.append(f"{where} runtime {entry['runtime']!r} != node24")
            if comment != entry["version"]:
                probs.append(f"{where} comentario {comment!r} != versión esperada {entry['version']!r}")
    return probs


def main() -> int:
    probs = check()
    if probs:
        print("✗ CHECK ACTION-PINS (registro positivo Node 24):")
        for p in probs:
            print(f"  - {p}")
        return 1
    reg = load_registry()
    n = sum(1 for f in _workflows(ROOT) for line in f.read_text().splitlines() if _USES.search(line))
    print(f"✓ {n} usos de Actions, todos autorizados en el registro ({len(reg)} acciones, node24, SHA+versión)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
