"""M74-B · Lo que la auditoría ciega demostró que la transacción NO impedía.

Cada prueba de aquí es el contraejemplo de un hallazgo, convertido en RED permanente:

* **H2** — que las cifras cambien es el resultado **esperado** de una re-derivación, y mandaba la
  campaña a `failed`, que es terminal: el único camino era repetir 8–11 h de cómputo.
* **H3** — una forja completa de `campaign.json` pasaba el esquema y abría la publicación.
* **H10** — `validated` era un permiso **permanente y repetible**, y `published` no se escribía nunca.
* **H15** — la ruta diagnóstica (`ALLOW_DIRTY`) llegaba a `validated`, y el manifiesto y la
  transacción podían describir campañas distintas pasando ambos gates.
* **H16** — diez etapas tolerables podían fallar sin dejar rastro.
* **H23** — `status` decía «ausente» ante un JSON corrupto y `archive` no validaba.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools import campaign_state as cs
from tools import campaign_txn as txn

SHA = "a" * 40
RECIBO = "c" * 64
SELLO = "e" * 64  # ★ M74-E-R12 · sha256 del sello de entradas comparado en vivo


def _sellada(tmp_path: Path, fabricar=None, **kw) -> Path:
    """★ M74-E-R13: con `fabricar`, la identidad sale de un manifiesto acreditado (`_manifiesto`): publicar lo exige."""
    p = tmp_path / "campaign.json"
    panel = tmp_path / "panel.csv"
    panel.write_text("x\n", encoding="utf-8")
    identidad: dict[str, Any] = {"campaign_id": "m74b", "source_git_sha": SHA, "input_seal_sha256": SELLO}
    if fabricar is not None:
        identidad = fabricar.identidad(fabricar(tmp_path, dirty=kw.get("git_dirty", False)))
    txn.open_campaign(p, **{**identidad, "git_dirty": kw.get("git_dirty", False)}, panel=panel)
    return p


def _manifiesto(tmp_path: Path) -> Path:
    return tmp_path / "reports" / "campaign" / "campaign_manifest.json"


# ───────────────────────────────────────────────────── H2 · el resultado positivo tiene salida
def test_consistency_pending_is_a_valid_computed_state_not_a_failure(tmp_path: Path, campana_sellada) -> None:
    """RED de H2: la campaña llega a `computed` con la consistencia pendiente, y sigue viva."""
    p = _sellada(tmp_path, campana_sellada)
    obj = cs.mark_computed(
        p, completed_at=txn.now_rfc3339(), input_gate="passed", output_gate="passed", consistency="pending"
    )
    assert obj["status"] == "computed" and obj["consistency"] == "pending"
    assert obj["status"] not in cs.TERMINAL, "un resultado esperado no puede ser terminal"
    # …pero NO publica mientras siga pendiente
    assert not txn.publishable(p, manifest=_manifiesto(tmp_path))[0]


def test_validating_requires_the_consistency_to_be_accredited(tmp_path: Path) -> None:
    """Validar exige la consistencia en `passed`: propagar es precondición, no buena fe."""
    p = _sellada(tmp_path)
    cs.mark_computed(
        p, completed_at=txn.now_rfc3339(), input_gate="passed", output_gate="passed", consistency="pending"
    )
    recibo = tmp_path / "recibo.md"
    recibo.write_text("revisión\n", encoding="utf-8")
    with pytest.raises(ValueError, match="consistency"):
        cs.mark_validated(
            p,
            validation_receipt_sha256=txn.sha256_file(recibo),
            validation_receipt_path=str(recibo),
            reviewed_by="Javier Rebull",
            validated_at=txn.now_rfc3339(),
            decision="aprobada",
            consistency="pending",
        )


def test_the_hard_gates_still_cannot_be_pending(tmp_path: Path) -> None:
    """La tolerancia es SÓLO para la consistencia: las puertas de inputs y outputs no ceden."""
    p = _sellada(tmp_path)
    with pytest.raises(ValueError, match="gate"):
        cs.mark_computed(
            p, completed_at=txn.now_rfc3339(), input_gate="pending", output_gate="passed", consistency="passed"
        )
    with pytest.raises(ValueError, match="gate"):
        cs.mark_computed(
            p, completed_at=txn.now_rfc3339(), input_gate="passed", output_gate="pending", consistency="passed"
        )


# ───────────────────────────────────────────────────────────── H3 · la forja ya no pasa
@pytest.mark.parametrize(
    "mutacion,motivo",
    [
        ({"input_gate": "failed"}, "una puerta en `failed`"),
        ({"output_gate": "no sé"}, "una puerta con texto libre"),
        ({"reviewed_by": ""}, "revisor vacío"),
        ({"decision": "   "}, "decisión en blanco"),
        ({"revision": 0}, "revisión 0 fuera de `running`"),
        ({"consistency": "pending"}, "consistencia pendiente en `validated`"),
        ({"failed_stage": "x"}, "clave de `failed` dentro de `validated`"),
        ({"release_sha": "b" * 40}, "clave de `published` dentro de `validated`"),
        ({"validation_receipt_sha256": "no-hex"}, "recibo que no es sha256"),
    ],
)
def test_a_forged_validated_no_longer_passes_the_schema(mutacion: dict, motivo: str) -> None:
    """RED de H3: cada campo del estado se comprueba por VALOR, no por mera presencia."""
    base = {
        "schema_version": cs.SCHEMA_VERSION,
        "campaign_id": "forjada",
        "status": "validated",
        "revision": 3,
        "source_git_sha": SHA,
        "git_dirty": False,
        "panel_sha256": "sha256:" + "b" * 64,
        "input_seal_sha256": SELLO,
        "started_at": "2026-09-11T00:00:00Z",
        "completed_at": "2026-09-11T00:00:00Z",
        "validated_at": "2026-09-11T00:00:00Z",
        "input_gate": "passed",
        "output_gate": "passed",
        "consistency": "passed",
        "reviewed_by": "Javier Rebull",
        "decision": "aprobada",
        "validation_receipt_sha256": RECIBO,
        "validation_receipt_path": "recibo.md",
    }
    assert cs.validate_schema(base) == [], "la base honesta debe ser válida (si no, la prueba no discrimina)"
    assert cs.validate_schema({**base, **mutacion}), f"debía rechazar: {motivo}"


# ──────────────────────────────────────────────── H15 · árbol sucio y campañas cruzadas
def test_a_dirty_tree_campaign_never_publishes(tmp_path: Path, campana_sellada) -> None:
    """RED de H15: la ruta diagnóstica llegaba a `validated` y de ahí a publicar."""
    p = _sellada(tmp_path, campana_sellada, git_dirty=True)
    recibo = tmp_path / "recibo.md"
    recibo.write_text("diagnóstica\n", encoding="utf-8")
    cs.mark_computed(p, completed_at=txn.now_rfc3339(), input_gate="passed", output_gate="passed", consistency="passed")
    cs.mark_validated(
        p,
        validation_receipt_sha256=txn.sha256_file(recibo),
        validation_receipt_path=str(recibo),
        reviewed_by="Javier Rebull",
        validated_at=txn.now_rfc3339(),
        decision="diagnóstica",
    )
    ok, motivo = txn.publishable(p, manifest=_manifiesto(tmp_path))
    assert not ok and "SUCIO" in motivo


def test_manifest_and_transaction_must_describe_the_same_campaign(tmp_path: Path, campana_sellada) -> None:
    """RED de H15: dos `campaign_id` distintos pasaban ambos gates por separado."""
    p = _validada(tmp_path, campana_sellada)
    manifiesto = _manifiesto(tmp_path)
    # ★ M74-E-R13: el legítimo es el manifiesto ACREDITADO que describe la transacción, no un JSON escrito a mano
    legitimo = json.loads(manifiesto.read_text(encoding="utf-8"))
    manifiesto.write_text(json.dumps({**legitimo, "campaign_id": "OTRA"}), encoding="utf-8")
    ok, motivo = txn.publishable(p, manifest=manifiesto)
    assert not ok and "no son la misma corrida" in motivo

    manifiesto.write_text(json.dumps(legitimo), encoding="utf-8")
    assert txn.publishable(p, manifest=manifiesto)[0]


# ─────────────────────────────────────────────── H3 · el recibo se liga por ruta y por hash
def test_the_validation_receipt_must_still_exist_and_match(tmp_path: Path, campana_sellada) -> None:
    """Un sha suelto no acredita nada si el archivo no está, o si cambió después."""
    p = _validada(tmp_path, campana_sellada)
    recibo = Path((cs.read(p) or {})["validation_receipt_path"])
    assert txn.publishable(p, manifest=_manifiesto(tmp_path))[0]

    recibo.write_text("MANIPULADO después de validar\n", encoding="utf-8")
    ok, motivo = txn.publishable(p, manifest=_manifiesto(tmp_path))
    assert not ok and "cambió desde la validación" in motivo

    recibo.unlink()
    ok, motivo = txn.publishable(p, manifest=_manifiesto(tmp_path))
    assert not ok and "no existe" in motivo


def escribir_recibo(tmp_path: Path, estado_path: Path, **cambios: str) -> Path:
    """Un recibo de revisión REAL, de esquema cerrado y ligado a la campaña de `estado_path`.

    ⚠️ Antes este andamiaje escribía un `recibo.md` de texto libre y llamaba a `mark_validated`
    directamente. Pasaba —y ahí estaba el hueco— porque la puerta de publicación sólo miraba ruta
    y hash. Ahora el andamiaje usa el camino real: si el recibo deja de acreditar, estas pruebas
    caen, que es lo que se quiere de un andamiaje.
    """
    estado = cs.read(estado_path) or {}
    acta = {
        "schema": txn.RECEIPT_SCHEMA,
        "campaign_id": estado["campaign_id"],
        "source_git_sha": estado["source_git_sha"],
        "panel_sha256": estado["panel_sha256"],
        "input_seal_sha256": estado["input_seal_sha256"],
        "reviewed_by": "Javier Rebull",
        "decision": "aprobada",
        "reviewed_at": txn.now_rfc3339(),
    }
    acta.update(cambios)
    destino = tmp_path / f"recibo_{len(list(tmp_path.glob('recibo_*.json')))}.json"
    destino.write_text(json.dumps(acta, ensure_ascii=False), encoding="utf-8")
    return destino


def _validada(tmp_path: Path, fabricar=None) -> Path:
    p = _sellada(tmp_path, fabricar)
    cs.mark_computed(p, completed_at=txn.now_rfc3339(), input_gate="passed", output_gate="passed", consistency="passed")
    recibo = escribir_recibo(tmp_path, p)
    acta = txn.load_validation_receipt(recibo, cs.read(p) or {})
    cs.mark_validated(
        p,
        validation_receipt_sha256=txn.sha256_file(recibo),
        validation_receipt_path=str(recibo.resolve()),
        reviewed_by=acta["reviewed_by"],
        validated_at=txn.now_rfc3339(),
        decision=acta["decision"],
    )
    return p


# ────────────────────────────────────────────────────── H10 · publicar consume el permiso
def test_publishing_consumes_the_permit(tmp_path: Path, campana_sellada) -> None:
    """RED de H10: `validated` era permanente y repetible, y `published` no se escribía nunca."""
    p = _validada(tmp_path, campana_sellada)
    assert txn.publishable(p, manifest=_manifiesto(tmp_path))[0]
    cs.mark_published(p, published_at=txn.now_rfc3339(), release_sha="b" * 40)
    ok, motivo = txn.publishable(p, manifest=_manifiesto(tmp_path))
    assert not ok and "published" in motivo, "una campaña ya publicada no vuelve a autorizar"


def test_the_publisher_consumes_the_permit_after_pushing_not_before() -> None:
    """El orden importa: consumirlo antes dejaría un fallo de push sin permiso y sin publicación."""
    guion = (Path(__file__).resolve().parent.parent / "experiments" / "sync_all.sh").read_text(encoding="utf-8")
    assert "txn_publish" in guion, "el publicador debe consumir el permiso (H10)"
    assert guion.index("git push") < guion.index("txn_publish ||"), "se consume DESPUÉS de publicar"
    assert "--manifest" in guion, "el guard debe cruzar el manifiesto (H15)"


# ──────────────────────────────────────────────────────────── H16 · las etapas tolerables
def test_best_effort_failures_are_recorded_for_the_human_review(tmp_path: Path) -> None:
    """RED de H16: diez etapas tolerables podían fallar sin dejar rastro en la transacción."""
    p = _sellada(tmp_path)
    obj = cs.mark_computed(
        p,
        completed_at=txn.now_rfc3339(),
        input_gate="passed",
        output_gate="passed",
        consistency="passed",
        best_effort_failures=["improve_stacking.py", "check_drift.py"],
    )
    assert obj["best_effort_failures"] == ["improve_stacking.py", "check_drift.py"]
    assert cs.validate_schema(obj) == []


def test_the_runbook_collects_and_reports_its_best_effort_failures() -> None:
    guion = (Path(__file__).resolve().parent.parent / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    assert "BEST_EFFORT_FAILED" in guion and "--best-effort-failure" in guion


# ───────────────────────────────────────────────────────── H23 · status y archive validan
def test_status_distinguishes_absent_from_unreadable(tmp_path: Path) -> None:
    """RED de H23: un JSON con claves duplicadas se reportaba como «ausente»."""
    ruta = tmp_path / "campaign.json"
    assert txn.main(["--path", str(ruta), "status"]) == txn.EXIT_OK  # ausente de verdad
    ruta.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    assert txn.main(["--path", str(ruta), "status"]) == txn.EXIT_ERROR, "ilegible ≠ ausente"


def test_archive_refuses_a_corrupt_transaction(tmp_path: Path) -> None:
    """RED de H23: archivar a ciegas un `campaign.json` que no se entiende."""
    ruta = tmp_path / "campaign.json"
    ruta.write_text('{"status": "failed"}', encoding="utf-8")
    with pytest.raises(ValueError, match="esquema"):
        txn.archive_if_terminal(ruta, tmp_path / "archivo")


# ─────────────────────────────────────────────────────────────── H14 · las señales
def test_the_runbook_traps_every_signal_that_can_orphan_a_campaign() -> None:
    """RED de H14: sin `HUP`, cerrar la terminal dejaba la txn en `running` y el hijo huérfano."""
    guion = (Path(__file__).resolve().parent.parent / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    import re

    # se buscan los traps de VERDAD, no la señal suelta en cualquier comentario
    atrapadas = set(re.findall(r"^trap\s+.*?\b(EXIT|INT|TERM|HUP)\s*$", guion, flags=re.M))
    assert atrapadas == {"EXIT", "INT", "TERM", "HUP"}, f"traps instalados: {sorted(atrapadas)}"
    # ★ R14: `kill -- -$$` sólo mataba el grupo si bash era su líder; ahora se recorre el árbol
    assert 'kill_descendants "$$"' in guion, "el trap debe matar a todos los descendientes, no sólo al padre"
