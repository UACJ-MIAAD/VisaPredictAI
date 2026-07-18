#!/usr/bin/env python
"""B250/B253: validador ESTRICTO, DERIVADO y ejecutable del recibo de diagnóstico B233
(`reports/governance/b233_receipt.json`).

Endurecido en B253 — el recibo YA NO se cree por su forma, se REDERIVA contra la realidad y se lee GOBERNADO:

- **Lectura gobernada (openat):** el fichero se abre caminando `reports/`→`governance/` con `O_DIRECTORY|O_NOFOLLOW`
  desde la raíz y el leaf con la primitiva única (`O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, `fstat` `S_ISREG` + UID actual +
  `nlink == 1` + no escribible por grupo/otros + snapshot fstat pre/post idéntico). NO hay check-then-open por ruta.
- **Esquema EXACTO y tipado:** claves superiores exactas; `schema_version is 2` (int, no bool); cada campo con su tipo
  y valor exactos; `bool` jamás cuenta como `int`; claves extra en `platform`/`toolchain`/`command` REVIENTAN.
- **`git_head` es un COMMIT:** `git cat-file -e <sha>^{commit}` (un blob/árbol/tag NO pasa).
- **Shas gobernados RECALCULADOS:** cada `governed_files[path]` se recomputa del fichero ACTUAL y se exige
  `recorded == actual == blob@git_head` (integridad del contenido + inmutabilidad commit↔checkout; el `path` es la
  clave, así que un sha mal-etiquetado no puede colarse).
- **Inventario DERIVADO:** `observed` = parseo de `raw_freeze`; `expected` = parseo de `locks/dev.txt` ∪ toolchain;
  `observed - expected` debe ser EXACTAMENTE `{visapredictai: 1.0.0}` y nada esperado puede faltar. Los tamaños
  registrados deben IGUALAR los derivados (no se confían).
- **Nunca revienta:** todo error de tipo/forma se acumula en la lista de problemas; jamás un traceback.

Uso: `python -m tools.validate_b233_receipt [ruta]` (por defecto el recibo versionado) → rc 0 sólo si es válido.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

from tools import governed_read as gr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_REL = "reports/governance/b233_receipt.json"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256_TAG = re.compile(r"^sha256:[0-9a-f]{64}$")
_PKG = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s]+)$")
_GOVERNED_PATHS = (
    "tools/python_env.py",
    "environments/python_profiles.json",
    "tools/lock_contracts.py",
    "locks/dev.txt",
    "locks/lockset.json",
)
_TOP_KEYS = {
    "schema_version", "purpose", "git_head", "platform", "toolchain", "governed_files", "command", "return_code",
    "error", "raw_freeze", "raw_freeze_sha256", "observed_inventory_size", "expected_inventory_size", "extras_exact",
    "pip_check", "conclusion",
}  # fmt: skip
_EXPECTED_DELTA = {"visapredictai": "1.0.0"}
_EXPECTED_ARGV = ["python", "-m", "tools.python_env", "build", "--profile", "dev"]
_EXPECTED_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave duplicada en el recibo: {k!r}")
        seen[k] = v
    return seen


def _is_int(v: object) -> bool:
    """int REAL (bool es subtipo de int → excluido explícitamente)."""
    return type(v) is int


def _is_str(v: object) -> bool:
    return isinstance(v, str)


def _git_is_commit(sha: str) -> bool:
    """True SÓLO si `sha` resuelve a un COMMIT (no un blob/árbol/tag): `git cat-file -e <sha>^{commit}`."""
    try:
        return subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0  # fmt: skip
    except OSError:
        return False


def _sha_file(rel: str) -> str | None:
    try:
        with open(os.path.join(ROOT, rel), "rb") as fh:
            return "sha256:" + hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _sha_blob(sha: str, rel: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", ROOT, "show", f"{sha}:{rel}"], capture_output=True)
    except OSError:
        return None
    return "sha256:" + hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None


def _parse_pkgs(text: str, *, where: str) -> tuple[dict[str, str], list[str]]:
    """Parsea un texto pip-freeze / lock a `{name: version}`. FALLA (problema) ante línea no-`pkg==ver` o duplicado.
    Ignora comentarios (`#…`) y líneas en blanco. Devuelve `(pkgs, problems)`."""
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
        if m.group(1) in pkgs:
            probs.append(f"{where}: pkg duplicado {m.group(1)!r}")
            continue
        pkgs[m.group(1)] = m.group(2)
    return pkgs, probs


def validate_receipt(d: object) -> list[str]:
    """Checks de ESQUEMA + DERIVACIÓN (sin leer el propio recibo de disco; sí lee ficheros gobernados y git para
    rederivar). NUNCA lanza: todo problema se acumula. Devuelve la lista de problemas (vacía = válido)."""
    probs: list[str] = []
    if not isinstance(d, dict) or set(d.keys()) != _TOP_KEYS:
        return [f"esquema superior != {sorted(_TOP_KEYS)} (obtenido {sorted(d) if isinstance(d, dict) else type(d)})"]

    # --- tipos escalares exactos ---
    if d["schema_version"] is not True and _is_int(d["schema_version"]) and d["schema_version"] == 2:
        pass
    else:
        probs.append("schema_version no es el entero 2")
    for key in ("purpose", "error", "conclusion"):
        if not (_is_str(d[key]) and d[key].strip()):
            probs.append(f"{key} no es un string no vacío")
    if not (_is_int(d["return_code"]) and d["return_code"] == 1):
        probs.append("return_code no es un entero == 1 (o es bool)")
    if d["pip_check"] is not True:
        probs.append("pip_check no es True")

    # --- git_head 40-hex Y es un COMMIT ---
    head = d["git_head"]
    if not (_is_str(head) and _HEX40.match(head)):
        probs.append("git_head no es 40-hex")
        head = None
    elif not _git_is_commit(head):
        probs.append(f"git_head {head} no es un commit del repo")
        head = None

    # --- platform / toolchain: claves EXACTAS, todos str no vacíos ---
    for key, exact in (("platform", {"system", "machine", "python"}), ("toolchain", {"pip", "setuptools", "wheel"})):
        v = d[key]
        if not (isinstance(v, dict) and set(v.keys()) == exact and all(_is_str(v[k]) and v[k].strip() for k in exact)):
            probs.append(f"{key} no tiene EXACTAMENTE las claves {sorted(exact)} con valores str no vacíos")

    # --- command: {argv, environment} exactos ---
    cmd = d["command"]
    if not (isinstance(cmd, dict) and set(cmd.keys()) == {"argv", "environment"}):
        probs.append("command no tiene EXACTAMENTE las claves ['argv', 'environment']")
    else:
        if cmd["argv"] != _EXPECTED_ARGV:
            probs.append(f"command.argv != {_EXPECTED_ARGV} (obtenido {cmd['argv']!r})")
        if cmd["environment"] != _EXPECTED_ENV:
            probs.append(f"command.environment != {_EXPECTED_ENV} (obtenido {cmd['environment']!r})")

    # --- governed_files: claves EXACTAS, sha recalculado == actual == blob@git_head ---
    gf = d["governed_files"]
    if not (isinstance(gf, dict) and set(gf.keys()) == set(_GOVERNED_PATHS)):
        probs.append(f"governed_files no tiene EXACTAMENTE las rutas {sorted(_GOVERNED_PATHS)}")
    else:
        for rel in _GOVERNED_PATHS:
            recorded = gf[rel]
            if not (_is_str(recorded) and _SHA256_TAG.match(recorded)):
                probs.append(f"governed_files[{rel}] no es un sha256:… válido")
                continue
            actual = _sha_file(rel)
            if actual is None:
                probs.append(f"governed_files[{rel}]: fichero actual ilegible")
            elif actual != recorded:
                probs.append(f"governed_files[{rel}]: sha registrado != fichero actual")
            if head is not None:
                blob = _sha_blob(head, rel)
                if blob is None:
                    probs.append(f"governed_files[{rel}]: no existe en el árbol de git_head")
                elif blob != recorded:
                    probs.append(f"governed_files[{rel}]: sha registrado != blob@git_head (cambió commit↔checkout)")

    # --- raw_freeze + su sha256 ---
    raw = d["raw_freeze"]
    if not _is_str(raw):
        probs.append("raw_freeze no es un string")
        return probs  # sin freeze no se puede derivar el inventario
    if not (_is_str(d["raw_freeze_sha256"]) and hashlib.sha256(raw.encode()).hexdigest() == d["raw_freeze_sha256"]):
        probs.append("raw_freeze_sha256 no corresponde a raw_freeze")

    # --- inventario DERIVADO: observed(raw_freeze) - expected(dev.txt ∪ toolchain) == {visapredictai: 1.0.0} ---
    observed, op = _parse_pkgs(raw, where="raw_freeze")
    probs.extend(op)
    dev_txt = None
    try:
        with open(os.path.join(ROOT, "locks/dev.txt"), encoding="utf-8") as fh:
            dev_txt = fh.read()
    except OSError as exc:
        probs.append(f"locks/dev.txt ilegible ({exc})")
    tc = d["toolchain"]
    if dev_txt is not None and isinstance(tc, dict):
        expected, ep = _parse_pkgs(dev_txt, where="locks/dev.txt")
        probs.extend(ep)
        for tool in ("pip", "setuptools", "wheel"):
            tv = tc.get(tool)
            if isinstance(tv, str):
                expected.setdefault(tool, tv)
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


def _governed_read_bytes(rel: str) -> tuple[bytes | None, str | None]:
    """Lee `rel` (multi-componente, relativo a la raíz) GOBERNADO: camina cada directorio con `O_DIRECTORY|O_NOFOLLOW`
    desde la raíz y lee el leaf con `read_governed_bytes` (UID actual + nlink==1 + no escribible + snapshot pre/post)."""
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or os.path.isabs(rel):
        return None, f"ruta no gobernada {rel!r}"
    root_fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fds: list[int] = []
    try:
        cur = root_fd
        for comp in parts[:-1]:
            try:
                nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
            except OSError as exc:
                return None, f"directorio {comp!r} no gobernado ({exc})"
            fds.append(nfd)
            cur = nfd
        return gr.read_governed_bytes(cur, parts[-1])
    finally:
        for fd in reversed(fds):
            with_close(fd)
        with_close(root_fd)


def with_close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def validate_receipt_file(path: str) -> list[str]:
    """Lee el recibo GOBERNADO (openat encadenado + snapshot) SÓLO si `path` es la ruta versionada canónica, parsea
    el JSON sin claves duplicadas y delega el esquema/derivación a `validate_receipt`. Fail-closed en cada paso."""
    rel = os.path.relpath(os.path.realpath(path), ROOT)
    if rel != _DEFAULT_REL:
        return [f"{path}: no es el recibo versionado canónico ({_DEFAULT_REL})"]
    data, err = _governed_read_bytes(rel)
    if data is None:
        return [f"{path}: lectura gobernada falló ({err})"]
    try:
        d = json.loads(data.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
    except (UnicodeDecodeError, ValueError) as exc:
        return [f"{path}: JSON inválido/duplicado ({exc})"]
    return validate_receipt(d)


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
    print(f"✓ recibo B233 válido (derivado + gobernado): {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
