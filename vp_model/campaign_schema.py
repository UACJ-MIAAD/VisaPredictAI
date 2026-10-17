"""Esquema de `campaign.json` (ADR 0003): la ÚNICA autoridad de lo que es una transacción de campaña válida.

Vive en `vp_model` porque lo LEEN dos capas: la máquina de estados (`tools.campaign_state`, que escribe y
transiciona) y los lectores de `vp_model` que prestan la identidad de la campaña a los artefactos
(`artifact_receipt.read_running_transaction`). ADR-0001 prohíbe que `vp_model` importe `tools`; duplicar el
esquema crearía dos autoridades que divergen, así que se trasladó aquí sin cambios y `tools.campaign_state` lo
reexporta (G7 · puerta 1). Stdlib-only.
"""

from __future__ import annotations

import datetime as dt
import re

SCHEMA_VERSION = 3
STATUSES = ("running", "computed", "failed", "validated", "published")
# Identidad: NUNCA cambia tras sellar RUNNING. ★ M74-E-R12: incluye el sha256 del sello comparado en vivo.
_IDENTIDAD = ("campaign_id", "source_git_sha", "git_dirty", "panel_sha256", "started_at", "input_seal_sha256")
_IMMUTABLE = ("schema_version", *_IDENTIDAD)
_REQUIRED = frozenset({*_IMMUTABLE, "status", "revision"})
# Toda clave legitima de campaign.json (una clave fuera de aqui = esquema roto).
ALLOWED_KEYS = _REQUIRED | frozenset(
    {
        "completed_at",
        "failed_at",
        "validated_at",
        "published_at",
        "input_gate",
        "output_gate",
        "consistency",
        "exit_code",
        "signal",
        "failed_stage",
        "reason",
        "reviewed_by",
        "decision",
        "validation_receipt_sha256",
        "release_sha",
        "validation_receipt_path",
        "best_effort_failures",
        # G7 · B5a: sha256 de la copia exacta del `campaign.json` en `computed` (la evidencia del cómputo).
        "computed_sha256",
    }
)
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GATE_OK = "passed"
#: H2 · «cómputo completo, propagación pendiente» NO es un fallo. La campaña llega a `computed`
#: con la consistencia en `pending` y es `validate` quien la vuelve a exigir en `passed`, tras
#: propagar. Antes, la consistencia rota mandaba la campaña a `failed`, que es TERMINAL: el
#: resultado positivo —que las cifras cambien— no tenía salida salvo repetir 8-11 h de cómputo.
_GATE_PENDING = "pending"
_GATE_VALUES = (_GATE_OK, _GATE_PENDING)
#: Claves que NO pueden coexistir con un estado: un `validated` que arrastre `failed_stage`
#: describe dos historias a la vez.
_STATE_FORBIDS: dict[str, tuple[str, ...]] = {
    "running": (
        "completed_at",
        "validated_at",
        "published_at",
        "failed_at",
        "failed_stage",
        "reason",
        "release_sha",
        "computed_sha256",
    ),
    "computed": (
        "validated_at",
        "published_at",
        "failed_at",
        "failed_stage",
        "reason",
        "release_sha",
        "computed_sha256",
    ),
    "validated": ("published_at", "failed_at", "failed_stage", "reason", "release_sha"),
    "published": ("failed_at", "failed_stage", "reason"),
    "failed": (),
}


def _valid_ts(v: object) -> bool:
    if not isinstance(v, str) or not v:
        return False
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return d.tzinfo is not None


#: Lo que cada estado DEBE llevar ya relleno. Un estado no es una etiqueta: es su evidencia.
_STATE_REQUIRES: dict[str, tuple[str, ...]] = {
    "running": (),
    "computed": ("completed_at", "input_gate", "output_gate", "consistency"),
    "failed": ("failed_at", "failed_stage", "reason"),
    "validated": (
        "completed_at",
        "input_gate",
        "output_gate",
        "consistency",
        "validated_at",
        "reviewed_by",
        "decision",
        "validation_receipt_sha256",
        "computed_sha256",
    ),
    "published": (
        "completed_at",
        "input_gate",
        "output_gate",
        "consistency",
        "validated_at",
        "reviewed_by",
        "decision",
        "validation_receipt_sha256",
        "published_at",
        "release_sha",
        "computed_sha256",
    ),
}


def _state_invariants(obj: dict) -> list[str]:
    """Invariantes POR ESTADO, exigidas al LEER y no sólo al transicionar.

    ⚠️ M72-R2 las añadió comprobando sólo ``is None``, y eso deja pasar una forja completa: un
    ``validated`` con ``input_gate="failed"``, ``reviewed_by=""``, ``revision=0`` y ``git_dirty``
    verdadero pasaba el esquema y abría la publicación (H3 de la auditoría ciega). Aquí se exige
    el VALOR, no la mera presencia.
    """
    probs: list[str] = []
    estado = obj.get("status")
    if not isinstance(estado, str) or estado not in STATUSES:
        return probs  # ya lo reporta validate_schema

    for campo in _STATE_REQUIRES[estado]:
        if obj.get(campo) is None:
            probs.append(f"campaign.json: estado '{estado}' exige {campo} no nulo")

    # claves incompatibles con el estado
    for campo in _STATE_FORBIDS[estado]:
        if obj.get(campo) is not None:
            probs.append(f"campaign.json: estado '{estado}' no admite {campo}")

    # revisión: sólo `running` recién sellado puede estar en 0
    rev = obj.get("revision")
    if isinstance(rev, int) and not isinstance(rev, bool) and estado != "running" and rev < 1:
        probs.append(f"campaign.json: estado '{estado}' exige revision >= 1 (hay {rev})")

    # los gates llevan VALOR, no cualquier cadena
    for campo in ("input_gate", "output_gate"):
        valor = obj.get(campo)
        if valor is not None and valor != _GATE_OK:
            probs.append(f"campaign.json: {campo} debe ser '{_GATE_OK}' (hay {valor!r})")
    consistencia = obj.get("consistency")
    if consistencia is not None and consistencia not in _GATE_VALUES:
        probs.append(f"campaign.json: consistency debe estar en {list(_GATE_VALUES)} (hay {consistencia!r})")
    # ★ `pending` sólo vive en `computed`: validar y publicar exigen la consistencia acreditada
    if consistencia == _GATE_PENDING and estado in {"validated", "published"}:
        probs.append(f"campaign.json: estado '{estado}' no admite consistency='{_GATE_PENDING}'")

    # cadenas que existen para que alguien las lea
    for campo in ("reviewed_by", "decision", "failed_stage", "reason"):
        valor = obj.get(campo)
        if valor is not None and (not isinstance(valor, str) or not valor.strip()):
            probs.append(f"campaign.json: {campo} no puede ser una cadena vacía")

    return probs + _invariantes_de_publicacion(obj, estado)


def _invariantes_de_publicacion(obj: dict, estado: str) -> list[str]:
    """Formato de lo que la validación y la publicación añaden (recibo, evidencia del cómputo, release)."""
    probs: list[str] = []
    if estado in {"validated", "published"}:
        recibo = obj.get("validation_receipt_sha256")
        if recibo is not None and (not isinstance(recibo, str) or not _HEX64.match(recibo)):
            probs.append("campaign.json: validation_receipt_sha256 debe ser 64 hex")
        ruta = obj.get("validation_receipt_path")
        if ruta is not None and (not isinstance(ruta, str) or not ruta.strip()):
            probs.append("campaign.json: validation_receipt_path no puede ser una cadena vacía")
    computado = obj.get("computed_sha256")
    if computado is not None and (not isinstance(computado, str) or not _HEX64.match(computado)):
        probs.append("campaign.json: computed_sha256 debe ser 64 hex")
    if estado == "published":
        rel = obj.get("release_sha")
        if rel is not None and (not isinstance(rel, str) or not _HEX40.match(rel)):
            probs.append("campaign.json: release_sha debe ser hex de 40")

    fallos = obj.get("best_effort_failures")
    if fallos is not None and not (isinstance(fallos, list) and all(isinstance(x, str) for x in fallos)):
        probs.append("campaign.json: best_effort_failures debe ser una lista de cadenas")
    return probs


def validate_schema(obj: object) -> list[str]:
    """Esquema ESTRICTO: tipos exactos, formatos canonicos, sin claves desconocidas."""
    probs: list[str] = []
    if not isinstance(obj, dict):
        return ["campaign.json no es un objeto"]
    missing = _REQUIRED - set(obj.keys())
    if missing:
        probs.append(f"campaign.json: faltan campos {sorted(missing)}")
    unknown = set(obj.keys()) - ALLOWED_KEYS
    if unknown:
        probs.append(f"campaign.json: claves desconocidas {sorted(unknown)}")
    if obj.get("schema_version") != SCHEMA_VERSION:
        probs.append(f"campaign.json: schema_version {obj.get('schema_version')!r} != {SCHEMA_VERSION}")
    if obj.get("status") not in STATUSES:
        probs.append(f"campaign.json: status {obj.get('status')!r} fuera del enum {list(STATUSES)}")
    rev = obj.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        probs.append(f"campaign.json: revision {rev!r} debe ser int >= 0")
    cid = obj.get("campaign_id")
    if not isinstance(cid, str) or not cid.strip():
        probs.append("campaign.json: campaign_id vacio/no-string")
    sha = obj.get("source_git_sha")
    if not isinstance(sha, str) or not _HEX40.match(sha):
        probs.append("campaign.json: source_git_sha debe ser hex de 40 ([0-9a-f]{40})")
    ph = obj.get("panel_sha256")
    if not isinstance(ph, str) or not _SHA256.match(ph):
        probs.append("campaign.json: panel_sha256 debe ser 'sha256:'+64 hex (no 'n/d')")
    if not isinstance(obj.get("input_seal_sha256"), str) or not _HEX64.match(obj["input_seal_sha256"]):
        probs.append("campaign.json: input_seal_sha256 debe ser el sha256 (64 hex) del sello de entradas")
    # git_dirty EXACTAMENTE bool (isinstance excluye 1/0; bool es subclase de int)
    if not isinstance(obj.get("git_dirty"), bool):
        probs.append("campaign.json: git_dirty debe ser booleano exacto")
    if not _valid_ts(obj.get("started_at")):
        probs.append("campaign.json: started_at debe ser timestamp RFC 3339 con tz")
    probs += _state_invariants(obj)
    return probs
