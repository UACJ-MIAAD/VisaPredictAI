"""Ausencia declarada del hermano documental; un hermano presente nunca se oculta."""

import os
from pathlib import Path

import pytest


def _absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def read_documental(path: Path, documental_root: Path) -> str:
    if _absent(documental_root) and _absent(path) and os.environ.get("VP_ALLOW_MISSING_LATEX") == "1":
        pytest.skip("documental ausente, declarado con VP_ALLOW_MISSING_LATEX=1", allow_module_level=True)
    return path.read_text(encoding="utf-8")
