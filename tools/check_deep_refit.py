#!/usr/bin/env python
"""¿Los 5 refits deep FAD camp_auto están COMPLETOS y CONSISTENTES? (P0R.5 · R9.4/B66/B74/B81/B86/B91/B93/B95 —
extraído del heredoc de run_campaign_aq_tail.sh). Se invoca desde ROOT (run-command fija cwd=root).

GOBERNANZA DE RUTAS (B91/B93): la cadena `.` → `reports` → `campaign` se abre COMPONENTE A COMPONENTE con
`openat` `O_DIRECTORY|O_NOFOLLOW` (un ancestro symlink — evidencia EXTERNA al árbol gobernado — devuelve 1) y
cada nivel exige directorio real, del UID actual y sin escritura de grupo/otros. Los tres descriptores quedan
ABIERTOS toda la verificación; la IDENTIDAD (st_dev/st_ino) de la cadena se REVERIFICA re-caminándola fresca
desde cwd tras cada lectura y ANTES del `return 0` — un swap de la ruta oficial a mitad de las lecturas (aunque
el descriptor original evite leer evidencia externa) NO certifica (B93). Cada semilla se lee vía
`governed_read.read_governed_csv` RELATIVA al descriptor de campaign: snapshot `fstat` pre/post exacto
(regular/UID/nlink==1/**sin escritura de grupo-otros**/dev·ino·size·mtime·ctime estables) y pandas del MISMO
descriptor — un CSV escribible por terceros o mutado durante la lectura se rechaza (B95).

Exit 0 solo si las CINCO semillas s1…s5 cumplen: columnas `unique_id`/`ds`/`y`/`AutoBiTCN` (formato ancho de
NeuralForecast), `unique_id` presente y no vacío (B86: `isna()` ANTES de `astype(str)`; whitespace también
bloquea), `ds` parseable, `y` y `AutoBiTCN` numéricos y finitos, `(unique_id, ds)` ÚNICO dentro de cada
semilla, el conjunto ORDENADO de `(unique_id, ds, y)` IDÉNTICO entre las cinco, Y la cadena gobernada con la
misma identidad de principio a fin. Exit 1 ante cualquier ausencia/inconsistencia/swap (⇒ el runbook re-corre
los 5 refits). Sin efectos secundarios.

B74/B81: el heredoc original solo miraba `s1` y una columna `model` inexistente; una versión intermedia usaba
`set()` sobre las claves, perdiendo la MULTIPLICIDAD (una semilla con filas duplicadas pasaba). Aquí se compara
por DataFrame ORDENADO y se exige unicidad. (El contrato explícito de elegibilidad 580/600 vive en P2b.)"""

from __future__ import annotations

import os
import stat
import sys

import numpy as np
import pandas as pd

from tools.governed_read import read_governed_csv

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


class _Chain:
    """Cadena gobernada `.` → reports → campaign. `reverify()` re-camina fresca desde cwd y exige la MISMA
    identidad (st_dev, st_ino) por nivel — un swap de ancestro tras validar devuelve False (B93)."""

    def __init__(self) -> None:
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
        self.dot, self.reports, self.camp = fds

    def _fds(self) -> tuple[int, int, int]:
        return (self.dot, self.reports, self.camp)

    def idents(self) -> list[tuple[int, int]]:
        return [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in self._fds()]

    def reverify(self) -> bool:
        try:
            fresh = _Chain()
        except OSError:
            return False  # la cadena oficial ya no es gobernable (symlink/ausente)
        try:
            return fresh.idents() == self.idents()
        finally:
            fresh.close()

    def close(self) -> None:
        for fd in self._fds():
            try:
                os.close(fd)
            except OSError:
                pass


def _seed_keys_at(camp_fd: int, fname: str) -> pd.DataFrame | None:
    """Lee y valida UNA semilla RELATIVA al descriptor de campaign vía el lector gobernado (B91/B95: openat
    O_NOFOLLOW + snapshot fstat pre/post + no escribible por terceros; jamás check-then-reopen por ruta)."""
    df, err = read_governed_csv(camp_fd, fname)
    if err is not None:
        return None  # ausente/symlink/ajena/hardlink/escribible/mutada
    assert df is not None
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
        chain = _Chain()
    except OSError:
        return 1  # cadena no gobernada (symlink/ausente/ajena) — no se certifica evidencia externa
    try:
        ref: pd.DataFrame | None = None
        for s in _SEEDS:
            keys = _seed_keys_at(chain.camp, f"global_FAD_camp_auto_s{s}.csv")
            if keys is None:
                return 1  # semilla ausente/incompleta/incoherente
            if not chain.reverify():
                return 1  # B93: la ruta oficial dejó de apuntar a la evidencia validada (swap)
            if ref is None:
                ref = keys
            elif not keys.equals(ref):  # mismo número de filas Y mismas (unique_id, ds, y) ORDENADAS
                return 1
        if not chain.reverify():
            return 1  # B93: reverificación final inmediatamente antes de certificar
        return 0
    finally:
        chain.close()


if __name__ == "__main__":
    sys.exit(main())
