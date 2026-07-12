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
    "row_hash",
    "frozen_at",
    "freeze_panel_vintage",
    "panel_hash",
    "git_sha",
    "model_version",
    "evaluation_mode",
]

# Contenido del pronóstico protegido por ``row_hash`` (auditoría 11-jul: ``forecast_id``
# solo cubre clave+receta — mutar days/bandas no lo alteraba). Lista FIJA presente en
# ambos ledgers; los campos de procedencia sombra (shadow/recipe/hold_mase) quedan
# anclados por git, no por el hash.
PAYLOAD_COLS = ("h", "days", "lo80", "hi80", "lo95", "hi95")


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

    Es IDENTIDAD (qué pronóstico es), no integridad de contenido: no incorpora days ni
    bandas. La integridad del contenido la verifica ``row_hash`` (auditoría 11-jul).
    """
    key = "|".join(str(row.get(k, "")) for k in ("origin", "country", "category", "table", "date", "model_version"))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _norm_payload(v: object) -> str:
    """Forma canónica que sobrevive el round-trip CSV↔pandas: NaN/None → ``""``; un float
    entero (25.0, resultado de una columna con NaN) y el int que lo originó (25) dan la
    MISMA forma — si no, el hash sellado al freeze no re-derivaría tras releer el CSV."""
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


def row_hash(row: dict) -> str:
    """sha1 12-hex del CONTENIDO del pronóstico (``PAYLOAD_COLS``) — mutar days o una
    banda en una fila congelada hace que ``validate`` truene."""
    key = "|".join(_norm_payload(row.get(c)) for c in PAYLOAD_COLS)
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
        rr["row_hash"] = row_hash(rr)
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


COMPLETENESS_ALLOWLIST = ROOT / "reports" / "governance" / "completeness_allowlist.json"


def load_completeness_allowlist(path: Path | None = None) -> dict[str, str]:
    """Excepciones NOMINALES de completitud (R0-04): clave → motivo, con expiración.
    Un porcentaje global no es trazable; cada omisión tolerada se registra POR CLAVE en
    git con motivo y mes de expiración (``expires`` >= añada del panel para contar).
    Entradas expiradas dejan de eximir — la añada vuelve a abortar hasta renovarlas."""
    p = path or COMPLETENESS_ALLOWLIST
    if not p.exists():
        return {}
    now = panel_vintage()
    out: dict[str, str] = {}
    for key, entry in json.loads(p.read_text()).items():
        if isinstance(entry, dict) and str(entry.get("expires", "")) >= now:
            out[key] = str(entry.get("reason", "sin motivo"))
    return out


def completeness_problems(
    expected: set[str], got: set[str], *, label: str, allowed: dict[str, str] | None = None
) -> list[str]:
    """Completitud de una añada por IGUALDAD DE SETS (A-05 + R0-04, auditoría ciega:
    el umbral 90 % dejaba desaparecer 1-2 series por mes en silencio — 19/20 pasaba).

    Reglas fail-closed: expected vacío = sin señal (no gate); got vacío = tabla completa
    ausente; CUALQUIER clave esperada ausente = violación salvo excepción NOMINAL vigente
    del allowlist versionado (por clave, con motivo y expiración — jamás un porcentaje);
    claves fuera del catálogo = deriva. Las omisiones eximidas se reportan aparte por el
    caller (visibles en log/SES), nunca silenciosas.
    """
    if not expected:
        return []
    allowed = allowed or {}
    problems: list[str] = []
    missing = sorted(expected - got)
    unexcused = [k for k in missing if k not in allowed]
    extra = sorted(got - expected)
    if not got:
        problems.append(f"{label}: 0 de {len(expected)} claves esperadas — tabla completa ausente")
    elif unexcused:
        problems.append(
            f"{label}: {len(unexcused)} clave(s) esperada(s) AUSENTE(s) sin excepción nominal: "
            f"{unexcused[:8]}{' …' if len(unexcused) > 8 else ''}"
        )
    if extra:
        problems.append(f"{label}: {len(extra)} claves FUERA del catálogo vigente (deriva): p.ej. {extra[:5]}")
    return problems


def validate(df: pd.DataFrame) -> list[str]:
    """Contrato del ledger v2 — regresa violaciones (lista vacía = OK).

    FAIL-CLOSED (2ª ronda de auditoría, 12-jul): TODA fila debe traer el sello v2
    completo — anular ``frozen_at`` o ``row_hash`` era una vía de escape que dejaba
    la fila fuera de todos los chequeos. Los ledgers están 100 % sellados desde el
    backfill; una fila sin sello ES una violación, no una fila exenta.

    Guardias: clave única; columnas v2 presentes; sello v2 no nulo en cada fila;
    ninguna fila ``live`` con un target ya publicado a su ``freeze_panel_vintage``
    (manipulación temporal); ``forecast_id`` re-deriva de clave+receta (identidad) y
    ``row_hash`` re-deriva del payload (contenido) en TODAS las filas.
    """
    v: list[str] = []
    if df.duplicated(subset=KEYS).any():
        v.append("filas duplicadas en la clave (origin, serie, fecha)")
    missing = [c for c in V2_COLS if c not in df.columns]
    if missing:
        v.append(f"faltan columnas v2: {missing}")
        return v
    for c in V2_COLS:
        nulls = int(df[c].isna().sum())
        if nulls:
            v.append(f"{nulls} filas con {c} nulo — sello v2 incompleto (fail-closed)")
    live = df[df["evaluation_mode"] == "live"]
    bad = live[live["date"].astype(str).str[:7] <= live["freeze_panel_vintage"].astype(str)]
    if len(bad):
        v.append(f"{len(bad)} filas 'live' cuyo target ya estaba publicado a su freeze_panel_vintage")
    ids = df.apply(lambda r: forecast_id(r.to_dict()), axis=1)
    tampered = int((ids != df["forecast_id"]).sum())
    if tampered:
        v.append(f"{tampered} filas con forecast_id que no re-deriva de su clave+receta")
    rehash = df.apply(lambda r: row_hash(r.to_dict()), axis=1)
    mutated = int((rehash != df["row_hash"]).sum())
    if mutated:
        v.append(f"{mutated} filas con row_hash que no re-deriva de su contenido (days/bandas mutados)")
    return v
