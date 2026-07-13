"""Contrato de cobertura por semilla del productor deep global (paso 3, ronda 9).

Corre en `ante_nf` (pandas, SIN vp_model/vp_data). Da al productor:

* una GRILLA CANONICA (las ultimas `holdout` filas por serie), NO 'las filas donde algun
  modelo tiene forecast' -> un modelo que fallo deja NaN en su columna, no borra la fila;
* escritura ATOMICA del CSV y del sidecar (tmp + ``os.replace``);
* un SIDECAR de cobertura por semilla (grid/truth/finite-mask sha256 + inventario de modelos)
  que el gate compara entre las 5 semillas: dos archivos con 600 filas distintas NO son
  equivalentes; una prediccion parcialmente NaN cambia la finite-mask -> fallo de cobertura.

Stdlib + pandas. Inserta la raiz del repo en sys.path para importar tools.campaign_hashing
tambien desde ante_nf (donde no hay instalacion editable del paquete).
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.campaign_hashing import finite_mask_sha256, grid_sha256, truth_sha256  # noqa: E402

SIDECAR_SCHEMA = 1


def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except TypeError, ValueError:
        return False


def _iso(d) -> str:
    return pd.Timestamp(d).date().isoformat()


def canonical_grid(level: pd.DataFrame, holdout: int) -> pd.DataFrame:
    """Las ULTIMAS `holdout` filas (unique_id, ds, y) por serie = grilla que TODO modelo cubre."""
    g = level.sort_values(["unique_id", "ds"]).groupby("unique_id", group_keys=False).tail(holdout)
    return g[["unique_id", "ds", "y"]].reset_index(drop=True)


def build_output(grid: pd.DataFrame, model_forecasts: dict[str, pd.DataFrame], required: list[str]) -> pd.DataFrame:
    """Grilla canonica + una columna por modelo REQUERIDO (left join; ausente/fallido -> NaN).

    Nunca elimina filas de la grilla: un modelo que fallo aparece como columna toda-NaN, lo que
    la finite-mask del sidecar registra como cobertura cero para ese modelo.
    """
    out = grid[["unique_id", "ds", "y"]].copy()
    for name in required:
        mf = model_forecasts.get(name)
        if mf is None or mf.empty:
            out[name] = float("nan")
            continue
        col = [c for c in mf.columns if c not in ("unique_id", "ds")][-1]
        m = mf[["unique_id", "ds", col]].rename(columns={col: name})
        out = out.merge(m, on=["unique_id", "ds"], how="left")
        if name not in out.columns:
            out[name] = float("nan")
    return out


def validate_output(out: pd.DataFrame, grid: pd.DataFrame, required: list[str]) -> list[str]:
    """Contenido correcto ANTES de renombrar: grilla intacta, sin duplicados, columnas presentes."""
    probs: list[str] = []
    gkey = list(zip(grid["unique_id"].astype(str), grid["ds"].map(_iso), strict=True))
    okey = list(zip(out["unique_id"].astype(str), out["ds"].map(_iso), strict=True))
    if len(okey) != len(set(okey)):
        probs.append("salida: filas (unique_id, ds) duplicadas")
    if set(okey) != set(gkey):
        probs.append("salida: la grilla no coincide con la canonica (filas de mas/menos)")
    missing = [m for m in required if m not in out.columns]
    if missing:
        probs.append(f"salida: faltan columnas de modelo {missing}")
    return probs


def coverage_sidecar(
    out: pd.DataFrame, required: list[str], *, campaign: dict, table: str, variant: str, seed: int
) -> dict:
    """Sidecar de cobertura por semilla (hashes de grilla/verdad/mascara + inventario)."""
    grid_rows = [(str(u), _iso(d)) for u, d in zip(out["unique_id"], out["ds"], strict=True)]
    truth_rows = [(str(u), _iso(d), float(y)) for u, d, y in zip(out["unique_id"], out["ds"], out["y"], strict=True)]
    models: dict[str, dict] = {}
    for m in required:
        col = out[m] if m in out.columns else pd.Series([float("nan")] * len(out))
        mask_rows = [(str(u), _iso(d), _finite(v)) for u, d, v in zip(out["unique_id"], out["ds"], col, strict=True)]
        models[m] = {
            "finite_rows": int(sum(1 for v in col if _finite(v))),
            "finite_mask_sha256": finite_mask_sha256(mask_rows),
        }
    return {
        "schema_version": SIDECAR_SCHEMA,
        "campaign_id": campaign.get("campaign_id"),
        "source_git_sha": campaign.get("source_git_sha"),
        "table": table,
        "variant": variant,
        "seed": int(seed),
        "grid_sha256": grid_sha256(grid_rows),
        "truth_sha256": truth_sha256(truth_rows),
        "n_rows": int(len(out)),
        "n_series": int(out["unique_id"].nunique()),
        "models": models,
    }


def _atomic_write(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".seed.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            write(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def finalize_seed(
    grid: pd.DataFrame,
    model_forecasts: dict[str, pd.DataFrame],
    required: list[str],
    *,
    out_path: Path,
    sidecar_path: Path,
    campaign: dict,
    table: str,
    variant: str,
    seed: int,
) -> dict:
    """Ensambla, VALIDA y promueve atomicamente el CSV + su sidecar. SystemExit si invalido."""
    out = build_output(grid, model_forecasts, required)
    problems = validate_output(out, grid, required)
    if problems:
        raise SystemExit(f"seed {table}/{variant}/s{seed}: salida invalida -> {problems}")
    sidecar = coverage_sidecar(out, required, campaign=campaign, table=table, variant=variant, seed=seed)
    _atomic_write(Path(out_path), lambda fh: out.to_csv(fh, index=False))
    _atomic_write(Path(sidecar_path), lambda fh: json.dump(sidecar, fh, ensure_ascii=False, indent=2, sort_keys=True))
    return sidecar
