"""Integrity gate for the archived benchmark; never recompute or accredit new metrics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "reports/release/release_manifest.json"
ARTIFACTS = (
    "reports/governance/champion_challenger.json",
    "reports/governance/champion_manifest.json",
    "reports/governance/e5_facts.json",
    "reports/eval/series_cohorts.json",
    "reports/prospective/pi_scale_by_h.json",
)
MAX_BYTES = 10 * 1024 * 1024


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "--no-replace-objects", *args], cwd=root, stderr=subprocess.PIPE)


def _blob(root: Path, relative: str) -> bytes:
    entries = _git(root, "ls-tree", "-z", "HEAD", "--", relative).split(b"\0")
    entries = [entry for entry in entries if entry]
    if len(entries) != 1:
        raise ValueError(f"{relative}: archivo commiteado ausente o ambiguo")
    metadata, name = entries[0].split(b"\t", 1)
    mode, kind, oid = metadata.split()
    if mode != b"100644" or kind != b"blob" or name.decode() != relative:
        raise ValueError(f"{relative}: se exige blob regular 100644")
    if not re.fullmatch(rb"[0-9a-f]{40,64}", oid):
        raise ValueError("oid inválido")
    object_id = oid.decode()
    size = int(_git(root, "cat-file", "-s", object_id))
    if not 0 < size <= MAX_BYTES:
        raise ValueError(f"{relative}: tamaño fuera de cota")
    raw = _git(root, "cat-file", "blob", object_id)
    if len(raw) != size:
        raise ValueError(f"{relative}: lectura incompleta")
    return raw


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"llave duplicada: {key}")
        result[key] = value
    return result


def check(root: Path = ROOT) -> list[str]:
    try:
        manifest = json.loads(_blob(root, MANIFEST), object_pairs_hook=_unique)
        for relative in ARTIFACTS:
            matching = [item for item in manifest["artifacts"] if item.get("path") == relative]
            if len(matching) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matching[0].get("sha256", "")):
                raise ValueError(f"{relative}: hash previo ausente o ambiguo")
            committed = _blob(root, relative)
            if hashlib.sha256(committed).hexdigest() != matching[0]["sha256"]:
                raise ValueError(f"{relative}: el benchmark commiteado no coincide con el manifiesto previo")
            path = root / relative
            snapshot = path.lstat()
            if not stat.S_ISREG(snapshot.st_mode) or not 0 < snapshot.st_size <= MAX_BYTES:
                raise ValueError(f"{relative}: archivo de trabajo no regular o fuera de cota")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_ino != snapshot.st_ino
                    or before.st_dev != snapshot.st_dev
                ):
                    raise ValueError(f"{relative}: sustitución durante la lectura")
                raw = stream.read(MAX_BYTES + 1)
                after = os.fstat(stream.fileno())
                if len(raw) > MAX_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError(f"{relative}: lectura fuera de cota o mutación concurrente")
            if raw != committed:
                raise ValueError(f"{relative}: el mensual no puede cambiar el benchmark archivado")
        return []
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        return [str(exc)]


def main() -> int:
    problems = check()
    if problems:
        print("Benchmark archivado rechazado: " + "; ".join(problems))
        return 1
    print("Benchmark archivado idéntico al manifiesto commiteado; sin recalcular ni acreditar métricas nuevas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
