"""M74-E-R12 · el sello comparado en vivo viaja DENTRO de la transacción y se cruza al publicar.

Auditoría del 15-sep (sin informe escrito): R11 protegía el arranque, pero el hash acreditado se perdía al
abrir la transacción. Con una transacción legítimamente `validated` y un manifiesto falsificado de la MISMA
campaña —bien formado, con entradas y protocolo falsos—, las dos puertas autorizaban publicar:
`_manifest_matches` sólo comparaba `campaign_id` y `txn open` no recibía el sello.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import campaign_manifest as cm
from tools import campaign_state as cs
from tools import campaign_txn as txn

RAIZ = Path(__file__).resolve().parents[1]
RUTAS_FALSAS = ("experiments/no_existe.py", "tools/tampoco.py", "vp_model/fantasma.py")


def _recibo(raiz: Path, estado: dict, **cambios) -> Path:
    datos = {"schema": txn.RECEIPT_SCHEMA, **{k: estado[k] for k in txn.IDENTIDAD_RECIBO}}
    datos |= {"reviewed_by": "Javier Rebull", "decision": "aprobada", "reviewed_at": txn.now_rfc3339(), **cambios}
    ruta = raiz / f"recibo_{len(list(raiz.glob('recibo_*.json')))}.json"
    ruta.write_text(json.dumps({k: v for k, v in datos.items() if v is not None}), encoding="utf-8")
    return ruta


def _campana_validada(tmp_path: Path, campana_sellada) -> tuple[Path, Path, Path]:
    """Manifiesto y sello legítimos, y su transacción validada con el MISMO sello, por el camino real."""
    raiz = tmp_path / "repo"
    manifiesto = campana_sellada(raiz)
    datos = json.loads(manifiesto.read_text(encoding="utf-8"))
    panel = raiz / "panel.csv"
    panel.write_text("x\n", encoding="utf-8")
    ruta = raiz / "reports" / "campaign" / "campaign.json"
    identidad = {"campaign_id": datos["campaign_id"], "source_git_sha": datos["git_sha"], "git_dirty": False}
    txn.open_campaign(ruta, **identidad, panel=panel, input_seal_sha256=datos["preflight_sha256"])
    cs.mark_computed(
        ruta, completed_at=txn.now_rfc3339(), input_gate="passed", output_gate="passed", consistency="passed"
    )
    recibo = _recibo(raiz, cs.read(ruta) or {})
    acta = txn.load_validation_receipt(recibo, cs.read(ruta) or {})
    cs.mark_validated(
        ruta,
        validation_receipt_sha256=txn.sha256_file(recibo),
        validation_receipt_path=str(recibo.resolve()),
        reviewed_by=acta["reviewed_by"],
        validated_at=txn.now_rfc3339(),
        decision=acta["decision"],
    )
    return ruta, manifiesto, raiz


def test_control_la_campana_legitima_publica(tmp_path: Path, campana_sellada) -> None:
    ruta, manifiesto, _ = _campana_validada(tmp_path, campana_sellada)
    assert cm.publish_blocker(manifiesto) is None
    ok, motivo = txn.publishable(ruta, manifest=manifiesto)
    assert ok, motivo


def test_RED_un_manifiesto_bien_formado_de_la_misma_campana_tras_validar(tmp_path: Path, campana_sellada) -> None:
    """★ La reproducción de la auditoría: tras `validated`, manifiesto y sello se sustituyen por otros de la MISMA
    campaña, bien formados, con entradas y protocolo falsos. La forma no puede verlo; la transacción, sí."""
    ruta, manifiesto, raiz = _campana_validada(tmp_path, campana_sellada)
    falso = campana_sellada.sello()
    falso["protocol"]["run_metadata"]["seed"] = 999999
    for k in ("code", "data", "governance"):
        falso["inputs"][k] = dict.fromkeys(RUTAS_FALSAS, "0" * 64)
        falso["counts"][k] = len(RUTAS_FALSAS)
    campana_sellada(raiz, campaign_id=json.loads(manifiesto.read_text(encoding="utf-8"))["campaign_id"], sello=falso)
    assert cm.publish_blocker(manifiesto) is None, "la forma sigue siendo válida: sólo la transacción puede delatarlo"
    ok, motivo = txn.publishable(ruta, manifest=manifiesto)
    assert not ok and "preflight_sha256" in motivo


@pytest.mark.parametrize(
    "campo,valor",
    [("git_sha", "b" * 40), ("dirty", True), ("dirty", "false"), ("campaign_id", "rederiv_aaaaaaa_20990101T000000")],
)
def test_RED_la_publicacion_cruza_campana_sha_y_dirty(tmp_path: Path, campana_sellada, campo: str, valor) -> None:
    ruta, manifiesto, _ = _campana_validada(tmp_path, campana_sellada)
    datos = json.loads(manifiesto.read_text(encoding="utf-8"))
    datos[campo] = valor
    manifiesto.write_text(json.dumps(datos), encoding="utf-8")
    ok, motivo = txn.publishable(ruta, manifest=manifiesto)
    assert not ok and campo in motivo


def test_el_sello_es_inmutable_en_la_transaccion(tmp_path: Path, campana_sellada) -> None:
    ruta, _, _ = _campana_validada(tmp_path, campana_sellada)
    assert "input_seal_sha256" in cs._IMMUTABLE
    actualizacion = {"input_seal_sha256": "f" * 64, "published_at": txn.now_rfc3339(), "release_sha": "c" * 40}
    with pytest.raises(ValueError, match="inmutable"):
        cs._transition(ruta, "published", actualizacion)


@pytest.mark.parametrize("valor", [None, "", "E" * 64, "sha256:" + "e" * 64, 17])
def test_RED_una_transaccion_sin_un_sello_valido_no_cumple_el_esquema(tmp_path: Path, campana_sellada, valor) -> None:
    ruta, _, _ = _campana_validada(tmp_path, campana_sellada)
    obj = cs.read(ruta) or {}
    if valor is None:
        del obj["input_seal_sha256"]
    else:
        obj["input_seal_sha256"] = valor
    assert any("input_seal_sha256" in p for p in cs.validate_schema(obj))


def test_RED_el_recibo_de_validacion_liga_el_mismo_sello(tmp_path: Path, campana_sellada) -> None:
    ruta, _, raiz = _campana_validada(tmp_path, campana_sellada)
    estado = cs.read(ruta) or {}
    with pytest.raises(txn.ReceiptError, match="input_seal_sha256"):
        txn.load_validation_receipt(_recibo(raiz, estado, input_seal_sha256="f" * 64), estado)
    with pytest.raises(txn.ReceiptError, match="faltan"):
        txn.load_validation_receipt(_recibo(raiz, estado, input_seal_sha256=None), estado)


def test_abrir_la_transaccion_exige_el_sello(tmp_path: Path) -> None:
    panel = tmp_path / "panel.csv"
    panel.write_text("x\n", encoding="utf-8")
    orden = ["--path", str(tmp_path / "c.json"), "open", "--campaign-id", "x", "--sha", "a" * 40]
    with pytest.raises(SystemExit):
        txn.main([*orden, "--dirty", "false", "--panel", str(panel)])


def test_el_runbook_abre_la_transaccion_con_el_sello_comparado_en_vivo() -> None:
    guion = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    vivo = "\n".join(ln for ln in guion.splitlines() if not ln.lstrip().startswith("#"))
    assert '--input-seal "$PREFLIGHT_SHA256"' in vivo
    assert vivo.index("cmp -s") < vivo.index("txn open")


def test_el_recibo_que_imprime_el_runbook_es_el_que_la_validacion_acepta(tmp_path: Path, campana_sellada) -> None:
    """★ Seguir la plantilla impresa al final del runbook produce un recibo ACEPTADO.

    ⚠️ Cazado antes del commit de R12: la plantilla seguía en `/1` y sin `input_seal_sha256`, así que la
    revisión humana que la siguiera al pie de la letra habría sido rechazada. Se parsea la plantilla, no se
    busca texto: lo que importa es que el recibo resultante pase `load_validation_receipt`.
    """
    _campana_validada(tmp_path, campana_sellada)
    estado = cs.read(tmp_path / "repo" / "reports" / "campaign" / "campaign.json")
    assert estado is not None
    guion = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8").splitlines()
    inicio = next(n for n, ln in enumerate(guion) if "escribe el recibo de revisión" in ln) + 1
    trozos = []
    for ln in guion[inicio:]:
        if not ln.startswith('echo "    #'):
            break
        trozos.append(ln[len('echo "    #') :].rstrip('"'))
    texto = "".join(trozos).replace('\\"', '"')
    for marca, valor in {
        "$CAMPAIGN_ID": estado["campaign_id"], "$CAMPAIGN_SHA": estado["source_git_sha"],
        "$PREFLIGHT_SHA256": estado["input_seal_sha256"], "<el del estado>": estado["panel_sha256"],
        "<persona>": "Javier Rebull", "<RFC3339>": txn.now_rfc3339(),
    }.items():  # fmt: skip
        texto = texto.replace(marca, valor)
    datos = json.loads(texto)
    assert set(datos) == txn.RECEIPT_KEYS and datos["schema"] == txn.RECEIPT_SCHEMA
    recibo = tmp_path / "plantilla.json"
    recibo.write_text(json.dumps(datos), encoding="utf-8")
    assert txn.load_validation_receipt(recibo, estado)["input_seal_sha256"] == estado["input_seal_sha256"]
