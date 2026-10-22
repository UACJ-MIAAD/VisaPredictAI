"""Read the immutable inputs at an archived artifact's last committed derivation.

Used for historical reproducibility tests, never to certify current-vintage quality.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "--no-replace-objects", *args], cwd=root, stderr=subprocess.PIPE)


def _blob(root: Path, revision: str, relative: str) -> bytes:
    entries = [entry for entry in _git(root, "ls-tree", "-z", revision, "--", relative).split(b"\0") if entry]
    assert len(entries) == 1, f"missing archived input: {relative}"
    metadata, name = entries[0].split(b"\t", 1)
    mode, kind, oid = metadata.split()
    assert mode == b"100644" and kind == b"blob" and name.decode() == relative, relative
    size = int(_git(root, "cat-file", "-s", oid.decode()))
    assert 0 < size <= 16 * 1024 * 1024, relative
    raw = _git(root, "cat-file", "blob", oid.decode())
    assert len(raw) == size, relative
    return raw


def archived_inputs(root: Path, artifact: str, inputs: tuple[str, ...]) -> tuple[str, dict[str, bytes]]:
    path = root / artifact
    assert path.is_file() and not path.is_symlink(), artifact
    assert path.read_bytes() == _blob(root, "HEAD", artifact), "uncommitted archived artifact"
    revision = _git(root, "log", "-1", "--format=%H", "HEAD", "--", artifact).decode().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", revision), "no committed derivation epoch"
    return revision, {relative: _blob(root, revision, relative) for relative in inputs}
