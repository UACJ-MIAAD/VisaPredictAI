"""Ledger v2 — identidad de freeze y contrato append-only compartido (A2, plan auditoría 2026-07-11).

Los dos ledgers prospectivos (``reports/prospective/forecast_log.csv`` del campeón y
``forecast_log_shadow.csv`` del retador) comparten desde aquí:

- la **identidad de freeze** por fila: ``forecast_id`` (determinista), ``frozen_at`` (UTC),
  ``freeze_panel_vintage`` (último boletín ingerido al congelar), ``panel_hash`` (md5 12-hex
  del panel, misma convención que ``build_model_card._panel_hash``), ``git_sha`` y
  ``model_version`` (receta desplegada/sombreada);
- el **modo de evaluación** por fila (``evaluation_mode``): ``live`` solo cuando el mes
  objetivo es POSTERIOR al vintage del panel al congelar (el boletín objetivo no existía
  aún en el conjunto de información); ``backfill`` en cualquier otro caso, incluido todo
  run explícito ``as_of``. Esta es la regla A1/D1: solo P6 (prospectivo real) autoriza
  claims de servicio en tiempo real, y una fila lo es únicamente si su target era
  desconocido al momento del freeze.
- el **append idempotente** ``keep="first"`` por (origin, serie, fecha): una fila congelada
  JAMÁS se reescribe (contrato C3); re-runs de la misma añada son no-op y una receta nueva
  sobre una añada ya congelada NO colisiona (no reemplaza ni mezcla — estrena en la añada
  siguiente).

``deployment_id`` (B1, migración aditiva): el ``release_id`` del manifiesto de release
vigente AL MOMENTO del freeze (``reports/release/release_manifest.json``) — identifica
bajo qué corte publicado corrió el congelador. Columna opcional: las filas anteriores a
B1 no la llevan y ``validate`` no la exige; las nuevas la estampan siempre.

Migración de las filas históricas: ``experiments/migrate_ledger_v2.py`` deriva sus actas
de nacimiento del ``git log`` (primer commit que contiene cada fila).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PANEL_CSV = ROOT / "data" / "processed" / "visa_panel_long.csv"

KEYS = ["origin", "country", "category", "table", "date"]
V2_COLS = [
    "forecast_id",
    "frozen_at",
    "freeze_panel_vintage",
    "panel_hash",
    "git_sha",
    "model_version",
    "evaluation_mode",
]


def git_sha() -> str:
    """HEAD corto (12 hex) con sufijo ``-dirty`` si el árbol tiene cambios; ``n/d`` sin git."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"], text=True, stderr=subprocess.DEVNULL, cwd=ROOT
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL, cwd=ROOT
        ).strip()
    except subprocess.CalledProcessError, FileNotFoundError:
        return "n/d"
    return f"{sha}-dirty" if dirty else sha


def panel_hash(path: Path = PANEL_CSV) -> str:
    """md5 12-hex del panel (misma convención que ``build_model_card._panel_hash``)."""
    return hashlib.md5(path.read_bytes()).hexdigest()[:12] if path.exists() else "n/d"


def panel_vintage(path: Path = PANEL_CSV) -> str:
    """Último mes de boletín ingerido (``YYYY-MM``) — la frontera de información del freeze."""
    if not path.exists():
        return "n/d"
    dates = pd.read_csv(path, usecols=["bulletin_date"])["bulletin_date"]
    return str(dates.max())[:7]


def current_release_id() -> str:
    """``release_id`` del manifiesto de release vigente (B1); ``n/d`` si aún no existe."""
    p = ROOT / "reports" / "release" / "release_manifest.json"
    if not p.exists():
        return "n/d"
    try:
        return str(json.loads(p.read_text()).get("release_id", "n/d"))
    except json.JSONDecodeError:
        return "n/d"


def forecast_id(row: dict) -> str:
    """Identificador determinista de la fila: sha1 12-hex de clave + receta.

    Determinista a propósito (no UUID): re-derivar el id de una fila congelada debe dar
    el mismo valor — es la base de la verificación anti-manipulación de ``validate``.
    """
    key = "|".join(str(row.get(k, "")) for k in ("origin", "country", "category", "table", "date", "model_version"))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def stamp_rows(
    rows: list[dict],
    model_version: str | dict[str, str] | None,
    *,
    as_of: str | None = None,
    frozen_at: str | None = None,
    vintage: str | None = None,
    phash: str | None = None,
    sha: str | None = None,
    deployment: str | None = None,
) -> list[dict]:
    """Sella la identidad de freeze v2 en cada fila (solo si aún no la trae).

    ``model_version``: str única, dict tabla→receta, o ``None`` para tomar la columna
    ``recipe`` de cada fila (ledger sombra). Los overrides (``frozen_at``/``vintage``/
    ``phash``/``sha``) existen para la migración histórica y los tests; producción los
    deja en ``None`` y se derivan del estado real al momento del freeze.
    """
    vintage = vintage or panel_vintage()
    phash = phash or panel_hash()
    sha = sha or git_sha()
    deployment = deployment or current_release_id()
    from vp_data.tracking import pipeline_run_id  # C3: misma resolución que el tracking

    run_id = pipeline_run_id()
    ts = frozen_at or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    out = []
    for r in rows:
        if r.get("forecast_id"):  # ya sellada (p.ej. re-append de filas migradas) — no tocar
            out.append(r)
            continue
        if isinstance(model_version, dict):
            mv = model_version.get(str(r.get("table")), "n/d")
        elif model_version is None:
            mv = str(r.get("recipe", "n/d"))
        else:
            mv = model_version
        mode = "backfill" if as_of else ("live" if str(r.get("date", ""))[:7] > vintage else "backfill")
        rr = {
            **r,
            "frozen_at": ts,
            "freeze_panel_vintage": vintage,
            "panel_hash": phash,
            "git_sha": sha,
            "model_version": mv,
            "evaluation_mode": mode,
            "deployment_id": deployment,
            "pipeline_run_id": run_id,
        }
        rr["forecast_id"] = forecast_id(rr)
        out.append(rr)
    return out


def append(path: Path, rows: list[dict], cols: list[str] | None = None) -> pd.DataFrame:
    """Append idempotente al ledger: ``keep="first"`` por ``KEYS`` (contrato C3).

    Una fila ya congelada nunca se reescribe; repetir la misma añada (reintento del cron,
    receta nueva, otro ``frozen_at``) es un no-op para las claves existentes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows)
    if cols:
        new = new[cols]
    combined = pd.concat([pd.read_csv(path), new], ignore_index=True) if path.exists() else new
    combined = combined.drop_duplicates(subset=KEYS, keep="first").sort_values(KEYS)
    combined.to_csv(path, index=False)
    return combined


def validate(df: pd.DataFrame) -> list[str]:
    """Contrato del ledger v2 — regresa violaciones (lista vacía = OK).

    Guardias: clave única; columnas v2 presentes; ninguna fila ``live`` con un target que
    ya estaba publicado a su ``freeze_panel_vintage`` (manipulación temporal); y todo
    ``forecast_id`` debe re-derivarse de su propia fila (manipulación de contenido).
    """
    v: list[str] = []
    if df.duplicated(subset=KEYS).any():
        v.append("filas duplicadas en la clave (origin, serie, fecha)")
    missing = [c for c in V2_COLS if c not in df.columns]
    if missing:
        v.append(f"faltan columnas v2: {missing}")
        return v
    stamped = df[df["frozen_at"].notna()]
    live = stamped[stamped["evaluation_mode"] == "live"]
    bad = live[live["date"].astype(str).str[:7] <= live["freeze_panel_vintage"].astype(str)]
    if len(bad):
        v.append(f"{len(bad)} filas 'live' cuyo target ya estaba publicado a su freeze_panel_vintage")
    ids = stamped.apply(lambda r: forecast_id(r.to_dict()), axis=1)
    tampered = int((ids != stamped["forecast_id"]).sum())
    if tampered:
        v.append(f"{tampered} filas con forecast_id que no re-deriva de su contenido")
    return v
