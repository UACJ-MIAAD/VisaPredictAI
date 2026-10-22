"""Refresh numeric diagram labels after sealing a monthly release, before its gates."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def render_er(svg: str, facts: dict) -> str:
    counts = {key: facts[key] for key in ("n_obs", "n_obs_F")}
    if any(type(value) is not int or value <= 0 for value in counts.values()):
        raise ValueError("ER: los conteos deben ser enteros positivos")
    if counts["n_obs_F"] > counts["n_obs"]:
        raise ValueError("ER: observaciones F mayores que el panel")
    for pattern, replacement in (
        (r"raw_value · [\d,]+ filas", f"raw_value · {counts['n_obs']:,} filas"),
        (r"[\d,]+ filas · listo para modelar", f"{counts['n_obs_F']:,} filas · listo para modelar"),
    ):
        svg, matches = re.subn(pattern, replacement, svg)
        if matches != 1:
            raise ValueError(f"ER: binding numérico ausente o ambiguo: {matches}")
    return svg


def main() -> int:
    facts = json.loads((ROOT / "reports/governance/key_facts.json").read_text())
    path = ROOT / "docs/schema_er.svg"
    original = path.read_text()
    updated = render_er(original, facts)
    if updated != original:
        path.write_text(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
