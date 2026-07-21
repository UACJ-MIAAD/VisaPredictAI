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
import re
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
            with art.open() as _fh:  # cerrar el fichero (evita ResourceWarning bajo -W error)
                header = _fh.readline().strip().split(",")
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
            # R0-03 (reauditoría ciega): un contrato que solo exige dicts top-level es
            # nominal — la deriva de esquema que motivó A-04 (gate_scope/holdout_winner
            # ausentes) pasaba limpia. required_paths exige rutas anidadas con tipo.
            for dotted, tname in c.get("required_paths", {}).items():
                node = data
                for part in dotted.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                    if node is None:
                        problems.append(f"{c['artifact']}: ruta requerida ausente '{dotted}'")
                        break
                else:
                    if not isinstance(node, TYPES[tname]):
                        problems.append(f"{c['artifact']}: '{dotted}' debería ser {tname}, es {type(node).__name__}")
            vk = c.get("vintage_key")
            if vk and isinstance(data.get(vk), str):
                vintages[c["artifact"]] = str(data[vk])[:7]
    pv = _panel_vintage(root)
    if pv:
        vintages["data/processed/visa_panel_long.csv (real)"] = pv
    if len(set(vintages.values())) > 1:
        problems.append(f"CORTE CON AÑADAS MEZCLADAS: {vintages}")
    # Auditoría 11-jul (+rondas 2-3, 12-jul): la identidad del manifiesto PUBLICADO debe
    # RESOLVER a un commit. Se exige: (a) forma 12-hex (ni 'n/d' ni sufijo -dirty), y
    # (b) que el objeto exista de verdad (`git cat-file -e`). FAIL-CLOSED total (ronda 3:
    # el bypass shallow dejaba pasar un sha fantasma justo en CI/cron): un clone shallow
    # o sin git ES violación — los checkouts que corren este gate usan fetch-depth: 0.
    man = root / "reports" / "release" / "release_manifest.json"
    if man.exists():
        try:
            manifest = json.loads(man.read_text())
            sha = str(manifest.get("git_sha", ""))
            # Reauditoría 12-jul (hueco cazado en vivo): cambiar un artefacto listado SIN
            # regenerar el manifiesto solo lo detectaba el verifyEntry del web (stale en
            # el deploy). El manifiesto debe describir los bytes del árbol AHORA.
            import hashlib

            for a in manifest.get("artifacts", []):
                ap = root / a["path"]
                # Reauditoría 2 (12-jul): `if ap.exists() and…` era fail-open — un artefacto
                # required listado pero BORRADO producía cero problemas.
                if not ap.exists():
                    problems.append(
                        f"release_manifest.json: '{a['path']}' listado en el manifiesto pero AUSENTE del árbol"
                    )
                elif hashlib.sha256(ap.read_bytes()).hexdigest() != a["sha256"]:
                    problems.append(
                        f"release_manifest.json: '{a['path']}' cambió tras sellar el manifiesto — regenerarlo"
                    )
            if sha.endswith("-dirty"):
                problems.append(f"release_manifest.json: git_sha '{sha}' es -dirty — regenerar con árbol limpio")
            elif not re.fullmatch(r"[0-9a-f]{12}", sha):
                problems.append(f"release_manifest.json: git_sha '{sha}' no es un sha 12-hex resoluble")
            else:
                problems.extend(_sha_unresolvable(root, sha))
        except json.JSONDecodeError:
            problems.append("release_manifest.json: JSON ilegible")
    return problems


def _sha_unresolvable(root: Path, sha: str) -> list[str]:
    """[] SOLO si el sha resuelve en un historial completo. Fail-closed en todo lo demás
    (ronda 3 de auditoría: el bypass shallow anulaba la garantía exactamente donde más
    importa — CI y cron corren en checkouts de Actions)."""
    import subprocess

    try:
        shallow = subprocess.check_output(
            ["git", "rev-parse", "--is-shallow-repository"], text=True, stderr=subprocess.DEVNULL, cwd=root
        ).strip()
    except subprocess.CalledProcessError, FileNotFoundError:
        return [f"release_manifest.json: git_sha '{sha}' NO VERIFICABLE (sin git/repo) — fail-closed"]
    if shallow == "true":
        return [
            f"release_manifest.json: clone SHALLOW — git_sha '{sha}' no verificable; "
            "el checkout que corre este gate debe usar fetch-depth: 0 (fail-closed)"
        ]
    try:
        subprocess.check_call(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=root,
        )
        return []
    except subprocess.CalledProcessError:
        return [f"release_manifest.json: git_sha '{sha}' NO existe en el historial (commit fantasma)"]


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
