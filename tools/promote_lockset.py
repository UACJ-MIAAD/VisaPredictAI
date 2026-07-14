#!/usr/bin/env python
"""Promoción CRASH-DETECTABLE de la matriz de 9 locks (P0R.4, ronda 10).

`make_locks.sh` resuelve los 9 locks en un directorio de staging; este helper los promueve a
`locks/` de forma que una interrupción (kill -9, corte) NUNCA deje una matriz mezclada aceptada:

  1. exige EXACTAMENTE los 9 locks esperados en staging, no vacíos y con pins;
  2. respalda en memoria bytes+existencia de los locks actuales;
  3. copia cada staged a un temporal DENTRO de locks/, luego rename sobre el destino;
  4. ante CUALQUIER excepción restaura todos los anteriores y borra los que no existían;
  5. escribe `locks/lockset.json` (hashes de fuentes + 9 locks) por ULTIMO, con rename atómico.

Como el manifest se escribe al final, un kill -9 a mitad deja hashes que NO coinciden con el
manifest anterior -> el runner de auditoría detecta la promoción parcial y bloquea. Stdlib puro.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCKS = ROOT / "locks"
MANIFEST = LOCKS / "lockset.json"
LOCK_NAMES = (
    "runtime.txt",
    "dev.txt",
    "model-cpu.txt",
    "runtime-linux-x86_64.txt",
    "dev-linux-x86_64.txt",
    "model-cpu-linux-x86_64.txt",
    "deep-macos-arm64.txt",
    "deep-linux-x86_64-cpu.txt",
    "deep-linux-x86_64-cu126.txt",
)
SOURCES = (
    "pyproject.toml",
    "requirements/deep.in",
    "requirements/deep-linux-cpu.in",
    "requirements/deep-linux-cu126.in",
)
_PIN = re.compile(r"^[A-Za-z0-9_.-]+==")


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _pins(text: str) -> int:
    return sum(1 for ln in text.splitlines() if _PIN.match(ln.strip()))


def _validate_staged(staged: Path) -> list[str]:
    present = {p.name for p in staged.glob("*.txt")}
    probs: list[str] = []
    if present != set(LOCK_NAMES):
        probs.append(f"staging: {sorted(present)} != los 9 esperados {sorted(LOCK_NAMES)}")
        return probs
    for name in LOCK_NAMES:
        text = (staged / name).read_text()
        if _pins(text) < 1:
            probs.append(f"staging {name}: 0 pins (lock vacío)")
        if "/var/folders" in text or "/tmp/" in text or str(staged) in text:
            probs.append(f"staging {name}: contiene ruta temporal de staging")
    return probs


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lockset.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def promote(staged: Path, generator: dict) -> dict:
    probs = _validate_staged(staged)
    if probs:
        raise SystemExit("promote: staging inválido -> " + "; ".join(probs))
    LOCKS.mkdir(parents=True, exist_ok=True)
    # respaldo: bytes de los locks actuales (o None si no existían)
    backup: dict[str, bytes | None] = {
        n: (LOCKS / n).read_bytes() if (LOCKS / n).exists() else None for n in LOCK_NAMES
    }
    old_manifest = MANIFEST.read_bytes() if MANIFEST.exists() else None
    promoted: list[str] = []
    try:
        for name in LOCK_NAMES:
            data = (staged / name).read_bytes()
            fd, tmp = tempfile.mkstemp(dir=str(LOCKS), prefix=f".{name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, LOCKS / name)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)  # limpia el .tmp en vuelo antes del rollback
                raise
            promoted.append(name)
        # manifest AL FINAL (última escritura). Un kill -9 antes deja hashes != manifest viejo.
        manifest = {
            "schema_version": 1,
            "generator": generator,
            "sources": {s: _sha256((ROOT / s).read_bytes()) for s in SOURCES},
            "locks": {
                f"locks/{n}": {"sha256": _sha256((LOCKS / n).read_bytes()), "pins": _pins((LOCKS / n).read_text())}
                for n in LOCK_NAMES
            },
        }
        _atomic_write(MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        os.fsync(os.open(str(LOCKS), os.O_RDONLY))
        return manifest
    except BaseException:
        # rollback: restaura cada lock a su estado previo (o lo borra si no existía)
        for name in promoted:
            prev = backup[name]
            if prev is None:
                (LOCKS / name).unlink(missing_ok=True)
            else:
                _atomic_write(LOCKS / name, prev)
        if old_manifest is None:
            MANIFEST.unlink(missing_ok=True)
        else:
            _atomic_write(MANIFEST, old_manifest)
        raise


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staged", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--pip", required=True)
    ap.add_argument("--setuptools", required=True)
    ap.add_argument("--wheel", required=True)
    ap.add_argument("--uv", required=True)
    ns = ap.parse_args(argv[1:])
    gen = {"python": ns.python, "pip": ns.pip, "setuptools": ns.setuptools, "wheel": ns.wheel, "uv": ns.uv}
    m = promote(Path(ns.staged), gen)
    print(f"✓ lockset promovido: {len(m['locks'])} locks + manifest (locks/lockset.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
