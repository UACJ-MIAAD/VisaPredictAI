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
import os
import re
import subprocess
import sys
import tempfile
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
    ("locks/deep-macos-arm64.txt", "deep"),
    ("locks/deep-linux-x86_64-cpu.txt", "deep"),
    ("locks/deep-linux-x86_64-cu126.txt", "deep"),
]

# --- Contrato del perfil DEEP (P0R.4): los 3 locks deep DEBEN fijar EXACTO el cierre directo de
#     requirements/deep.in (torch aparte por variante) y llevar hashes. Divergencia ⇒ bloquea. ----
DEEP_LOCKS = ("locks/deep-macos-arm64.txt", "locks/deep-linux-x86_64-cpu.txt", "locks/deep-linux-x86_64-cu126.txt")
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
    "setuptools": "81.0.0",
}
DEEP_TORCH: dict[str, str] = {
    "locks/deep-macos-arm64.txt": "2.12.1",
    "locks/deep-linux-x86_64-cpu.txt": "2.12.1+cpu",
    "locks/deep-linux-x86_64-cu126.txt": "2.12.1+cu126",
}

# --- Cierre del hueco de versión LOCAL (P0R.4): pip-audit consulta OSV por la versión EXACTA del
#     lock. Un wheel de índice no-PyPI con versión local (torch 2.12.1+cpu) puede recibir "sin
#     vulnerabilidades" porque OSV no reconoce ese artefacto. Para cada (lock, paquete) AUTORIZADO
#     se AÑADE una consulta con la versión PÚBLICA normalizada; el universo de findings es la UNIÓN.
#     ACOTADO: una versión local NO declarada aquí BLOQUEA (fail-closed) — nunca se normaliza sola.
LOCAL_VERSION_QUERIES: dict[tuple[str, str], str] = {
    ("locks/deep-linux-x86_64-cpu.txt", "torch"): "2.12.1",
    ("locks/deep-linux-x86_64-cu126.txt", "torch"): "2.12.1",
}
_PIN_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s\\;]+)")
_ADV_ID = re.compile(r"^(?:CVE-\d{4}-\d+|PYSEC-\d{4}-\d+|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})$")
_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENTRY_KEYS = frozenset(
    {
        "id",
        "aliases",
        "package",
        "versions",
        "profiles",
        "locks",
        "decision",
        "severity",
        "scope",
        "owner",
        "expires_at",
        "rationale",
    }
)


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
    if obj.get("schema_version") != 1:
        raise ValueError(f"python_advisories.json: schema_version {obj.get('schema_version')!r} != 1")
    unknown = set(obj) - {"schema_version", "_doc", "advisories"}
    if unknown:
        raise ValueError(f"python_advisories.json: claves desconocidas {sorted(unknown)}")
    return obj["advisories"]


def validate_advisory_schema(entries: list[dict]) -> list[str]:
    """Esquema estricto: IDs únicos, alias no reusados, campos obligatorios, fecha ISO, enum."""
    probs: list[str] = []
    seen_tokens: dict[str, int] = {}  # id/alias -> índice de entrada (no reutilizables)
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            probs.append(f"advisory #{i}: no es objeto")
            continue
        need = _ENTRY_KEYS - {"locks"}
        missing = need - set(e.keys())
        if missing:
            probs.append(f"advisory #{i}: faltan campos {sorted(missing)}")
            continue
        unknown = set(e) - _ENTRY_KEYS
        if unknown:
            probs.append(f"advisory #{i}: claves desconocidas {sorted(unknown)}")
        aid = e.get("id")
        aliases = e.get("aliases")
        package = e.get("package")
        versions = e.get("versions")
        profiles = e.get("profiles")
        locks = e.get("locks")
        if not isinstance(aid, str) or not _ADV_ID.fullmatch(aid):
            probs.append(f"advisory #{i}: id inválido {aid!r}")
            continue
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not _ADV_ID.fullmatch(a) for a in aliases):
            probs.append(f"advisory {aid}: aliases inválidos {aliases!r}")
            aliases = []
        elif len(set(aliases)) != len(aliases) or aid in aliases:
            probs.append(f"advisory {aid}: aliases duplicados o iguales al id")
        if not isinstance(package, str) or not _PKG.fullmatch(package) or _norm_pkg(package) != package:
            probs.append(f"advisory {aid}: package debe estar normalizado, recibido {package!r}")
        if e["decision"] not in ("accept",):
            probs.append(f"advisory {aid}: decision {e['decision']!r} fuera del enum")
        if e.get("severity") not in {"low", "moderate", "high", "critical"}:
            probs.append(f"advisory {aid}: severity inválida {e.get('severity')!r}")
        for field in ("scope", "owner", "rationale"):
            if not isinstance(e.get(field), str) or not e[field].strip():
                probs.append(f"advisory {aid}: {field} vacío/no-string")
        if not isinstance(e["owner"], str) or not e["owner"].strip():
            probs.append(f"advisory {aid}: owner vacío")
        if (
            not isinstance(versions, list)
            or not versions
            or any(not isinstance(v, str) or not v.strip() for v in versions)
            or len(set(versions)) != len(versions)
        ):
            probs.append(f"advisory {aid}: versions debe ser lista única de strings no vacíos")
        if (
            not isinstance(profiles, list)
            or not profiles
            or any(not isinstance(p, str) for p in profiles)
            or set(profiles) - set(PROFILES)
            or len(set(profiles)) != len(profiles)
        ):
            probs.append(f"advisory {aid}: profiles inválidos {profiles}")
        if locks is not None:
            known_locks = {rel for rel, _profile in LOCKS}
            if (
                not isinstance(locks, list)
                or not locks
                or any(not isinstance(lock, str) for lock in locks)
                or set(locks) - known_locks
                or len(set(locks)) != len(locks)
            ):
                probs.append(f"advisory {aid}: locks inválidos {locks!r}")
        try:
            dt.date.fromisoformat(str(e["expires_at"]))
        except TypeError, ValueError:
            probs.append(f"advisory {aid}: expires_at {e['expires_at']!r} no es fecha ISO")
        toks = [aid, *aliases]
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
    # pip-audit: 0 = limpio; 1 = halló vulnerabilidades. Cualquier otro código es un fallo
    # operacional y NO puede convertirse en "cero findings".
    if proc.returncode not in (0, 1):
        raise ValueError(
            f"pip-audit falló operacionalmente para {lock.name}: exit={proc.returncode}; stderr={proc.stderr[:200]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"pip-audit produjo JSON ilegible para {lock.name}: {exc}; stderr={proc.stderr[:200]}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
        raise ValueError(f"pip-audit JSON sin lista dependencies para {lock.name}")
    out: list[dict] = []
    for dep in data["dependencies"]:
        if not isinstance(dep, dict) or not isinstance(dep.get("vulns", []), list):
            raise ValueError(f"pip-audit JSON con dependency inválida para {lock.name}")
        for v in dep.get("vulns", []) or []:
            if not isinstance(v, dict) or not isinstance(v.get("id"), str):
                raise ValueError(f"pip-audit JSON con vulnerability inválida para {lock.name}")
            out.append(
                {
                    "package": _norm_pkg(dep.get("name", "")),
                    "version": str(dep.get("version", "")),
                    "id": v.get("id", ""),
                    "aliases": list(v.get("aliases", []) or []),
                }
            )
    if (proc.returncode == 0 and out) or (proc.returncode == 1 and not out):
        raise ValueError(f"pip-audit salida incoherente para {lock.name}: exit={proc.returncode}, findings={len(out)}")
    return out


def reconcile_lock(observed: list[dict], entries: list[dict], *, profile: str, lock: str, today: dt.date) -> list[str]:
    """Biyección EXACTA observado↔permitido para UN lock; otro lock nunca puede ocultarlo."""
    probs: list[str] = []
    entries_by_token = {t: e for e in entries for t in _entry_tokens(e)}
    allowed = [
        e for e in entries if profile in e.get("profiles", []) and (not e.get("locks") or lock in e.get("locks", []))
    ]
    matched_ids: set[str] = set()
    seen_findings: set[tuple[str, str, str]] = set()
    for obs in observed:
        toks = {obs["id"], *obs["aliases"]}
        hits = {entries_by_token[t]["id"]: entries_by_token[t] for t in toks if t in entries_by_token}
        if not hits:
            probs.append(f"[{profile}:{lock}] advisory NUEVO: {obs['id']} en {obs['package']} {obs['version']}")
            continue
        if len(hits) != 1:
            probs.append(f"[{profile}:{lock}] finding AMBIGUO {sorted(toks)} casa con {sorted(hits)}")
            continue
        hit = next(iter(hits.values()))
        finding_key = (hit["id"], obs["package"], obs["version"])
        if finding_key in seen_findings:
            probs.append(f"[{profile}:{lock}] finding DUPLICADO {finding_key}")
        seen_findings.add(finding_key)
        if hit not in allowed:
            probs.append(f"[{profile}:{lock}] {hit['id']} observado pero NO permitido para este lock/perfil")
            continue
        if _norm_pkg(hit["package"]) != obs["package"]:
            probs.append(f"[{profile}:{lock}] {hit['id']}: paquete {obs['package']} != {hit['package']}")
        if obs["version"] not in [str(x) for x in hit["versions"]]:
            probs.append(f"[{profile}:{lock}] {hit['id']}: versión {obs['version']} != {hit['versions']}")
        if today > dt.date.fromisoformat(str(hit["expires_at"])):
            probs.append(f"[{profile}:{lock}] {hit['id']}: excepción EXPIRADA ({hit['expires_at']})")
        matched_ids.add(hit["id"])
    for e in allowed:
        if e["id"] not in matched_ids:
            probs.append(f"[{profile}:{lock}] excepción HUÉRFANA no observada: {e['id']} ({e['package']})")
    return probs


def read_pins(lock: Path) -> dict[str, str]:
    """paquete_normalizado -> versión (primer ==). Ignora comentarios, opciones (-…) y hashes."""
    pins: dict[str, str] = {}
    for ln in lock.read_text().splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", "-")):
            continue
        m = _PIN_LINE.match(s)
        if m:
            pins.setdefault(_norm_pkg(m.group(1)), m.group(2))
    return pins


def check_local_version_policy(lock_rel: str, pins: dict[str, str]) -> list[str]:
    """FAIL-CLOSED: toda versión local (`+…`) del lock debe estar DECLARADA en LOCAL_VERSION_QUERIES
    con su parte pública coincidente; y toda consulta declarada debe corresponder a una versión
    local presente. Impide ocultar una vuln tras una etiqueta local no autorizada."""
    probs: list[str] = []
    for pkg, ver in pins.items():
        if "+" in ver:
            key = (lock_rel, pkg)
            if key not in LOCAL_VERSION_QUERIES:
                probs.append(
                    f"[{lock_rel}] versión local NO declarada: {pkg}=={ver} (autorizar en LOCAL_VERSION_QUERIES)"
                )
            elif LOCAL_VERSION_QUERIES[key] != ver.split("+", 1)[0]:
                probs.append(
                    f"[{lock_rel}] {pkg}: normalización declarada {LOCAL_VERSION_QUERIES[key]} != pública del lock {ver.split('+', 1)[0]}"
                )
    for (lk, pkg), _public in LOCAL_VERSION_QUERIES.items():
        if lk != lock_rel:
            continue
        if pkg not in pins:
            probs.append(f"[{lock_rel}] normalización declarada para {pkg} pero ausente del lock")
        elif "+" not in pins[pkg]:
            probs.append(
                f"[{lock_rel}] normalización declarada para {pkg} pero el lock no fija versión local ({pins[pkg]})"
            )
    return probs


def run_pip_audit_normalized(lock_rel: str) -> list[dict]:
    """Consulta pip-audit por las versiones PÚBLICAS autorizadas de este lock (cierre del hueco
    de versión local). Devuelve findings extra a UNIR con los del lock as-is."""
    out: list[dict] = []
    for (lk, pkg), public in sorted(LOCAL_VERSION_QUERIES.items()):
        if lk != lock_rel:
            continue
        fd, synth = tempfile.mkstemp(suffix=".txt", prefix="vp_norm_")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{pkg}=={public}\n")
            out.extend(run_pip_audit(Path(synth)))
        finally:
            Path(synth).unlink(missing_ok=True)
    return out


def validate_deep_lock_contract() -> list[str]:
    """Los 3 locks deep fijan EXACTO el cierre directo de deep.in (torch por variante) y llevan hashes."""
    probs: list[str] = []
    for rel in DEEP_LOCKS:
        lp = ROOT / rel
        if not lp.exists():
            probs.append(f"deep lock ausente: {rel}")
            continue
        text = lp.read_text()
        pins = read_pins(lp)
        for pkg, ver in DEEP_DIRECT.items():
            if pins.get(pkg) != ver:
                probs.append(f"[{rel}] {pkg}: {pins.get(pkg)} != {ver} esperado (requirements/deep.in)")
        if pins.get("torch") != DEEP_TORCH[rel]:
            probs.append(f"[{rel}] torch: {pins.get('torch')} != {DEEP_TORCH[rel]} esperado")
        if "--hash=sha256:" not in text:
            probs.append(f"[{rel}] sin hashes (deep exige --generate-hashes)")
    return probs


def audit(today: dt.date | None = None) -> tuple[list[str], dict]:
    today = today or dt.date.today()
    entries = load_advisories(ADVISORIES)
    probs = validate_advisory_schema(entries)
    if probs:  # sin esquema válido no reconciliamos
        return probs, {}
    probs += validate_deep_lock_contract()
    observed_by_lock: dict[str, list[dict]] = {}
    lock_hashes: dict[str, str] = {}
    normalizations: dict[str, list[str]] = {}
    for rel, profile in LOCKS:
        lp = ROOT / rel
        pins = read_pins(lp)
        probs += check_local_version_policy(rel, pins)
        observed = run_pip_audit(lp)
        # registra la consulta normalizada REALIZADA (evidencia de que el hueco de versión local se
        # revisó), sea o no que haya hallado algo — un receipt sin ella no probaría la cobertura.
        applicable = sorted(f"{pkg}=={pub}" for (lk, pkg), pub in LOCAL_VERSION_QUERIES.items() if lk == rel)
        if applicable:
            normalizations[rel] = applicable
        extra = run_pip_audit_normalized(rel)
        all_observed = observed + extra
        observed_by_lock[rel] = all_observed
        probs += reconcile_lock(all_observed, entries, profile=profile, lock=rel, today=today)
        lock_hashes[rel] = "sha256:" + hashlib.sha256(lp.read_bytes()).hexdigest()
    receipt = {
        "tool": f"pip-audit=={PIP_AUDIT_VERSION}",
        "evaluated_on": today.isoformat(),
        "advisories_sha256": "sha256:" + hashlib.sha256(ADVISORIES.read_bytes()).hexdigest(),
        "lock_hashes": lock_hashes,
        "normalizations": normalizations,
        "observed": {
            rel: sorted(
                ({"id": o["id"], "package": o["package"], "version": o["version"]} for o in observed),
                key=lambda x: (x["package"], x["version"], x["id"]),
            )
            for rel, observed in observed_by_lock.items()
        },
        "n_accepted": len(entries),
    }
    return probs, receipt


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt", metavar="PATH", help="escribe un receipt JSON determinista")
    ns = ap.parse_args(argv[1:])
    ver_proc = subprocess.run(["pip-audit", "--version"], capture_output=True, text=True, check=False)
    installed = ver_proc.stdout.strip().split()[-1] if ver_proc.returncode == 0 and ver_proc.stdout.strip() else ""
    if installed != PIP_AUDIT_VERSION:
        print(f"✗ pip-audit {installed or ver_proc.stderr.strip()!r} != esperado {PIP_AUDIT_VERSION}")
        return 1
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
