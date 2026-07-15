#!/usr/bin/env python
"""Validador ESTRICTO y SEMÁNTICO de recibos dvc-tool (P0R.5, B17/B19). No confía en el recibo: deriva
lo permitido del repo/plataforma y RECOMPUTA. Un recibo fabricado (procedencia falsa, pip_check roto,
expected≠observed, lock inexistente, SBOM inválido) DEBE ser rechazado.

  python -m tools.validate_dvc_receipt --receipt r.json --sbom sbom.json
  python -m tools.validate_dvc_receipt --cross r_linux sbom_linux r_macos sbom_macos

Exige: ficheros regulares no-symlink; esquema exacto y tipos; `profile=="dvc-tool"`; lock DERIVADO de
la plataforma (no `receipt["lock"]`) y sus hashes + lockset + dvc.in RECOMPUTADOS (los ficheros DEBEN
existir); `expected==observed==` contrato DVC; `pip_check=="ok"`, `version_ok`, `site_cache_confined`,
`smoke_ok` True; `git_dirty` False; `dag_returncode==dvc_status_returncode==0`; SBOM CycloneDX válido con
componentes ÚNICOS cuyo conteo y digest reconstruyen `n_packages`/`inventory_digest`; `sbom_sha256`==sha
real del fichero; y `env_id` RECOMPUTADO cuando la plataforma del recibo == la actual. En `--cross`
además: procedencia/versiones/inventario/DAG idénticos entre plataformas. Fail-closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from tools import lock_contracts as lc
from tools import python_env as pe

ROOT = lc.ROOT
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_GITSHA = re.compile(r"^[0-9a-f]{40}$")
_SITE_CACHE = re.compile(r"^repo/[0-9a-f]{32}$")
# B50: esquema EXACTO de python/platform (no basta con que sean dicts).
_PY_KEYS = {"implementation", "version", "cache_tag", "abi"}
_PLAT_KEYS = {"system", "machine", "libc_or_macos"}
_PYVER = re.compile(r"^3\.14\.\d+")
_ENVID = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^[0-9]+$")  # string decimal ASCII (no dígitos unicode)
_SYSTEMS = {"Darwin", "Linux"}
_MACHINES = {"arm64", "x86_64"}


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError, OSError:
        return None


def _git_has(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", sha], cwd=str(ROOT), capture_output=True).returncode == 0


_KEYS = {
    "schema_version",
    "profile",
    "source_head_sha",
    "checkout_sha",
    "checkout_tree_sha",
    "base_sha",
    "git_dirty",
    "github_run_id",
    "github_run_attempt",
    "env_id",
    "python",
    "platform",
    "lock",
    "lock_sha256",
    "lockset_sha256",
    "dvc_in_sha256",
    "expected",
    "observed",
    "version_ok",
    "pip_check",
    "cache_guard",
    "site_cache_dir",
    "site_cache_confined",
    "dag_returncode",
    "dag_hash",
    "dvc_status_returncode",
    "inventory_digest",
    "n_packages",
    "sbom_component_count",
    "sbom_sha256",
    "smoke_ok",
}
_LOCKS_BY_PLATFORM = {
    "Linux-x86_64": "locks/dvc-tool-linux-x86_64.txt",
    "Darwin-arm64": "locks/dvc-tool-macos-arm64.txt",
}


def _sha(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _no_dup(pairs):
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"clave duplicada {k}")
        d[k] = v
    return d


def _safe_regular(path: Path) -> str | None:
    if path.is_symlink():
        return f"{path.name}: es symlink — prohibido"
    if not path.is_file():
        return f"{path.name}: no existe o no es fichero regular"
    try:
        path.resolve().relative_to(ROOT.resolve())
    except ValueError, OSError:
        return f"{path.name}: fuera del workspace"
    return None


def _load(path: Path) -> tuple[dict | None, str | None]:
    prob = _safe_regular(path)
    if prob:
        return None, prob
    try:
        return json.loads(path.read_text(), object_pairs_hook=_no_dup), None
    except (OSError, ValueError) as exc:
        return None, f"{path.name}: ilegible/duplicado ({exc})"


def validate(receipt_path: Path, sbom_path: Path) -> list[str]:
    probs: list[str] = []
    r, err = _load(receipt_path)
    if err or r is None:
        return [err or f"{receipt_path.name}: ilegible"]
    if set(r) != _KEYS:
        return [f"{receipt_path.name}: esquema {sorted(set(r) ^ _KEYS)} distinto del exacto"]
    n = receipt_path.name

    # B45: IDENTIDAD DE TIPO — 2.0 no es 2, False no es 0 (bool ⊂ int en Python).
    if type(r["schema_version"]) is not int or r["schema_version"] != 2:
        probs.append(f"{n}: schema_version no es int == 2")
    if r["profile"] != "dvc-tool":
        probs.append(f"{n}: profile != dvc-tool")
    for flag in ("smoke_ok", "version_ok", "site_cache_confined"):
        if r[flag] is not True:
            probs.append(f"{n}: {flag} != true")
    if r["git_dirty"] is not False:
        probs.append(f"{n}: git_dirty != false")
    for rc in ("dag_returncode", "dvc_status_returncode"):
        if type(r[rc]) is not int or r[rc] != 0:
            probs.append(f"{n}: {rc} no es int == 0")
    for cnt in ("n_packages", "sbom_component_count"):
        if type(r[cnt]) is not int or r[cnt] < 0:
            probs.append(f"{n}: {cnt} no es int >= 0")
    # B50: esquema EXACTO de python/platform + tipos de run-id/attempt/env_id — un recibo mutado con
    # python={}, plataforma con claves de más/menos o run-id de tipo incorrecto NO se acepta.
    py = r["python"]
    if not isinstance(py, dict) or set(py) != _PY_KEYS:
        probs.append(f"{n}: python con claves != {sorted(_PY_KEYS)}")
    else:
        if py["implementation"] != "cpython":
            probs.append(f"{n}: python.implementation != cpython")
        if not (isinstance(py["version"], str) and _PYVER.match(py["version"])):
            probs.append(f"{n}: python.version no casa 3.14.x")
        for k in ("cache_tag", "abi"):
            if not (isinstance(py[k], str) and py[k]):
                probs.append(f"{n}: python.{k} vacío o no-str")
    pl = r["platform"]
    if not isinstance(pl, dict) or set(pl) != _PLAT_KEYS:
        probs.append(f"{n}: platform con claves != {sorted(_PLAT_KEYS)}")
    else:
        if pl["system"] not in _SYSTEMS:
            probs.append(f"{n}: platform.system {pl['system']!r} inválido")
        if pl["machine"] not in _MACHINES:
            probs.append(f"{n}: platform.machine {pl['machine']!r} inválido")
        if not (isinstance(pl["libc_or_macos"], str) and pl["libc_or_macos"]):
            probs.append(f"{n}: platform.libc_or_macos vacío o no-str")
    if not (isinstance(r["github_run_id"], str) and _DECIMAL.fullmatch(r["github_run_id"])):
        probs.append(f"{n}: github_run_id no es string decimal")
    ra = r["github_run_attempt"]
    if not (isinstance(ra, str) and _DECIMAL.fullmatch(ra) and int(ra) >= 1):
        probs.append(f"{n}: github_run_attempt no es string decimal >= 1")
    if not (isinstance(r["env_id"], str) and _ENVID.fullmatch(r["env_id"])):
        probs.append(f"{n}: env_id no es 64 hex")
    if r["pip_check"] != "ok" or r["cache_guard"] != "ok":
        probs.append(f"{n}: pip_check/cache_guard != ok")
    # B45: el lockset/contrato debe ser válido AHORA para aceptar un recibo.
    if lc.validate_all(ROOT):
        probs.append(f"{n}: lockset/contrato inválido en el repo")
    for f in ("lock_sha256", "lockset_sha256", "dvc_in_sha256", "dag_hash", "sbom_sha256", "inventory_digest"):
        if not isinstance(r[f], str) or not _SHA.fullmatch(r[f]):
            probs.append(f"{n}: {f} no es sha256 válido")
    # B27: PROCEDENCIA git real — cada sha 40-hex, existe, y casa el checkout/variables reales.
    # base_sha es NULLABLE (fuera de un PR no hay base); si está, DEBE ser un sha git real.
    for f in ("source_head_sha", "checkout_sha", "checkout_tree_sha"):
        if not isinstance(r[f], str) or not _GITSHA.fullmatch(r[f]):
            probs.append(f"{n}: {f} no es un sha git de 40 hex")
    if r["base_sha"] is not None and (not isinstance(r["base_sha"], str) or not _GITSHA.fullmatch(r["base_sha"])):
        probs.append(f"{n}: base_sha no es null ni un sha git de 40 hex")
    head, tree = _git("rev-parse", "HEAD"), _git("rev-parse", "HEAD^{tree}")
    if head is None or tree is None:
        probs.append(f"{n}: no se pudo resolver el checkout git — fail-closed")
    else:
        if isinstance(r["checkout_sha"], str) and _GITSHA.fullmatch(r["checkout_sha"]) and r["checkout_sha"] != head:
            probs.append(f"{n}: checkout_sha != git HEAD real")
        if (
            isinstance(r["checkout_tree_sha"], str)
            and _GITSHA.fullmatch(r["checkout_tree_sha"])
            and r["checkout_tree_sha"] != tree
        ):
            probs.append(f"{n}: checkout_tree_sha != árbol real de HEAD")
        for f in ("source_head_sha", "checkout_sha", "base_sha"):
            if isinstance(r[f], str) and _GITSHA.fullmatch(r[f]) and not _git_has(r[f]):
                probs.append(f"{n}: {f}={r[f]} no existe en el repo (git cat-file)")
    for field, envvar in (
        ("source_head_sha", "GITHUB_PR_HEAD_SHA"),
        ("base_sha", "GITHUB_BASE_SHA"),
        ("github_run_id", "GITHUB_RUN_ID"),
        ("github_run_attempt", "GITHUB_RUN_ATTEMPT"),
    ):
        expected = os.environ.get(envvar)
        if expected and str(r[field]) != expected:
            probs.append(f"{n}: {field} != {envvar} ({expected})")
    # site_cache_dir debe tener la forma segura repo/<token de 32 hex>, no `../../outside`
    if not isinstance(r["site_cache_dir"], str) or not _SITE_CACHE.fullmatch(r["site_cache_dir"]):
        probs.append(f"{n}: site_cache_dir {r['site_cache_dir']!r} != patrón repo/<token>")

    # lock DERIVADO de la plataforma del recibo (no confiar en receipt["lock"])
    plat = f"{r['platform'].get('system')}-{r['platform'].get('machine')}" if isinstance(r["platform"], dict) else None
    lock_rel = _LOCKS_BY_PLATFORM.get(plat or "")
    if not lock_rel:
        probs.append(f"{n}: plataforma {plat!r} sin lock dvc-tool derivable")
    elif r["lock"] != lock_rel:
        probs.append(f"{n}: lock declarado {r['lock']!r} != derivado {lock_rel!r}")
    # hashes RECOMPUTADOS (los ficheros DEBEN existir)
    for field, target in (
        ("lock_sha256", ROOT / lock_rel if lock_rel else None),
        ("lockset_sha256", ROOT / lc.MANIFEST_REL),
        ("dvc_in_sha256", ROOT / "requirements/dvc.in"),
    ):
        if target is None or not target.exists():
            probs.append(f"{n}: {field} — fichero de referencia ausente")
        elif r[field] != _sha(target):
            probs.append(f"{n}: {field} no casa el fichero actual")

    # expected==observed== contrato DVC
    contract = {**lc.DVC_TOOL_DIRECT, "diskcache": lc.DVC_TOOL_DISKCACHE}
    if r["expected"] != contract or r["observed"] != contract:
        probs.append(f"{n}: expected/observed != contrato DVC {contract}")

    # env_id RECOMPUTADO si la plataforma del recibo == la actual
    if plat == pe.platform_key():
        if r["env_id"] != pe.env_id("dvc-tool"):
            probs.append(f"{n}: env_id sellado != recomputado en esta plataforma")

    # SBOM: fichero, CycloneDX, componentes únicos, conteo, sha, y reconstrucción del inventario
    sbom, serr = _load(sbom_path)
    if serr or sbom is None:
        probs.append(serr or f"{sbom_path.name}: ilegible")
    else:
        if _sha(sbom_path) != r["sbom_sha256"]:
            probs.append(f"{n}: sbom_sha256 != sha real del fichero SBOM")
        if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.5":
            probs.append(f"{sbom_path.name}: bomFormat/specVersion != CycloneDX/1.5")
        comps = sbom.get("components", []) if isinstance(sbom, dict) else []
        # B34: estructura EXACTA por componente (type/name/version/purl canónico)
        for c in comps:
            nm, ver = c.get("name"), c.get("version")
            if c.get("type") != "library" or not isinstance(nm, str) or not nm or not isinstance(ver, str) or not ver:
                probs.append(f"{sbom_path.name}: componente con type/name/version inválido ({nm!r})")
            elif c.get("purl") != f"pkg:pypi/{nm.lower()}@{ver}":
                probs.append(f"{sbom_path.name}: purl inválido para {nm} ({c.get('purl')!r})")
        names = [(c.get("name"), c.get("version")) for c in comps]
        by_canon = {pe._canon(nm): ver for nm, ver in names if nm}
        if len(by_canon) != len(names):  # duplicados por nombre canónico (grafías distintas incluidas)
            probs.append(f"{sbom_path.name}: componentes con nombre canónico duplicado")
        if len(names) != r["sbom_component_count"] or len(names) != r["n_packages"]:
            probs.append(f"{n}: sbom_component_count/n_packages != componentes del SBOM ({len(names)})")
        inv = sorted(f"{nm}=={ver}" for nm, ver in names if nm and ver)
        if pe._inventory_digest(inv) != r["inventory_digest"]:
            probs.append(f"{n}: inventory_digest != reconstruido del SBOM")
        # B34: el SBOM debe ser EXACTAMENTE el cierre esperado (pins + toolchain), no un superset — un
        # componente extra (evil-extra) o una versión falsa de pip/wheel se rechazan.
        if plat:
            probs += [f"{n}: SBOM {p}" for p in pe._inventory_problems(by_canon, "dvc-tool", None, pe.load_profiles())]
    return probs


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipt")
    ap.add_argument("--sbom")
    ap.add_argument("--cross", nargs=4, metavar=("R1", "SBOM1", "R2", "SBOM2"))
    ns = ap.parse_args(argv[1:])
    probs: list[str] = []
    pairs: list[tuple[Path, Path]] = []
    if ns.receipt and ns.sbom:
        pairs.append((Path(ns.receipt), Path(ns.sbom)))
    elif ns.receipt or ns.sbom:
        ap.error("--receipt exige --sbom")
    if ns.cross:
        pairs += [(Path(ns.cross[0]), Path(ns.cross[1])), (Path(ns.cross[2]), Path(ns.cross[3]))]
    if not pairs:
        ap.error("da --receipt+--sbom o --cross")
    for rp, sp in pairs:
        probs += validate(rp, sp)
    if ns.cross and not probs:
        a = json.loads(Path(ns.cross[0]).read_text())
        b = json.loads(Path(ns.cross[2]).read_text())
        for field in (
            "source_head_sha",
            "base_sha",
            "checkout_tree_sha",
            "dag_hash",
            "inventory_digest",
            "expected",
            "observed",
            "n_packages",
        ):
            if a[field] != b[field]:
                probs.append(f"cross: {field} difiere entre plataformas")
    if probs:
        print("✗ RECIBO(S) dvc-tool inválido(s):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ recibo(s) dvc-tool válido(s) [{len(pairs)} par(es)]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
