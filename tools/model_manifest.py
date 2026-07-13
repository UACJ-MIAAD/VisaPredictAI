"""Manifiesto de modelos content-addressed + inventario EXACTO (endurecido, ronda 10).

Inventario DERIVADO y exacto (no pisos): cada clave semantica aparece EXACTAMENTE una vez,
con identidad de campana verificada (SHA COMPLETO de 40, panel_sha256, artifact_sha256
recalculado), sin extras ni duplicados (por ruta CANONICA, no string), y con rutas que no
escapan del repo. Stdlib-only (corre en `ante` y en `ante_nf`).

Endurecimientos (auditoria 13-jul-2026 ronda 10, falsos verdes reproducidos):
* DOS contratos separados: ``validate_attempt_inventory`` (todo lo esperado se INTENTO una vez)
  y ``validate_release_inventory`` (politica de fallos: 'todo failed' SIEMPRE bloquea; un fallo
  legitimo pasa solo si la politica sellada lo permite). Registrar intentos != autorizar release;
* un artefacto ``ok`` debe ser archivo/directorio con archivos regulares NO vacios: un
  directorio vacio (hash del arbol vacio) ya no es un modelo valido;
* rechazo de paths absolutos, ``..``, symlinks en cualquier nivel y aliases que resuelven al
  mismo path canonico (inode) para inflar el conteo;
* el esquema de la campana se valida ANTES de comparar identidades;
* lector JSONL estricto (claves duplicadas, lineas truncadas, objetos no-dict).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from tools.campaign_hashing import artifact_tree_sha256

SCHEMA_VERSION = 3
TYPES = frozenset({"local", "global_deep"})
STATUSES = frozenset({"ok", "failed"})
GLOBAL_KEY = ("type", "table", "model")
LOCAL_KEY = ("type", "table", "model", "country", "category")
REQUIRED = frozenset(
    {"schema_version", "campaign_id", "source_git_sha", "git_dirty", "panel_sha256", "type", "table", "model", "status"}
)
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def semantic_key(entry: dict) -> tuple:
    """Clave semantica EXACTA por tipo: global=(type,table,model); local=+ (country,category)."""
    keys = LOCAL_KEY if entry.get("type") == "local" else GLOBAL_KEY
    return tuple(entry.get(k) for k in keys)


def _no_dupes(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def load_jsonl_strict(path: str | Path) -> list[dict]:
    """Lee un JSONL rechazando claves duplicadas, lineas truncadas y objetos no-dict."""
    entries: list[dict] = []
    for i, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line, object_pairs_hook=_no_dupes)
        except ValueError as e:  # incluye JSONDecodeError (subclase) y clave duplicada
            raise ValueError(f"MANIFEST linea {i}: JSON invalido ({e})") from e
        if not isinstance(obj, dict):
            raise ValueError(f"MANIFEST linea {i}: no es un objeto")
        entries.append(obj)
    return entries


def validate_campaign(campaign: object) -> list[str]:
    """Esquema minimo de la identidad sellada que el manifiesto compara (validar ANTES de usar)."""
    if not isinstance(campaign, dict):
        return ["campaign no es un objeto"]
    probs: list[str] = []
    cid = campaign.get("campaign_id")
    if not isinstance(cid, str) or not cid.strip():
        probs.append("campaign.campaign_id vacio/no-string")
    if not isinstance(campaign.get("source_git_sha"), str) or not _HEX40.match(str(campaign.get("source_git_sha"))):
        probs.append("campaign.source_git_sha no es hex de 40")
    if not isinstance(campaign.get("panel_sha256"), str) or not _SHA256.match(str(campaign.get("panel_sha256"))):
        probs.append("campaign.panel_sha256 no es 'sha256:'+64 hex")
    if not isinstance(campaign.get("git_dirty"), bool):
        probs.append("campaign.git_dirty no es bool")
    return probs


def _path_problem(root: Path, rel: str) -> str | None:
    """Motivo por el que la ruta relativa es insegura, o None. Rechaza absoluta/``..``/symlink."""
    if not rel:
        return "sin path"
    if os.path.isabs(rel):
        return f"path absoluto {rel!r}"
    if ".." in Path(rel).parts:
        return f"path con '..' {rel!r}"
    cur = root
    for part in Path(rel).parts:  # symlink en CUALQUIER nivel
        cur = cur / part
        if cur.is_symlink():
            return f"symlink en la ruta {rel!r}"
    real = os.path.realpath(root / rel)
    rootreal = os.path.realpath(root)
    if not (real == rootreal or real.startswith(rootreal + os.sep)):
        return f"resuelve fuera del repo {rel!r}"
    return None


def _artifact_problem(target: Path) -> str | None:
    """Un artefacto ok debe ser archivo/dir con archivos REGULARES no vacios; None si valido."""
    if not target.exists():
        return "no existe en disco"
    if target.is_symlink():
        return "es symlink"
    if target.is_file():
        return "archivo vacio" if target.stat().st_size == 0 else None
    if target.is_dir():
        anysym = any(q.is_symlink() for q in target.rglob("*"))
        if anysym:
            return "contiene symlink"
        files = [q for q in target.rglob("*") if q.is_file()]
        if not files:
            return "directorio sin archivos regulares (modelo vacio)"
        if any(q.stat().st_size == 0 for q in files):
            return "contiene archivo vacio"
        return None
    return "no es archivo ni directorio regular"


def _identity_problems(e: dict, campaign: dict) -> list[str]:
    probs: list[str] = []
    tag = f"{e.get('type')}/{e.get('table')}/{e.get('model')}"
    if e.get("campaign_id") != campaign.get("campaign_id"):
        probs.append(f"MANIFEST {tag}: campaign_id {e.get('campaign_id')!r} != sellado")
    if e.get("source_git_sha") != campaign.get("source_git_sha"):
        probs.append(f"MANIFEST {tag}: source_git_sha != sellado (otro SHA no cuenta al inventario)")
    if not isinstance(e.get("git_dirty"), bool) or bool(e.get("git_dirty")) != bool(campaign.get("git_dirty")):
        probs.append(f"MANIFEST {tag}: git_dirty {e.get('git_dirty')!r} invalido/!= sellado")
    if e.get("panel_sha256") != campaign.get("panel_sha256"):
        probs.append(f"MANIFEST {tag}: panel_sha256 != sellado")
    return probs


def validate_attempt_inventory(entries: list, *, expected: set, campaign: dict, root: Path) -> list[str]:
    """Contrato de INTENTOS: cada clave esperada aparece EXACTAMENTE una vez (ok o failed).

    'ok' aporta un artefacto no-vacio verificado por hash; 'failed' declara error_type/reason.
    Registrar intentos (incluidos fallos legitimos) NO autoriza publicar — ver
    ``validate_release_inventory``."""
    probs: list[str] = list(f"MANIFEST {p}" for p in validate_campaign(campaign))
    if probs:
        return probs  # sin identidad sellada valida no tiene sentido comparar
    seen_key: dict[tuple, int] = {}
    seen_real: dict[str, int] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            probs.append(f"MANIFEST entrada #{i}: no es un objeto")
            continue
        missing = REQUIRED - set(e.keys())
        if missing:
            probs.append(f"MANIFEST entrada #{i}: faltan campos {sorted(missing)}")
            continue
        if e.get("schema_version") != SCHEMA_VERSION:
            probs.append(f"MANIFEST entrada #{i}: schema_version {e.get('schema_version')!r} != {SCHEMA_VERSION}")
        if e.get("type") not in TYPES:
            probs.append(f"MANIFEST entrada #{i}: type {e.get('type')!r} fuera del enum {sorted(TYPES)}")
            continue
        if e.get("status") not in STATUSES:
            probs.append(f"MANIFEST entrada #{i}: status {e.get('status')!r} fuera del enum {sorted(STATUSES)}")
            continue
        probs += _identity_problems(e, campaign)

        key = semantic_key(e)
        if key in seen_key:
            probs.append(f"MANIFEST: clave semantica duplicada {key} (entradas #{seen_key[key]} y #{i})")
        else:
            seen_key[key] = i

        if e.get("status") == "ok":
            path = str(e.get("path", ""))
            pp = _path_problem(root, path)
            if pp:
                probs.append(f"MANIFEST {key}: {pp}")
            else:
                ap = _artifact_problem(root / path)
                if ap:
                    probs.append(f"MANIFEST {key}: artefacto invalido ({ap})")
                elif e.get("artifact_sha256") != artifact_tree_sha256(root / path):
                    probs.append(f"MANIFEST {key}: artifact_sha256 != recalculado (artefacto alterado/otro)")
                # dedup por ruta CANONICA (inode), no por string: dos textos al mismo artefacto
                real = os.path.realpath(root / path)
                if real in seen_real:
                    probs.append(f"MANIFEST: artefacto duplicado (alias) {path!r} (inflado de conteo)")
                else:
                    seen_real[real] = i
        else:  # failed
            if not e.get("error_type") or not e.get("reason"):
                probs.append(f"MANIFEST {key}: status=failed sin error_type/reason")

    present = set(seen_key)
    unexpected = present - expected
    absent = expected - present
    if unexpected:
        probs.append(
            f"MANIFEST inventario: claves INESPERADAS {sorted(unexpected)[:5]} (+{max(0, len(unexpected) - 5)})"
        )
    if absent:
        probs.append(f"MANIFEST inventario: claves FALTANTES {sorted(absent)[:5]} (+{max(0, len(absent) - 5)})")
    return probs


def validate_release_inventory(
    entries: list, *, expected: set, campaign: dict, root: Path, failure_policy: dict | None = None
) -> list[str]:
    """Contrato de RELEASE: el inventario de intentos DEBE pasar Y la politica de fallos sellada.

    'todo failed' SIEMPRE bloquea. Un fallo legitimo pasa solo si la politica lo permite:
    ``failure_policy = {"required_ok": {claves...}, "max_failed": N}``.
    """
    probs = validate_attempt_inventory(entries, expected=expected, campaign=campaign, root=root)
    ok_keys = {semantic_key(e) for e in entries if isinstance(e, dict) and e.get("status") == "ok"}
    failed_keys = {semantic_key(e) for e in entries if isinstance(e, dict) and e.get("status") == "failed"}
    if expected and not (ok_keys & expected):
        probs.append("MANIFEST release: inventario 100% failed (ningun modelo ok) — no publicable")
    fp = failure_policy or {}
    missing_req = set(fp.get("required_ok", ())) - ok_keys
    if missing_req:
        probs.append(f"MANIFEST release: modelos REQUERIDOS no-ok {sorted(missing_req)[:5]}")
    max_failed = fp.get("max_failed")
    if isinstance(max_failed, int) and len(failed_keys) > max_failed:
        probs.append(f"MANIFEST release: {len(failed_keys)} fallidos > max politica {max_failed}")
    return probs


# Alias de compatibilidad: la validacion de completitud de intentos.
validate_inventory = validate_attempt_inventory
