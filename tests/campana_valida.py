"""Transacciones de campaña con el ESQUEMA REAL (ADR 0003) para las pruebas.

G7 · puerta 1: `artifact_receipt.read_running_transaction` exige el esquema de `campaign.json`, así que una
transacción de juguete (cuatro claves) ya no presta identidad. Las pruebas que necesitan una transacción la
piden aquí: identidad que cada prueba elige, el resto de campos como los escribe la máquina de estados, y el
validador real (`vp_model.campaign_schema.validate_schema`) la acepta antes de devolverla.
"""

from __future__ import annotations

import json
from pathlib import Path

from vp_model.campaign_schema import SCHEMA_VERSION, validate_schema

T0 = "2026-10-14T12:00:00+00:00"
T1 = "2026-10-14T13:00:00+00:00"

#: Lo que cada estado añade a una transacción recién sellada, como lo dejan las transiciones de la máquina.
_POR_ESTADO: dict[str, dict] = {
    "running": {"revision": 0},
    "computed": {"revision": 1, "completed_at": T1, "input_gate": "passed", "output_gate": "passed",
                 "consistency": "pending"},
    "failed": {"revision": 1, "failed_at": T1, "failed_stage": "prueba", "reason": "fallo de prueba"},
    "validated": {"revision": 2, "completed_at": T1, "input_gate": "passed", "output_gate": "passed",
                  "consistency": "passed", "validated_at": T1, "reviewed_by": "revisor", "decision": "approved",
                  "validation_receipt_sha256": "d" * 64},
    "published": {"revision": 3, "completed_at": T1, "input_gate": "passed", "output_gate": "passed",
                  "consistency": "passed", "validated_at": T1, "reviewed_by": "revisor", "decision": "approved",
                  "validation_receipt_sha256": "d" * 64, "published_at": T1, "release_sha": "e" * 40},
}  # fmt: skip


def transaccion(*, campaign_id: str, source_git_sha: str, panel_sha256: str, status: str = "running") -> dict:
    """Una transacción `status` válida con esa identidad; falla si el esquema real no la acepta."""
    doc = {"schema_version": SCHEMA_VERSION, "campaign_id": campaign_id, "source_git_sha": source_git_sha,
           "git_dirty": False, "panel_sha256": panel_sha256, "started_at": T0, "input_seal_sha256": "c" * 64,
           "status": status, **_POR_ESTADO[status]}  # fmt: skip
    problemas = validate_schema(doc)
    assert not problemas, f"transacción de prueba fuera de esquema: {problemas}"
    return doc


def escribir(reports: Path, **kw) -> Path:
    """Escribe `reports/campaign/campaign.json` con :func:`transaccion`. Devuelve la ruta."""
    destino = Path(reports) / "campaign" / "campaign.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(transaccion(**kw)), encoding="utf-8")
    return destino
