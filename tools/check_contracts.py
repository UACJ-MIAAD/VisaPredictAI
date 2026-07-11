"""Contratos cross-repo (B3, plan auditoría 2026-07-11).

Valida cada artefacto publicado contra su contrato versionado en
``vp_data/contracts/*.json`` (columnas requeridas para CSV; llaves y tipos top-level
para JSON) y exige que TODO el corte comparta la misma añada: los artefactos que
declaran ``vintage_key`` deben coincidir entre sí y con la añada real del panel
(``max(bulletin_date)``) — un corte con añadas mezcladas FALLA.

Cero dependencias (ni pandas): corre igual en el CI dev, en el cron (antes del
manifiesto de release) y en un clone pelón. El lado TypeScript vendoriza estos mismos
contratos (``lib/contracts/`` del repo web) y los verifica al construir; el manifiesto
de release los lista como artefactos required, así el loader detecta la deriva
vendored-vs-publicado por hash.

Corre desde la raíz:  python tools/check_contracts.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = ROOT / "vp_data" / "contracts"

TYPES: dict[str, type | tuple] = {
    "str": str,
    "int": int,
    "float": float,
    "number": (int, float),
    "dict": dict,
    "list": list,
    "bool": bool,
}


def _panel_vintage(root: Path) -> str | None:
    """``max(bulletin_date)[:7]`` leyendo el CSV a mano (sin pandas)."""
    panel = root / "data" / "processed" / "visa_panel_long.csv"
    if not panel.exists():
        return None
    with panel.open() as fh:
        header = fh.readline().strip().split(",")
        if "bulletin_date" not in header:
            return None
        idx = header.index("bulletin_date")
        best = ""
        for line in fh:
            cols = line.rstrip("\n").split(",")
            if len(cols) > idx and cols[idx] > best:
                best = cols[idx]
    return best[:7] or None


def check(root: Path = ROOT, contracts_dir: Path = CONTRACTS_DIR) -> list[str]:
    problems: list[str] = []
    vintages: dict[str, str] = {}
    contracts = sorted(contracts_dir.glob("*.json"))
    if not contracts:
        return [f"sin contratos en {contracts_dir}"]
    for cpath in contracts:
        c = json.loads(cpath.read_text())
        art = root / c["artifact"]
        if not art.exists():
            problems.append(f"{c['artifact']}: artefacto ausente")
            continue
        if c["kind"] == "csv":
            header = art.open().readline().strip().split(",")
            missing = [col for col in c["required_columns"] if col not in header]
            if missing:
                problems.append(f"{c['artifact']}: columnas requeridas ausentes {missing}")
        else:
            try:
                data = json.loads(art.read_text())
            except json.JSONDecodeError as e:
                problems.append(f"{c['artifact']}: JSON ilegible ({e})")
                continue
            for key, tname in c.get("required_keys", {}).items():
                if key not in data:
                    problems.append(f"{c['artifact']}: llave requerida ausente '{key}'")
                elif not isinstance(data[key], TYPES[tname]):
                    problems.append(f"{c['artifact']}: '{key}' debería ser {tname}, es {type(data[key]).__name__}")
            vk = c.get("vintage_key")
            if vk and isinstance(data.get(vk), str):
                vintages[c["artifact"]] = str(data[vk])[:7]
    pv = _panel_vintage(root)
    if pv:
        vintages["data/processed/visa_panel_long.csv (real)"] = pv
    if len(set(vintages.values())) > 1:
        problems.append(f"CORTE CON AÑADAS MEZCLADAS: {vintages}")
    return problems


def main() -> int:
    problems = check()
    if problems:
        print(f"✗ CONTRATOS ROTOS — {len(problems)} problema(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    n = len(list(CONTRACTS_DIR.glob("*.json")))
    print(f"✓ Contratos OK — {n} artefactos validados, añada única {_panel_vintage(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
