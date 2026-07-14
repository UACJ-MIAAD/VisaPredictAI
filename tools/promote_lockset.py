#!/usr/bin/env python
"""Promoción con ROLLBACK TRANSACCIONAL y DETECCIÓN DE MATRIZ PARCIAL (P0R.4R, ronda 10).

`make_locks.sh` resuelve los 9 locks en staging; este helper los promueve a `locks/`. NO es
atomicidad de bundle (son nueve renames sucesivos), sino:

  1. VALIDA todo el staging con el contrato estático único (tools/lock_contracts.py) antes del
     primer rename — staging inválido aborta sin tocar locks/;
  2. respalda bytes+existencia de los locks actuales;
  3. copia cada staged a un temporal dentro de locks/ y rename sobre el destino (fsync fichero);
  4. ante CUALQUIER excepción hace ROLLBACK: restaura previos, borra los que no existían; si el
     rollback TAMBIÉN falla, reporta ambas excepciones y deja una SEÑAL inequívoca de matriz
     inválida (elimina el manifiesto) para que el auditor bloquee;
  5. escribe `locks/lockset.json` por ÚLTIMO (fsync fichero + fsync directorio con fd cerrado);
  6. se autovalida con el contrato completo tras escribir el manifiesto.

Como el manifiesto es la última escritura y liga los hashes de locks + fuentes (incluidos los 3
scripts del contrato), una interrupción a mitad deja el árbol y el manifiesto DIVERGENTES: el
auditor recalcula ambos y BLOQUEA (detección de matriz parcial). Stdlib puro.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from tools import lock_contracts as lc

ROOT = lc.ROOT
LOCKS = ROOT / "locks"
MANIFEST = LOCKS / "lockset.json"
LOCK_NAMES = lc.LOCK_NAMES
SOURCES = lc.SOURCES


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _fsync_dir(path: Path) -> None:
    dfd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


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
    probs = lc.validate_staging(staged, ROOT)
    if probs:
        raise SystemExit("promote: staging inválido -> " + "; ".join(probs))
    LOCKS.mkdir(parents=True, exist_ok=True)
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
                Path(tmp).unlink(missing_ok=True)
                raise
            promoted.append(name)
        # manifiesto AL FINAL: liga hashes de locks + fuentes (incl. los 3 scripts del contrato).
        manifest = {
            "schema_version": 1,
            "generator": generator,
            "sources": {s: _sha256((ROOT / s).read_bytes()) for s in SOURCES},
            "locks": {
                f"locks/{n}": {
                    "sha256": _sha256((LOCKS / n).read_bytes()),
                    "pins": len(lc.pin_map((LOCKS / n).read_text())),
                }
                for n in LOCK_NAMES
            },
        }
        _atomic_write(MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        _fsync_dir(LOCKS)
        # autovalidación: el árbol promovido + el manifiesto recién escrito deben cumplir el contrato.
        post = lc.validate_all(ROOT, manifest=manifest)
        if post:
            raise RuntimeError("post-promoción incoherente con el contrato: " + "; ".join(post))
        return manifest
    except BaseException as promote_exc:
        try:
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
            _fsync_dir(LOCKS)
        except BaseException as rollback_exc:
            # rollback fallido: matriz posiblemente parcial. Elimina el manifiesto (señal inequívoca
            # de invalidez para el auditor) y reporta AMBAS excepciones.
            try:
                MANIFEST.unlink(missing_ok=True)
                _fsync_dir(LOCKS)
            except BaseException:
                pass
            raise RuntimeError(
                f"ROLLBACK FALLIDO tras {promote_exc!r}: {rollback_exc!r} — manifiesto eliminado, "
                f"MATRIZ POSIBLEMENTE INVÁLIDA en locks/, regenerar con `make lock`"
            ) from rollback_exc
        raise


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staged", required=True)
    ap.add_argument("--python", required=True, help="versión COMPLETA X.Y.Z")
    ap.add_argument("--platform", required=True, help="p. ej. 'Darwin arm64'")
    ap.add_argument("--pip", required=True)
    ap.add_argument("--setuptools", required=True)
    ap.add_argument("--wheel", required=True)
    ap.add_argument("--uv", required=True)
    ns = ap.parse_args(argv[1:])
    gen = {
        "python": ns.python,
        "platform": ns.platform,
        "pip": ns.pip,
        "setuptools": ns.setuptools,
        "wheel": ns.wheel,
        "uv": ns.uv,
    }
    m = promote(Path(ns.staged), gen)
    print(f"✓ lockset promovido: {len(m['locks'])} locks + manifest (locks/lockset.json), contrato OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
