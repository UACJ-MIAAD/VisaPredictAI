"""Contrato de cobertura por semilla del productor deep global (paso 3, ronda 9).

Corre en `ante_nf` (pandas, SIN vp_model/vp_data). Da al productor:

* una GRILLA CANONICA (las ultimas `holdout` filas por serie), NO 'las filas donde algun
  modelo tiene forecast' -> un modelo que fallo deja NaN en su columna, no borra la fila;
* escritura ATOMICA del CSV y del sidecar (tmp + ``os.replace``);
* un SIDECAR de cobertura por semilla (grid/truth/finite-mask sha256 + inventario de modelos);
* ★ M74-E-R7 · `seed_problems` y `accredit_group`: UNA regla de cobertura exacta contra la rejilla
  del panel, para el productor antes de escribir y para el lector antes de evaluar. Comparar las
  semillas ENTRE SI no bastaba: cinco copias truncadas igual son identicas entre si.

Stdlib + pandas. El importador (run_global_deep en ante_nf) debe poner la raiz del repo en
sys.path ANTES de ``import seed_coverage`` para resolver ``tools.campaign_hashing`` (en `ante`
y en los tests ya esta en el path). Ver el wiring en experiments/run_global_deep.py.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import tempfile
from pathlib import Path

import pandas as pd

from tools.campaign_hashing import finite_mask_sha256, grid_sha256, truth_sha256

SIDECAR_SCHEMA = 1


def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except TypeError, ValueError:
        return False


def _iso(d) -> str:
    return pd.Timestamp(d).date().isoformat()


def canonical_grid(level: pd.DataFrame, holdout: int) -> pd.DataFrame:
    """Las ULTIMAS `holdout` filas (unique_id, ds, y) por serie = grilla que cada modelo cubre."""
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


def seed_problems(out: pd.DataFrame, grid: pd.DataFrame, required: list[str]) -> list[str]:
    """Cobertura EXACTA de una semilla, medida sobre el marco y contra la rejilla canonica.

    Cada clave ``(unique_id, ds)`` de la rejilla exactamente una vez, la verdad del panel en cada
    una y un pronostico finito de cada modelo declarado. Ni una columna de mas ni de menos.
    """
    columnas = sorted(["unique_id", "ds", "y", *required])
    if sorted(map(str, out.columns)) != columnas:
        return [f"columnas {sorted(map(str, out.columns))} != {columnas}"]
    verdad = {(str(u), _iso(d)): float(y) for u, d, y in zip(grid["unique_id"], grid["ds"], grid["y"], strict=True)}
    claves = list(zip(out["unique_id"].astype(str), out["ds"].map(_iso), strict=True))
    probs: list[str] = []
    if len(claves) != len(set(claves)):
        probs.append(f"{len(claves) - len(set(claves))} clave(s) (unique_id, ds) duplicada(s)")
    faltan, sobran = set(verdad) - set(claves), set(claves) - set(verdad)
    if faltan or sobran:
        probs.append(f"rejilla: faltan {len(faltan)} y sobran {len(sobran)} de las {len(verdad)} claves del panel")
    distintas = sum(
        1 for k, y in zip(claves, out["y"], strict=True) if k in verdad and not (_finite(y) and float(y) == verdad[k])
    )
    if distintas:
        probs.append(f"{distintas} fila(s) con y distinta de la verdad del panel")
    for m in required:
        malas = sum(1 for v in out[m] if not _finite(v))
        if malas:
            probs.append(f"{m}: {malas} pronostico(s) no finito(s)")
    return probs


def coverage_sidecar(
    out: pd.DataFrame, required: list[str], *, campaign: dict, table: str, variant: str, seed: int, csv_sha256: str
) -> dict:
    """Sidecar de cobertura por semilla (hashes de grilla/verdad/mascara + CSV + inventario).

    ``csv_sha256`` liga el sidecar al CSV por BYTES: el gate lo recalcula del archivo y no confia
    en el sidecar como fuente de verdad (un CSV alterado despues del sidecar no coincide)."""
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
        "csv_sha256": csv_sha256,
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
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
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
    problems = seed_problems(out, grid, required)
    if problems:
        raise SystemExit(f"seed {table}/{variant}/s{seed}: salida invalida -> {problems}")
    # hashea los BYTES EXACTOS que se escriben (el CSV se escribe desde este mismo texto).
    csv_text = out.to_csv(index=False)
    csv_sha256 = "sha256:" + hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    sidecar = coverage_sidecar(
        out, required, campaign=campaign, table=table, variant=variant, seed=seed, csv_sha256=csv_sha256
    )
    _atomic_write(Path(out_path), lambda fh: fh.write(csv_text))
    _atomic_write(Path(sidecar_path), lambda fh: json.dump(sidecar, fh, ensure_ascii=False, indent=2, sort_keys=True))
    return sidecar


def accredit_group(
    table: str,
    variant: str,
    camp_dir: Path,
    *,
    grid: pd.DataFrame,
    required: list[str],
    campaign_id: str,
    source_git_sha: str,
    n_seeds: int = 5,
) -> dict[str, pd.DataFrame]:
    """★ M74-E-R7 · la puerta UNICA del grupo s1..sN, antes de que nadie lo evalue.

    Aplica `seed_problems` contra la rejilla que el LLAMADOR derivo del panel y exige que el sidecar
    sea EXACTAMENTE el que ese CSV produce: el sidecar es un compromiso que se comprueba, nunca la
    fuente. Sustituye a `validate_seed_group`, que solo comparaba semillas entre si.

    ★ M74-E-R8 (auditoria `e6c76896…`): cada CSV se lee UNA vez; se hashean y se parsean esos mismos
    bytes, y la puerta DEVUELVE los marcos acreditados por variante. Leer la ruta para validar y
    otra vez para hashear permitia acreditar A, sellar el hash de B y que el evaluador consumiera B.
    """
    from vp_model.artifact_receipt import ReceiptError, loads_strict

    camp = Path(camp_dir)
    esperados = {f"global_{table}_{variant}_s{i}.csv" for i in range(1, n_seeds + 1)}
    presentes = {q.name for q in camp.glob(f"global_{table}_{variant}_s*.csv")}
    problemas = [] if presentes == esperados else [f"grupo {sorted(presentes)} != {sorted(esperados)}"]
    identidad = {"campaign_id": campaign_id, "source_git_sha": source_git_sha}
    acreditados: dict[str, pd.DataFrame] = {}
    for i in range(1, n_seeds + 1):
        csv, side = camp / f"global_{table}_{variant}_s{i}.csv", camp / f"coverage_{table}_{variant}_s{i}.json"
        if not csv.is_file() or not side.is_file():
            problemas.append(f"s{i}: sin CSV o sin sidecar")
            continue
        try:
            crudo = csv.read_bytes()
            out = pd.read_csv(io.BytesIO(crudo), parse_dates=["ds"])
            propios = seed_problems(out, grid, required)
            declarado = loads_strict(side.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, ReceiptError) as exc:
            problemas.append(f"s{i}: ilegible ({exc})")
            continue
        if propios:
            problemas += [f"s{i}: {p}" for p in propios]
            continue
        real = "sha256:" + hashlib.sha256(crudo).hexdigest()
        medido = coverage_sidecar(
            out, required, campaign=identidad, table=table, variant=variant, seed=i, csv_sha256=real
        )
        distintos = sorted(k for k in medido.keys() | declarado.keys() if medido.get(k) != declarado.get(k))
        if distintos:
            problemas.append(f"s{i}: el sidecar no es el que produce su CSV en {distintos}")
            continue
        acreditados[f"{variant}_s{i}"] = out
    if problemas:
        raise SystemExit(f"semillas {table}/{variant}: grupo NO acreditado — " + " · ".join(problemas))
    return acreditados
