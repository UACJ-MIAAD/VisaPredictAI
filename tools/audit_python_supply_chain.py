#!/usr/bin/env python
"""Ejecutor ÚNICO de auditoría de supply chain Python (P0R.3, ronda 10).

Audita cada lock con ``pip-audit`` en JSON (SIN ocultar advisories primero) y reconcilia el
resultado BRUTO contra ``security/python_advisories.json`` con BIYECCIÓN EXACTA por perfil:

  * todo advisory OBSERVADO debe estar PERMITIDO para ese perfil (paquete+versión exactos);
  * toda excepción PERMITIDA para un perfil debe estar OBSERVADA (huérfana ⇒ bloquea);
  * una excepción EXPIRADA bloquea; runtime/dev deben observar CERO.

Reemplaza como autoridad la allowlist textual ``--ignore-vuln`` del workflow. No parsea YAML.

Uso:  python tools/audit_python_supply_chain.py            # audita el repo
      python tools/audit_python_supply_chain.py --receipt reports/governance/supply_chain_receipt.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADVISORIES = ROOT / "security" / "python_advisories.json"
PIP_AUDIT_VERSION = "2.10.1"
PROFILES = ("runtime", "dev", "model", "deep")
# (lock relativo, perfil). Los espejos Linux comparten perfil con su lock macOS.
LOCKS: list[tuple[str, str]] = [
    ("locks/runtime.txt", "runtime"),
    ("locks/runtime-linux-x86_64.txt", "runtime"),
    ("locks/dev.txt", "dev"),
    ("locks/dev-linux-x86_64.txt", "dev"),
    ("locks/model-cpu.txt", "model"),
    ("locks/model-cpu-linux-x86_64.txt", "model"),
]


def _norm_pkg(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def load_advisories(path: Path) -> list[dict]:
    """Carga el JSON rechazando claves duplicadas. Lanza ValueError si malformado."""

    def _no_dupes(pairs):
        seen: dict = {}
        for k, v in pairs:
            if k in seen:
                raise ValueError(f"clave JSON duplicada: {k!r}")
            seen[k] = v
        return seen

    obj = json.loads(path.read_text(), object_pairs_hook=_no_dupes)
    if not isinstance(obj, dict) or not isinstance(obj.get("advisories"), list):
        raise ValueError("python_advisories.json: falta la lista 'advisories'")
    return obj["advisories"]


def validate_advisory_schema(entries: list[dict]) -> list[str]:
    """Esquema estricto: IDs únicos, alias no reusados, campos obligatorios, fecha ISO, enum."""
    probs: list[str] = []
    seen_tokens: dict[str, int] = {}  # id/alias -> índice de entrada (no reutilizables)
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            probs.append(f"advisory #{i}: no es objeto")
            continue
        need = {"id", "aliases", "package", "versions", "profiles", "decision", "owner", "expires_at"}
        missing = need - set(e.keys())
        if missing:
            probs.append(f"advisory #{i}: faltan campos {sorted(missing)}")
            continue
        if e["decision"] not in ("accept",):
            probs.append(f"advisory {e['id']}: decision {e['decision']!r} fuera del enum")
        if not isinstance(e["owner"], str) or not e["owner"].strip():
            probs.append(f"advisory {e['id']}: owner vacío")
        if not isinstance(e["versions"], list) or not e["versions"]:
            probs.append(f"advisory {e['id']}: versions debe ser lista no vacía")
        if not isinstance(e["profiles"], list) or not e["profiles"] or set(e["profiles"]) - set(PROFILES):
            probs.append(f"advisory {e['id']}: profiles inválidos {e.get('profiles')}")
        try:
            dt.date.fromisoformat(str(e["expires_at"]))
        except TypeError, ValueError:
            probs.append(f"advisory {e['id']}: expires_at {e['expires_at']!r} no es fecha ISO")
        toks = [e["id"], *(e.get("aliases") or [])]
        for t in toks:
            if t in seen_tokens and seen_tokens[t] != i:
                probs.append(f"advisory {e['id']}: ID/alias {t!r} reutilizado (ya en entrada #{seen_tokens[t]})")
            seen_tokens[t] = i
    return probs


def _entry_tokens(e: dict) -> set[str]:
    return {e["id"], *(e.get("aliases") or [])}


def run_pip_audit(lock: Path) -> list[dict]:
    """Ejecuta pip-audit en JSON sobre un lock; devuelve [{package,version,id,aliases}]. RAW (sin ignores)."""
    if not lock.exists():
        raise FileNotFoundError(f"lock inexistente: {lock}")
    proc = subprocess.run(
        ["pip-audit", "-r", str(lock), "--no-deps", "--disable-pip", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    # pip-audit sale con código != 0 cuando HAY vulnerabilidades; eso es esperado (parseamos el JSON).
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"pip-audit produjo JSON ilegible para {lock.name}: {exc}; stderr={proc.stderr[:200]}"
        ) from exc
    out: list[dict] = []
    for dep in data.get("dependencies", []):
        for v in dep.get("vulns", []) or []:
            out.append(
                {
                    "package": _norm_pkg(dep.get("name", "")),
                    "version": str(dep.get("version", "")),
                    "id": v.get("id", ""),
                    "aliases": list(v.get("aliases", []) or []),
                }
            )
    return out


def reconcile(observed_by_profile: dict[str, list[dict]], entries: list[dict], today: dt.date) -> list[str]:
    """Biyección EXACTA observado↔permitido por perfil. Devuelve la lista de problemas."""
    probs: list[str] = []
    entries_by_token = {t: e for e in entries for t in _entry_tokens(e)}
    for profile in PROFILES:
        observed = observed_by_profile.get(profile, [])
        allowed = [e for e in entries if profile in e.get("profiles", [])]
        matched_ids: set[str] = set()
        # cada observado debe casar con una entrada permitida (por id/alias), paquete y versión
        for obs in observed:
            toks = {obs["id"], *obs["aliases"]}
            hit = next((e for t in toks if (e := entries_by_token.get(t)) is not None), None)
            if hit is None:
                probs.append(
                    f"[{profile}] advisory NUEVO no permitido: {obs['id']} en {obs['package']} {obs['version']}"
                )
                continue
            if profile not in hit.get("profiles", []):
                probs.append(f"[{profile}] {hit['id']} observado pero NO permitido para este perfil")
                continue
            if _norm_pkg(hit["package"]) != obs["package"]:
                probs.append(f"[{profile}] {hit['id']}: paquete {obs['package']} != permitido {hit['package']}")
            if obs["version"] not in [str(x) for x in hit["versions"]]:
                probs.append(f"[{profile}] {hit['id']}: versión {obs['version']} != permitidas {hit['versions']}")
            if today > dt.date.fromisoformat(str(hit["expires_at"])):
                probs.append(f"[{profile}] {hit['id']}: excepción EXPIRADA ({hit['expires_at']})")
            matched_ids.add(hit["id"])
        # toda excepción permitida para el perfil debe haber sido observada (huérfana ⇒ bloquea)
        for e in allowed:
            if e["id"] not in matched_ids:
                probs.append(f"[{profile}] excepción HUÉRFANA permitida pero no observada: {e['id']} ({e['package']})")
    return probs


def audit(today: dt.date | None = None) -> tuple[list[str], dict]:
    today = today or dt.date.today()
    entries = load_advisories(ADVISORIES)
    probs = validate_advisory_schema(entries)
    if probs:  # sin esquema válido no reconciliamos
        return probs, {}
    observed_by_profile: dict[str, list[dict]] = {p: [] for p in PROFILES}
    lock_hashes: dict[str, str] = {}
    for rel, profile in LOCKS:
        lp = ROOT / rel
        observed_by_profile[profile].extend(run_pip_audit(lp))
        lock_hashes[rel] = "sha256:" + hashlib.sha256(lp.read_bytes()).hexdigest()
    probs += reconcile(observed_by_profile, entries, today)
    receipt = {
        "tool": f"pip-audit=={PIP_AUDIT_VERSION}",
        "advisories_sha256": "sha256:" + hashlib.sha256(ADVISORIES.read_bytes()).hexdigest(),
        "lock_hashes": lock_hashes,
        "observed": {p: sorted({o["id"] for o in v}) for p, v in observed_by_profile.items()},
        "n_accepted": len(entries),
    }
    return probs, receipt


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt", metavar="PATH", help="escribe un receipt JSON determinista")
    ns = ap.parse_args(argv[1:])
    ver = subprocess.run(["pip-audit", "--version"], capture_output=True, text=True, check=False).stdout
    if PIP_AUDIT_VERSION not in ver:
        print(f"⚠ pip-audit {ver.strip()!r} != esperado {PIP_AUDIT_VERSION} (continúo, pero el CI lo pinnea)")
    try:
        probs, receipt = audit()
    except (FileNotFoundError, ValueError) as exc:
        print(f"✗ AUDIT SUPPLY-CHAIN abortado: {exc}")
        return 1
    if ns.receipt and receipt:
        Path(ns.receipt).parent.mkdir(parents=True, exist_ok=True)
        Path(ns.receipt).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if probs:
        print(f"✗ SUPPLY-CHAIN incoherente ({len(probs)}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    obs = receipt.get("observed", {})
    print(f"✓ Supply-chain OK: {receipt.get('n_accepted')} avisos permitidos, biyección por perfil. Observados: {obs}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
