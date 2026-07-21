#!/usr/bin/env python
"""B250/B253/B256/B257: validador del recibo de diagnóstico B233 (`reports/governance/b233_receipt.json`).

El recibo es un DIAGNÓSTICO HISTÓRICO (schema v3), no una certificación del checkout actual. Se valida por DERIVACIÓN y
se lee GOBERNADO fd-bound:

- **Procedencia honesta (B256):** `capture_head` es el commit REAL donde se ejecutó el build gobernado (no se
  reetiqueta); `imported_into_repository_at` es donde el recibo se versionó. El validador exige que `capture_head` sea
  un COMMIT, que los `governed_files` recalculados igualen los blobs @capture_head, y que sigan byte-idénticos en el
  checkout actual (si algo relevante cambió, el diagnóstico histórico DEJA de ser aplicable → falla).
- **Todo derivado (B257):** el toolchain (pip/setuptools/wheel) se DERIVA de `environments/python_profiles.json`
  (lectura gobernada), no del recibo. El inventario se DERIVA: `observed(raw_freeze) - expected(dev.txt ∪ toolchain)`
  debe ser exactamente `{visapredictai: 1.0.0}`. `capture_platform`/`capture_command` son EVIDENCIA de captura (no
  rederivables); se validan por forma, no se aceptan como cálculo del sistema actual.
- **Lecturas fd-bound (B257):** cada fichero gobernado (recibo, profiles, dev.txt, los 7 governed_files) se lee
  caminando directorios con `openat(O_DIRECTORY|O_NOFOLLOW)` verificando cada dir (real, UID actual, no escribible por
  grupo/otros) y el leaf con la primitiva (`O_NOFOLLOW|O_NONBLOCK`, S_ISREG, UID, nlink==1, no escribible, snapshot
  fstat pre/post). Un error al CERRAR un descriptor produce un problema (no `pass`). NUNCA se hace `open(ruta)`.
- **Nunca revienta:** todo error de tipo/forma se acumula en la lista de problemas.

FRONTERA HONESTA: el recibo es evidencia local reproducible-en-forma, NO una atestación externa. Sin re-ejecutar
`tools/capture_b233_receipt.py` (que requiere construir el entorno dev gobernado, R9-scope), sigue siendo un
diagnóstico histórico, no una certificación viva.

Uso: `python -m tools.validate_b233_receipt [ruta]` → rc 0 sólo si es válido.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys

from tools import governed_read as gr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_REL = "reports/governance/b233_receipt.json"
_PROFILES = "environments/python_profiles.json"
_DEV_LOCK = "locks/dev.txt"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256_TAG = re.compile(r"^sha256:[0-9a-f]{64}$")
_PKG = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s]+)$")
_GOVERNED_PATHS = (
    "tools/python_env.py",
    "tools/lock_contracts.py",
    "environments/python_profiles.json",
    "locks/dev.txt",
    "locks/lockset.json",
    "pyproject.toml",
    ".python-version",
)
_TOP_KEYS = {
    "schema_version", "capture_kind", "purpose", "capture_head", "capture_platform", "capture_command",
    "imported_into_repository_at", "return_code", "error", "raw_freeze", "capture_freeze_sha256", "governed_files",
    "observed_inventory_size", "expected_inventory_size", "extras_exact", "conclusion",
}  # fmt: skip
_EXPECTED_DELTA = {"visapredictai": "1.0.0"}
_EXPECTED_ARGV_DISPLAY = ["python", "-m", "tools.python_env", "build", "--profile", "dev"]
_EXPECTED_ENV_OVERRIDES = {"PYTHONDONTWRITEBYTECODE": "1"}
_MAX_FREEZE_BYTES = 1 << 16  # B262: cota de tamaño para raw_freeze (evita consumo no acotado)
_MAX_STR = 4096  # B262: cota para strings libres del recibo


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave duplicada en el recibo: {k!r}")
        seen[k] = v
    return seen


def _is_int(v: object) -> bool:
    return type(v) is int  # bool es subtipo de int → excluido


def _is_str(v: object) -> bool:
    return isinstance(v, str)


def _git_is_ancestor(anc: str, desc: str) -> bool:
    """True si `anc` es ancestro (o igual) de `desc`."""
    try:
        return subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", anc, desc], capture_output=True).returncode == 0  # fmt: skip
    except OSError:
        return False


def _git_path_added_at(commit: str, rel: str) -> bool:
    """True si `rel` fue AÑADIDO (diff-filter=A) exactamente en `commit` (el add más reciente en su historia es él)."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "log", "-1", "--diff-filter=A", "--format=%H", commit, "--", rel], capture_output=True, text=True)  # fmt: skip
    except OSError:
        return False
    return out.returncode == 0 and out.stdout.strip() == subprocess.run(["git", "-C", ROOT, "rev-parse", commit], capture_output=True, text=True).stdout.strip()  # fmt: skip


def _imported_commit_problems(imp: object, head: str | None) -> list[str]:
    """B262: `imported_into_repository_at` debe ser un commit REAL de 40-hex, DESCENDIENTE de capture_head, que
    CONTIENE el recibo canónico y donde el recibo fue AÑADIDO. Un commit cualquiera (p. ej. la base de main) NO pasa."""
    if not (isinstance(imp, str) and _HEX40.match(imp)):
        return ["imported_into_repository_at no es 40-hex"]
    if not _git_is_commit(imp):
        return [f"imported_into_repository_at {imp} no es un commit del repo"]
    if _sha_blob(imp, _DEFAULT_REL) is None:
        return ["imported_into_repository_at: el recibo canónico no existe en ese commit"]
    if head is not None and not _git_is_ancestor(head, imp):
        return ["imported_into_repository_at no es descendiente de capture_head"]
    if not _git_path_added_at(imp, _DEFAULT_REL):
        return ["imported_into_repository_at: el recibo canónico no fue AÑADIDO en ese commit"]
    return []


def _git_is_commit(sha: str) -> bool:
    try:
        return subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0  # fmt: skip
    except OSError:
        return False


def _sha_blob(sha: str, rel: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", ROOT, "show", f"{sha}:{rel}"], capture_output=True)
    except OSError:
        return None
    return "sha256:" + hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None


def _dir_problem(fd: int, name: str) -> str | None:
    """B257: gobierna un descriptor de DIRECTORIO — real, del UID actual, no escribible por grupo/otros. None si OK."""
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        return f"{name!r} no es un directorio"
    if st.st_uid != os.geteuid():
        return f"dir {name!r} de UID ajeno ({st.st_uid})"
    if stat.S_IMODE(st.st_mode) & 0o022:
        return f"dir {name!r} escribible por grupo/otros"
    return None


def _governed_bytes(rel: str) -> tuple[bytes | None, str | None]:
    """Lee `rel` (relativo a la raíz) GOBERNADO fd-bound: camina cada directorio con `openat(O_DIRECTORY|O_NOFOLLOW)`
    verificando (dir real, UID actual, no escribible por grupo/otros) y el leaf con `read_governed_bytes` (S_ISREG +
    UID + nlink==1 + no escribible + snapshot fstat pre/post). Un error al CERRAR un descriptor → problema (no pass).
    NUNCA usa `open(ruta)`."""
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or os.path.isabs(rel):
        return None, f"ruta no gobernada {rel!r}"
    fds: list[int] = []
    data: bytes | None = None
    err: str | None = None
    try:
        cur = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(cur)
        err = _dir_problem(cur, "<root>")  # la RAÍZ también se gobierna (real, UID actual, no escribible g/o)
        if err is None:
            for comp in parts[:-1]:
                nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
                fds.append(nfd)
                err = _dir_problem(nfd, comp)
                if err is not None:
                    break
                cur = nfd
            else:
                data, err = gr.read_governed_bytes(cur, parts[-1])
    except OSError as exc:
        err = f"apertura falló ({exc})"
    close_errs: list[str] = []
    for fd in reversed(fds):
        try:
            os.close(fd)
        except OSError as exc:
            close_errs.append(str(exc))
    if close_errs:
        return None, f"error(es) al cerrar descriptores de directorio: {close_errs}"
    return (None, err) if err is not None else (data, None)


def _sha_governed(rel: str) -> tuple[str | None, str | None]:
    data, err = _governed_bytes(rel)
    if data is None:
        return None, err
    return "sha256:" + hashlib.sha256(data).hexdigest(), None


def _canon_pep503(name: str) -> str:
    """Nombre canónico PEP 503: minúsculas y `[-_.]+` colapsado a `-`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_pkgs(text: str, *, where: str) -> tuple[dict[str, str], list[str]]:
    """Parsea un pip-freeze / lock a `{nombre_canónico_PEP503: versión}`. B262: canonicaliza los nombres antes de
    comparar y RECHAZA colisiones (dos nombres crudos que canonicalizan al mismo)."""
    pkgs: dict[str, str] = {}
    probs: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        m = _PKG.match(s)
        if not m:
            probs.append(f"{where}: línea no-pkg {s!r}")
            continue
        canon = _canon_pep503(m.group(1))
        if canon in pkgs:
            probs.append(f"{where}: colisión canónica PEP 503 de {m.group(1)!r} → {canon!r}")
            continue
        pkgs[canon] = m.group(2)
    return pkgs, probs


def _load_profiles() -> tuple[dict | None, str | None]:
    data, err = _governed_bytes(_PROFILES)
    if data is None:
        return None, f"{_PROFILES}: {err}"
    try:
        prof = json.loads(data)
    except ValueError as exc:
        return None, f"{_PROFILES}: no-JSON ({exc})"
    return (prof, None) if isinstance(prof, dict) else (None, f"{_PROFILES}: no es objeto")


def _derive_toolchain() -> tuple[dict[str, str] | None, str | None]:
    """B257: toolchain DERIVADO de `python_profiles.json` (lectura gobernada), no del recibo."""
    prof, err = _load_profiles()
    if prof is None:
        return None, err
    tc = prof.get("toolchain")
    if not (isinstance(tc, dict) and all(isinstance(tc.get(k), str) for k in ("pip", "setuptools", "wheel"))):
        return None, f"{_PROFILES}: sin toolchain pip/setuptools/wheel"
    return tc, None


def _derive_platform_expectation() -> tuple[dict[str, str] | None, str | None]:
    """B257: el sistema/arquitectura de captura NO se aceptan arbitrarios — se DERIVAN del lock que alimenta la
    derivación del inventario (`_DEV_LOCK`): el profile `dev` mapea platform-key→lock, así que el lock macOS
    `locks/dev.txt` implica `Darwin-arm64`. La versión menor de Python se deriva de `python_minor`."""
    prof, err = _load_profiles()
    if prof is None:
        return None, err
    dev = prof.get("profiles", {}).get("dev", {}) if isinstance(prof.get("profiles"), dict) else {}
    locks = dev.get("locks", {}) if isinstance(dev, dict) else {}
    key = next((k for k, v in locks.items() if v == _DEV_LOCK), None) if isinstance(locks, dict) else None
    minor = prof.get("python_minor")
    if not (isinstance(key, str) and "-" in key and isinstance(minor, str)):
        return None, f"{_PROFILES}: no se pudo derivar el platform-key de {_DEV_LOCK}"
    system, machine = key.split("-", 1)
    return {"system": system, "machine": machine, "python_minor": minor}, None


def _safe_keys(d: object) -> str:
    """Repr seguro de las claves de una entrada JSON-like sin ordenar tipos heterogéneos (B262: `sorted()` sobre
    claves mixtas str/int elevaba TypeError)."""
    if not isinstance(d, dict):
        return f"tipo {type(d).__name__}"
    return "{" + ", ".join(repr(k) for k in d) + "}"


def validate_receipt(d: object) -> list[str]:
    """FRONTERA PÚBLICA: garantiza que se DEVUELVE una lista de problemas para CUALQUIER entrada JSON-like (dicts con
    claves mixtas, listas, escalares, objetos) — B262: nunca eleva. Un fallo inesperado del núcleo se reporta como un
    problema estructurado (no se oculta como traceback ni se traga en silencio)."""
    try:
        return _validate_receipt(d)
    except Exception as exc:  # noqa: BLE001 — frontera pública: cualquier entrada mal formada → problema, jamás traceback
        return [f"error inesperado validando el recibo: {type(exc).__name__}: {str(exc)[:120]}"]


def _validate_receipt(d: object) -> list[str]:
    """Checks de ESQUEMA v3 + DERIVACIÓN + PROCEDENCIA (lee ficheros gobernados fd-bound y git para rederivar). Devuelve
    la lista de problemas (vacía = válido). Envuelto por `validate_receipt` para la garantía de no-elevación."""
    probs: list[str] = []
    if not isinstance(d, dict) or set(d.keys()) != _TOP_KEYS:
        return [f"esquema superior != {sorted(_TOP_KEYS)} (obtenido {_safe_keys(d)})"]

    if not (_is_int(d["schema_version"]) and d["schema_version"] == 3):
        probs.append("schema_version no es el entero 3")
    if d["capture_kind"] != "local_governed_build_diagnostic":
        probs.append("capture_kind != 'local_governed_build_diagnostic'")
    for key in ("purpose", "error", "conclusion"):
        if not (_is_str(d[key]) and d[key].strip()):
            probs.append(f"{key} no es un string no vacío")
        elif len(d[key]) > _MAX_STR:
            probs.append(f"{key} excede el tope de tamaño ({_MAX_STR}) (B262)")
    if not (_is_int(d["return_code"]) and d["return_code"] == 1):
        probs.append("return_code no es un entero == 1 (o es bool)")

    # capture_head e imported_into_repository_at: commits reales
    head = d["capture_head"]
    if not (_is_str(head) and _HEX40.match(head)):
        probs.append("capture_head no es 40-hex")
        head = None
    elif not _git_is_commit(head):
        probs.append(f"capture_head {head} no es un commit del repo")
        head = None
    probs.extend(_imported_commit_problems(d["imported_into_repository_at"], head))

    # capture_platform: forma EXACTA + sistema/arquitectura/python DERIVADOS del lock+profiles (no arbitrarios, B257)
    pl = d["capture_platform"]
    if not (isinstance(pl, dict) and set(pl.keys()) == {"system", "machine", "python"} and all(_is_str(pl[k]) and pl[k].strip() for k in pl)):  # fmt: skip
        probs.append("capture_platform no tiene EXACTAMENTE {system, machine, python} str no vacíos")
    else:
        exp_pl, plerr = _derive_platform_expectation()
        if exp_pl is None:
            probs.append(plerr or "capture_platform no derivable")
        elif pl["system"] != exp_pl["system"] or pl["machine"] != exp_pl["machine"]:
            probs.append(f"capture_platform system/machine != derivado del lock ({exp_pl['system']}-{exp_pl['machine']})")  # fmt: skip
        elif not (re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pl["python"]) and pl["python"].rsplit(".", 1)[0] == exp_pl["python_minor"]):  # fmt: skip
            probs.append(f"capture_platform.python no es X.Y.Z con major.minor == {exp_pl['python_minor']} (B262)")
    # capture_command: EVIDENCIA de captura. HONESTO (B262): argv_display es lo que un humano teclearía (NO el argv
    # ejecutado con intérprete absoluto); environment_overrides son SÓLO los overrides (NO el entorno efectivo).
    cmd = d["capture_command"]
    if not (isinstance(cmd, dict) and set(cmd.keys()) == {"argv_display", "environment_overrides"}):
        probs.append("capture_command no tiene EXACTAMENTE {argv_display, environment_overrides}")
    else:
        if cmd["argv_display"] != _EXPECTED_ARGV_DISPLAY:
            probs.append(f"capture_command.argv_display != {_EXPECTED_ARGV_DISPLAY}")
        if cmd["environment_overrides"] != _EXPECTED_ENV_OVERRIDES:
            probs.append(f"capture_command.environment_overrides != {_EXPECTED_ENV_OVERRIDES}")

    # governed_files: sha recalculado == blob@capture_head == fichero actual gobernado (procedencia + aplicabilidad)
    gf = d["governed_files"]
    if not (isinstance(gf, dict) and set(gf.keys()) == set(_GOVERNED_PATHS)):
        probs.append(f"governed_files no tiene EXACTAMENTE las rutas {sorted(_GOVERNED_PATHS)}")
    else:
        for rel in _GOVERNED_PATHS:
            recorded = gf[rel]
            if not (_is_str(recorded) and _SHA256_TAG.match(recorded)):
                probs.append(f"governed_files[{rel}] no es un sha256:… válido")
                continue
            if head is not None:
                blob = _sha_blob(head, rel)
                if blob is None:
                    probs.append(f"governed_files[{rel}]: no existe en el árbol de capture_head")
                elif blob != recorded:
                    probs.append(f"governed_files[{rel}]: sha registrado != blob@capture_head (procedencia falsa)")
            now, gerr = _sha_governed(rel)
            if now is None:
                probs.append(f"governed_files[{rel}]: lectura gobernada falló ({gerr})")
            elif now != recorded:
                probs.append(f"governed_files[{rel}]: cambió entre capture_head y el checkout actual → el diagnóstico histórico YA NO es aplicable")  # fmt: skip

    # raw_freeze + su sha256
    raw = d["raw_freeze"]
    if not _is_str(raw):
        probs.append("raw_freeze no es un string")
        return probs
    if len(raw) > _MAX_FREEZE_BYTES:
        probs.append(f"raw_freeze excede el tope de tamaño ({_MAX_FREEZE_BYTES}) (B262)")
        return probs
    if not (_is_str(d["capture_freeze_sha256"]) and hashlib.sha256(raw.encode()).hexdigest() == d["capture_freeze_sha256"]):  # fmt: skip
        probs.append("capture_freeze_sha256 no corresponde a raw_freeze")

    # inventario DERIVADO con toolchain DERIVADO de profiles (no del recibo)
    observed, op = _parse_pkgs(raw, where="raw_freeze")
    probs.extend(op)
    toolchain, terr = _derive_toolchain()
    dev_txt, derr = _governed_bytes(_DEV_LOCK)
    if terr:
        probs.append(terr)
    if dev_txt is None:
        probs.append(f"{_DEV_LOCK}: {derr}")
    if toolchain is not None and dev_txt is not None:
        expected, ep = _parse_pkgs(dev_txt.decode("utf-8", "replace"), where=_DEV_LOCK)
        probs.extend(ep)
        for tool in ("pip", "setuptools", "wheel"):
            expected.setdefault(tool, toolchain[tool])
        delta = {n: v for n, v in observed.items() if expected.get(n) != v}
        missing = sorted(n for n in expected if n not in observed)
        if delta != _EXPECTED_DELTA:
            probs.append(f"observed - expected != {_EXPECTED_DELTA} (obtenido {delta})")
        if missing:
            probs.append(f"paquetes esperados ausentes del freeze: {missing}")
        if not (_is_int(d["observed_inventory_size"]) and d["observed_inventory_size"] == len(observed)):
            probs.append(f"observed_inventory_size != {len(observed)} derivado")
        if not (_is_int(d["expected_inventory_size"]) and d["expected_inventory_size"] == len(expected)):
            probs.append(f"expected_inventory_size != {len(expected)} derivado")
        if d["extras_exact"] != sorted(delta):
            probs.append(f"extras_exact != {sorted(delta)} (obtenido {d['extras_exact']!r})")
    return probs


def validate_receipt_file(path: str) -> list[str]:
    """Lee el recibo GOBERNADO fd-bound SÓLO si `path` es la ruta versionada canónica, parsea el JSON sin claves
    duplicadas y delega a `validate_receipt`. Fail-closed en cada paso."""
    rel = os.path.relpath(os.path.realpath(path), ROOT)
    if rel != _DEFAULT_REL:
        return [f"{path}: no es el recibo versionado canónico ({_DEFAULT_REL})"]
    data, err = _governed_bytes(rel)
    if data is None:
        return [f"{path}: lectura gobernada falló ({err})"]
    try:
        d = json.loads(data.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
    except (UnicodeDecodeError, ValueError) as exc:
        return [f"{path}: JSON inválido/duplicado ({exc})"]
    return validate_receipt(d)


def read_and_validate_canonical() -> tuple[bytes | None, list[str]]:
    """B267: lee el recibo canónico UNA sola vez fd-bound (openat encadenado + snapshot, sin seguir symlinks), lo
    valida, y devuelve `(bytes_validados, problemas)`. Los bytes devueltos son EXACTAMENTE los leídos y validados — el
    consumidor NO debe reabrir por ruta. `bytes` es None si hay cualquier problema (identidad/modo/nlink/JSON/schema/
    derivación)."""
    data, err = _governed_bytes(_DEFAULT_REL)
    if data is None:
        return None, [f"{_DEFAULT_REL}: lectura gobernada falló ({err})"]
    try:
        d = json.loads(data.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
    except (UnicodeDecodeError, ValueError) as exc:
        return None, [f"{_DEFAULT_REL}: JSON inválido/duplicado ({exc})"]
    probs = validate_receipt(d)
    return (None, probs) if probs else (data, [])


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        sys.stderr.write("uso: python -m tools.validate_b233_receipt [ruta]\n")
        return 2
    path = argv[1] if len(argv) == 2 else os.path.join(ROOT, _DEFAULT_REL)
    probs = validate_receipt_file(path)
    if probs:
        print("✗ recibo B233 inválido:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ recibo B233 v3 válido (diagnóstico histórico derivado + gobernado, capture_head verificado): {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
