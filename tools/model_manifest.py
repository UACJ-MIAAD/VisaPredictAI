"""Manifiesto de modelos content-addressed + inventario EXACTO (fundacion).

Reemplaza los PISOS ('>= 250 locales', 'startswith(global)') por un inventario DERIVADO y
exacto (auditoria 13-jul-2026 ronda 9): cada clave semantica aparece EXACTAMENTE una vez,
con identidad de campana verificada (SHA COMPLETO de 40, panel_sha256, artifact_sha256
recalculado), sin extras, sin duplicados y con rutas que no escapan del repo. Distingue un
fallo LEGITIMO (entrada status='failed' con error_type/reason) de 'el productor nunca lo
intento' (clave ausente). Stdlib-only (corre en `ante` y en `ante_nf`).
"""

from __future__ import annotations

import os
from pathlib import Path

from tools.campaign_hashing import artifact_tree_sha256

SCHEMA_VERSION = 3
TYPES = frozenset({"local", "global_deep"})
STATUSES = frozenset({"ok", "failed"})
GLOBAL_KEY = ("type", "table", "model")
LOCAL_KEY = ("type", "table", "model", "country", "category")
# Campos comunes obligatorios en TODA entrada (ok o failed).
REQUIRED = frozenset(
    {"schema_version", "campaign_id", "source_git_sha", "git_dirty", "panel_sha256", "type", "table", "model", "status"}
)


def semantic_key(entry: dict) -> tuple:
    """Clave semantica EXACTA por tipo: global=(type,table,model); local=+ (country,category)."""
    keys = LOCAL_KEY if entry.get("type") == "local" else GLOBAL_KEY
    return tuple(entry.get(k) for k in keys)


def _path_escapes(root: Path, rel: str) -> bool:
    """True si `rel` (relativa al repo) escapa del arbol del repo (``..`` / absoluta)."""
    try:
        target = (root / rel).resolve()
        return not (target == root.resolve() or str(target).startswith(str(root.resolve()) + os.sep))
    except OSError, ValueError:
        return True


def _identity_problems(e: dict, campaign: dict) -> list[str]:
    """Identidad EXACTA de una entrada contra la campana sellada (campaign.json)."""
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


def validate_inventory(entries: list, *, expected: set, campaign: dict, root: Path) -> list[str]:
    """Verifica el manifiesto contra el inventario EXACTO esperado + identidad de campana.

    ``expected`` = conjunto de claves semanticas esperadas (globales ∪ locales), derivado por
    el gate (TABLES × GLOBAL_FINALISTS y series_elegibles × LOCAL_FINALISTS). Cada clave debe
    tener EXACTAMENTE una entrada (ok o failed): 'ok' aporta el artefacto verificado por hash;
    'failed' explica su ausencia. Devuelve la lista de problemas (vacia = valido).
    """
    probs: list[str] = []
    seen_key: dict[tuple, int] = {}
    seen_path: dict[str, int] = {}
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
            if not path:
                probs.append(f"MANIFEST {key}: status=ok sin path")
            elif _path_escapes(root, path):
                probs.append(f"MANIFEST {key}: path {path!r} escapa del repo")
            elif not (root / path).exists():
                probs.append(f"MANIFEST {key}: el artefacto {path!r} no existe en disco")
            elif e.get("artifact_sha256") != artifact_tree_sha256(root / path):
                probs.append(f"MANIFEST {key}: artifact_sha256 != recalculado (artefacto alterado/otro)")
            if path:
                if path in seen_path:
                    probs.append(f"MANIFEST: path duplicado {path!r} (inflado de conteo)")
                else:
                    seen_path[path] = i
        else:  # failed: debe declarar por que
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
