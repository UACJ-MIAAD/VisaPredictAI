"""Hashes ESTABLES de contenido para el contrato de campana-transaccion (fundacion).

Stdlib-only (corre en `ante` Y en `ante_nf`, sin pandas/numpy) para que ambos productores
-local y deep- computen el MISMO hash sobre el MISMO contenido. Las funciones reciben
estructuras planas (iterables de tuplas), no DataFrames: el llamador extrae las columnas.

Estabilidad (auditoria 13-jul-2026 ronda 9): filas ORDENADAS + serializacion canonica
(unique_id crudo, fecha ISO, float.hex(y) exacto y round-trippable, con nan/inf explicitos).
Dos archivos con las MISMAS 600 filas logicas dan el MISMO hash aunque difieran en el orden
de las filas o en el formato del float; dos con y distinta dan hashes distintos.

Convencion de prefijos (segun el esquema del contrato): grid/truth/mask devuelven hex
crudo; los hashes de artefacto/panel llevan el prefijo ``sha256:``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

_US = "\x1f"  # separador de campo (no aparece en ids/fechas/hex-float)
_RS = "\x1e"  # separador de registro


def hexfloat(y: float) -> str:
    """``float.hex`` (exacto, round-trippable) con nan/inf canonicos y explicitos."""
    yf = float(y)
    if yf != yf:  # NaN
        return "nan"
    if yf == float("inf"):
        return "+inf"
    if yf == float("-inf"):
        return "-inf"
    return yf.hex()


def _digest(records: Iterable[str]) -> str:
    h = hashlib.sha256()
    for rec in sorted(records):
        h.update(rec.encode("utf-8"))
        h.update(_RS.encode("utf-8"))
    return h.hexdigest()


def grid_sha256(rows: Iterable[tuple[str, str]]) -> str:
    """Hash de la grilla (unique_id, ds_iso). Orden-independiente."""
    return _digest(f"{uid}{_US}{ds}" for uid, ds in rows)


def truth_sha256(rows: Iterable[tuple[str, str, float]]) -> str:
    """Hash de la verdad (unique_id, ds_iso, y). Detecta y distinta con la MISMA grilla."""
    return _digest(f"{uid}{_US}{ds}{_US}{hexfloat(y)}" for uid, ds, y in rows)


def finite_mask_sha256(rows: Iterable[tuple[str, str, bool]]) -> str:
    """Hash de la mascara finita (unique_id, ds_iso, is_finite) de UN modelo."""
    return _digest(f"{uid}{_US}{ds}{_US}{int(bool(f))}" for uid, ds, f in rows)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_tree_sha256(path: str | Path) -> str:
    """sha256 de un ARCHIVO o de un ARBOL de directorio (relpaths ordenados + bytes).

    Un modelo deep se guarda como directorio (``nf.save``); uno local como ``.pkl``. Para un
    directorio se hashea cada archivo con su ruta relativa POSIX, de modo estable e
    independiente del orden del walk. Devuelve ``sha256:<hex>``.
    """
    p = Path(path)
    if p.is_file():
        return "sha256:" + _file_sha256(p)
    h = hashlib.sha256()
    for f in sorted(q for q in p.rglob("*") if q.is_file()):
        h.update(f.relative_to(p).as_posix().encode("utf-8"))
        h.update(_US.encode("utf-8"))
        h.update(_file_sha256(f).encode("utf-8"))
        h.update(_RS.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def panel_sha256(path: str | Path) -> str:
    """sha256 del panel (bytes) con prefijo ``sha256:``; ``n/d`` si falta (fail-loud arriba)."""
    p = Path(path)
    return "sha256:" + _file_sha256(p) if p.exists() else "n/d"
