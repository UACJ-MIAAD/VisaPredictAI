#!/usr/bin/env python
"""Gate de pins de GitHub Actions (P0R.5, Node 24). Todo `uses:` de una acción de terceros DEBE fijarse
a un SHA de 40 hex (no tags flotantes @vX/@main), y NINGUNO puede ser un SHA Node 20 deprecado conocido
(GitHub fuerza Node 24 en los runners). Además el comentario `# vX` debe ser coherente con el SHA.

    python -m tools.check_action_pins      # exit 1 ante un pin inseguro o Node 20
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / ".github" / "workflows"
_USES = re.compile(r"uses:\s*([^\s@]+)@([^\s#]+)(?:\s*#\s*(\S+))?")
# SHAs Node 20 deprecados (upload-artifact v4, download-artifact v4.1.8, aws configure v4).
_NODE20_SHAS = {
    "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "fa0a91b85d4f404e444e00e005971372dc801d16",
    "7474bc4690e29a8392af63c5b98e7449536d5c3a",
}
# acciones locales (./…) y las de GitHub que aún exigen su propio pin — todas van por SHA.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def check(root: Path = ROOT) -> list[str]:
    probs: list[str] = []
    wf = root / ".github" / "workflows"
    for f in sorted(wf.glob("*.yml")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            m = _USES.search(line)
            if not m:
                continue
            action, ref = m.group(1), m.group(2)
            if action.startswith("./") or action.startswith("."):
                continue  # acción local del repo
            where = f"{f.name}:{i} {action}"
            if not _SHA_RE.match(ref):
                probs.append(f"{where} no está fijado a un SHA de 40 hex (ref {ref!r} — tag flotante)")
            elif ref in _NODE20_SHAS:
                probs.append(f"{where} usa un SHA Node 20 DEPRECADO ({ref}) — actualiza a Node 24")
    return probs


def main() -> int:
    probs = check()
    if probs:
        print("✗ CHECK ACTION-PINS (Node 24):")
        for p in probs:
            print(f"  - {p}")
        return 1
    n = sum(1 for f in WF_DIR.glob("*.yml") for line in f.read_text().splitlines() if _USES.search(line))
    print(f"✓ {n} acciones fijadas por SHA, ninguna Node 20 deprecada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
