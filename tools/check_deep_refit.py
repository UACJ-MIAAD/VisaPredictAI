#!/usr/bin/env python
"""¿Los 5 refits deep FAD camp_auto están COMPLETOS y CONSISTENTES? (P0R.5 · R9.4/B66/B74/B81/B86/B91 —
extraído del heredoc de run_campaign_aq_tail.sh). Se invoca desde ROOT (run-command fija cwd=root).

GOBERNANZA DE RUTAS (B91): la cadena `.` → `reports` → `campaign` se abre COMPONENTE A COMPONENTE con `openat`
`O_DIRECTORY|O_NOFOLLOW` (un ancestro symlink — evidencia EXTERNA al árbol gobernado — devuelve 1) y cada nivel
exige directorio real, del UID actual y sin escritura de grupo/otros. El descriptor de campaign queda ABIERTO
durante las CINCO lecturas: cada semilla se abre RELATIVA a él con `O_NOFOLLOW`, se valida por `fstat` del
descriptor (regular/UID/nlink==1) y se entrega a pandas como file object del MISMO descriptor — sin ventana
check→reopen, y un swap de la ruta a mitad de las lecturas no afecta al descriptor ya validado.

Exit 0 solo si las CINCO semillas s1…s5 cumplen, ANTE evidencia posiblemente alterada: columnas
`unique_id`/`ds`/`y`/`AutoBiTCN` (formato ancho de NeuralForecast), `unique_id` presente y no vacío (B86:
`isna()` se evalúa ANTES de `astype(str)` — NaN/None/celda vacía NO pasan como el string "nan"; whitespace
también bloquea), `ds` parseable, `y` y `AutoBiTCN` numéricos y finitos, `(unique_id, ds)` ÚNICO dentro de cada
semilla, y el conjunto ORDENADO de `(unique_id, ds, y)` IDÉNTICO (mismo número de filas y mismas claves) entre
las cinco. Exit 1 ante cualquier ausencia/inconsistencia (⇒ el runbook re-corre los 5 refits). Sin efectos
secundarios.

B74/B81: el heredoc original solo miraba `s1` y una columna `model` inexistente; una versión intermedia usaba
`set()` sobre las claves, perdiendo la MULTIPLICIDAD (una semilla con filas duplicadas pasaba). Aquí se compara
por DataFrame ORDENADO y se exige unicidad. (El contrato explícito de elegibilidad 580/600 vive en P2b.)"""

from __future__ import annotations

import os
import stat
import sys

import numpy as np
import pandas as pd

_SEEDS = (1, 2, 3, 4, 5)
_REQUIRED_COLS = ("unique_id", "ds", "y", "AutoBiTCN")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_dir_at(parent_fd: int | None, name: str) -> int:
    """B91: un componente de la cadena gobernada — symlink (sano o roto) revienta en el open; el fstat del
    DESCRIPTOR exige dir real, del UID actual y sin escritura de grupo/otros."""
    fd = os.open(name, _DIR_FLAGS) if parent_fd is None else os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or (stat.S_IMODE(st.st_mode) & 0o022):
        os.close(fd)
        raise OSError(f"directorio gobernado {name!r} ajeno o escribible por grupo/otros")
    return fd


def _open_campaign_chain() -> list[int]:
    """Abre `.` → reports → campaign y devuelve los TRES descriptores (el llamador los cierra). OSError si
    cualquier componente es symlink/ausente/ajeno — evidencia fuera del árbol gobernado NO se certifica."""
    fds: list[int] = []
    try:
        fds.append(_open_dir_at(None, "."))
        fds.append(_open_dir_at(fds[0], "reports"))
        fds.append(_open_dir_at(fds[1], "campaign"))
    except BaseException:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    return fds


def _seed_keys_at(camp_fd: int, fname: str) -> pd.DataFrame | None:
    """Lee y valida UNA semilla RELATIVA al descriptor de campaign (B91: openat O_NOFOLLOW + fstat del fd +
    file object del MISMO descriptor; jamás check-then-reopen por ruta)."""
    try:
        fd = os.open(fname, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=camp_fd)
    except OSError:
        return None  # ausente o symlink
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
        os.close(fd)
        return None  # no-regular/ajena/hardlink
    with os.fdopen(fd, "rb") as fh:
        df = pd.read_csv(fh)
    if df.empty or not set(_REQUIRED_COLS) <= set(df.columns):
        return None
    # B86: isna ANTES de astype — NaN/None/celda vacía se leen como NaN y astype(str) los
    # enmascararía como el string "nan" (que NO es vacío). Después, vacío/whitespace también bloquean.
    if df["unique_id"].isna().any():
        return None  # unique_id ausente (None/NaN/celda vacía)
    if df["unique_id"].astype(str).str.strip().eq("").any():
        return None  # unique_id en blanco/whitespace
    ds = pd.to_datetime(df["ds"], errors="coerce")
    if ds.isna().any():
        return None  # ds no parseable
    for col in ("y", "AutoBiTCN"):
        v = pd.to_numeric(df[col], errors="coerce")
        if v.isna().any() or not np.isfinite(v).all():
            return None  # no numérico/no finito
    keys = df[["unique_id", "ds", "y"]].copy()
    keys["ds"] = ds
    if keys[["unique_id", "ds"]].duplicated().any():
        return None  # (unique_id, ds) NO único dentro de la semilla
    return keys.sort_values(["unique_id", "ds"]).reset_index(drop=True)


def main() -> int:
    try:
        fds = _open_campaign_chain()
    except OSError:
        return 1  # cadena no gobernada (symlink/ausente/ajena) — no se certifica evidencia externa
    camp_fd = fds[-1]
    try:
        ref: pd.DataFrame | None = None
        for s in _SEEDS:
            keys = _seed_keys_at(camp_fd, f"global_FAD_camp_auto_s{s}.csv")
            if keys is None:
                return 1  # semilla ausente/incompleta/incoherente
            if ref is None:
                ref = keys
            elif not keys.equals(ref):  # mismo número de filas Y mismas (unique_id, ds, y) ORDENADAS
                return 1
        return 0
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
