"""M74-B-R2 · La puerta de PUBLICACIÓN vuelve a acreditar el recibo, no sólo su hash.

La auditoría delta encontró que R1 había cerrado la mitad del problema. `validate` acredita el
recibo de esquema cerrado, sí — pero eso vive en el camino de **escritura**. `publishable()`, que es
quien decide si se publica, sólo comprobaba **ruta, existencia y hash**. Por tanto un
`campaign.json` en `validated` escrito a mano —o por cualquier vía que no pase por esta CLI—
publicaba con **cualquier archivo** cuya ruta y hash cuadraran.

Es exactamente la lección que M73 ya había dejado escrita sobre las invariantes de estado, y que no
se aplicó al recibo: **lo que decide publicar es un lector, así que la acreditación tiene que vivir
en la lectura.** Aquí se prueba desde el lado del atacante: cuatro formas de forjar un `validated`
publicable, y ninguna pasa.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import campaign_state as cs
from tools import campaign_txn as txn

SHA = "a" * 40
OTRO_SHA = "b" * 40
SELLO = "e" * 64  # ★ M74-E-R12 · sha256 del sello de entradas comparado en vivo


def _sellar(tmp_path: Path, fabricar, *, sha: str = SHA, sufijo: str = "") -> Path:
    """Una campaña sintética en `computed`, lista para que alguien intente publicarla.

    ★ M74-E-R13: con su manifiesto acreditado al lado (`_manifiesto`), porque publicar ya lo exige.
    """
    base = tmp_path / f"campana{sufijo}"
    manifiesto = fabricar(base, head=sha)
    panel = base / "panel.csv"
    panel.write_text(f"country,category,table,value\nmexico,EB2,FAD,1{sufijo}\n", encoding="utf-8")
    ruta = manifiesto.with_name("campaign.json")
    txn.open_campaign(ruta, **fabricar.identidad(manifiesto), panel=panel)
    cs.mark_computed(
        ruta, completed_at=txn.now_rfc3339(), input_gate="passed", output_gate="passed", consistency="passed"
    )
    return ruta


def _manifiesto(ruta: Path) -> Path:
    """El manifiesto acreditado que `_sellar` dejó junto a la transacción."""
    return ruta.with_name("campaign_manifest.json")


def _acta(ruta: Path, destino: Path, **cambios: str) -> Path:
    estado = cs.read(ruta) or {}
    datos = {
        "schema": txn.RECEIPT_SCHEMA,
        "campaign_id": estado["campaign_id"],
        "source_git_sha": estado["source_git_sha"],
        "panel_sha256": estado["panel_sha256"],
        "input_seal_sha256": estado["input_seal_sha256"],
        "reviewed_by": "Javier Rebull",
        "decision": "aprobada",
        "reviewed_at": txn.now_rfc3339(),
    }
    datos.update(cambios)
    destino.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return destino


def _forjar_validated(ruta: Path, recibo: Path, **campos: str) -> None:
    """Escribe `validated` A MANO, como lo haría cualquier cosa que no pase por la CLI.

    Éste es el modelo de amenaza real: `campaign.json` es un archivo JSON en el disco, y la
    prohibición de publicar no puede descansar en que todo el mundo use `mark_validated`.
    """
    obj = json.loads(ruta.read_text(encoding="utf-8"))
    obj.update(
        {
            "status": "validated",
            "revision": int(obj.get("revision", 1)) + 1,
            "validated_at": txn.now_rfc3339(),
            "reviewed_by": "Javier Rebull",
            "decision": "aprobada",
            "consistency": "passed",
            "validation_receipt_path": str(recibo.resolve()),
            "validation_receipt_sha256": txn.sha256_file(recibo),
        }
    )
    obj.update(campos)
    ruta.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ═══════════════════════════════════ el control: lo legítimo sigue publicando
def test_a_properly_accredited_validation_still_authorises_publication(tmp_path: Path, campana_sellada) -> None:
    """La mitad que da sentido a los REDs: un recibo real y acreditado SÍ abre la publicación."""
    ruta = _sellar(tmp_path, campana_sellada)
    recibo = _acta(ruta, tmp_path / "acta_ok.json")
    _forjar_validated(ruta, recibo)
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(ruta))
    assert ok, motivo


# ═══════════════════════════════════ cuatro formas de forjar, ninguna publica
def test_arbitrary_markdown_with_the_right_hash_does_not_publish(tmp_path: Path, campana_sellada) -> None:
    """★ El hueco exacto: el hash cuadraba porque se derivaba DEL PROPIO archivo suelto."""
    ruta = _sellar(tmp_path, campana_sellada)
    suelto = tmp_path / "recibo.md"
    suelto.write_text("# Revisión humana\n\nAprobada por el director.\n", encoding="utf-8")
    _forjar_validated(ruta, suelto)  # ruta y sha256 correctos: sólo el CONTENIDO es libre
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(ruta))
    assert not ok, "un markdown de texto libre no puede autorizar una publicación"
    assert "NO acredita" in motivo


def test_a_valid_receipt_from_another_campaign_does_not_publish(tmp_path: Path, campana_sellada) -> None:
    """★ JSON impecable, esquema correcto… de OTRA corrida. Reutilizar un recibo es forjar."""
    otra = _sellar(tmp_path, campana_sellada, sha=OTRO_SHA, sufijo="_otra")
    ajeno = _acta(otra, tmp_path / "acta_ajena.json")
    assert txn.load_validation_receipt(ajeno, cs.read(otra) or {})  # es válido PARA LA SUYA

    ruta = _sellar(tmp_path, campana_sellada)
    _forjar_validated(ruta, ajeno)
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(ruta))
    assert not ok, "un recibo de otra campaña no acredita ésta"
    assert "misma corrida" in motivo


@pytest.mark.parametrize(
    "campo,valor,patron",
    [
        # discrepa el revisor: el recibo es válido, pero no acredita a quien el estado nombra
        ("reviewed_by", "Otra Persona Distinta", "no describe la revisión"),
        # discrepa la decisión: el recibo ni siquiera llega a la comparación — RECHAZA la campaña
        ("decision", "rechazada", "RECHAZA"),
    ],
)
def test_a_state_that_contradicts_its_own_receipt_does_not_publish(
    tmp_path: Path, campana_sellada, campo: str, valor: str, patron: str
) -> None:
    """★ El estado dice una cosa y el recibo que liga dice otra: no describen la misma revisión."""
    ruta = _sellar(tmp_path, campana_sellada)
    recibo = _acta(ruta, tmp_path / "acta_discrepante.json", **{campo: valor})
    _forjar_validated(ruta, recibo)  # el estado conserva 'Javier Rebull' / 'aprobada'
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(ruta))
    assert not ok, f"el estado y el recibo discrepan en {campo} y aun así publicaba"
    assert patron in motivo, motivo


def test_a_forged_validated_state_with_an_automated_reviewer_does_not_publish(tmp_path: Path, campana_sellada) -> None:
    """★ `validated` escrito a mano de punta a punta, con recibo bien formado y revisor de CI."""
    ruta = _sellar(tmp_path, campana_sellada)
    recibo = _acta(ruta, tmp_path / "acta_bot.json", reviewed_by="release-bot")
    _forjar_validated(ruta, recibo, reviewed_by="release-bot")
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(ruta))
    assert not ok, "una identidad automatizada no puede acreditar la revisión en la puerta tampoco"
    assert "automatizada" in motivo


def test_a_receipt_predating_the_computation_does_not_publish(tmp_path: Path, campana_sellada) -> None:
    """Un recibo escrito antes del cómputo no puede ser su revisión — también en la puerta."""
    ruta = _sellar(tmp_path, campana_sellada)
    recibo = _acta(ruta, tmp_path / "acta_vieja.json", reviewed_at="2020-01-01T00:00:00Z")
    _forjar_validated(ruta, recibo)
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(ruta))
    assert not ok and "precede" in motivo


def test_the_receipt_cannot_be_swapped_after_validation(tmp_path: Path, campana_sellada) -> None:
    """Sustituir el recibo por otro bien formado DESPUÉS de validar rompe el permiso por hash…

    …y si además se actualiza el hash a mano, lo rompe la acreditación. Las dos capas, medidas.
    """
    ruta = _sellar(tmp_path, campana_sellada)
    recibo = _acta(ruta, tmp_path / "acta_ok.json")
    _forjar_validated(ruta, recibo)
    assert txn.publishable(ruta, manifest=_manifiesto(ruta))[0]

    # (a) mismo archivo, contenido cambiado ⇒ cae por hash
    _acta(ruta, recibo, reviewed_by="Otra Persona Distinta")
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(ruta))
    assert not ok and "cambió desde la validación" in motivo

    # (b) el forjador actualiza también el hash ⇒ cae por acreditación cruzada
    obj = json.loads(ruta.read_text(encoding="utf-8"))
    obj["validation_receipt_sha256"] = txn.sha256_file(recibo)
    ruta.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(ruta))
    assert not ok and "no describe la revisión" in motivo


def test_the_guard_subcommand_refuses_the_same_forgeries(tmp_path: Path, campana_sellada) -> None:
    """La puerta no es sólo una función: `txn guard` es lo que invoca el publicador real."""
    ruta = _sellar(tmp_path, campana_sellada)
    suelto = tmp_path / "recibo.md"
    suelto.write_text("revisión humana\n", encoding="utf-8")
    _forjar_validated(ruta, suelto)
    assert txn.main(["--path", str(ruta), "guard", "--manifest", str(_manifiesto(ruta))]) == txn.EXIT_BLOCKED

    recibo = _acta(ruta, tmp_path / "acta_ok.json")
    _forjar_validated(ruta, recibo)
    assert txn.main(["--path", str(ruta), "guard", "--manifest", str(_manifiesto(ruta))]) == txn.EXIT_OK
