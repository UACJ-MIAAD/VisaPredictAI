"""M74-E-R13 · publicar exige el manifiesto en TODAS las puertas públicas, y `publish` re-acredita antes de escribir.

Auditoría del 15-sep sobre `32ad8a7` (sin informe escrito), reproducida por conducta: con un manifiesto falsificado,
`publishable(..., manifest=...)` bloqueaba, pero `publishable()` sin manifiesto aceptaba, `txn guard` sin `--manifest`
salía 0 y `txn publish` escribía `published` sin repetir la acreditación.

⚠️ Y un caso que el cierre mínimo habría dejado abierto, medido antes de tocar nada: un manifiesto que COPIA
`preflight_sha256` de la transacción con un sello falsificado en disco pasaba también `guard --manifest`, porque la
transacción sólo cruzaba campos del JSON y la acreditación del sello vivía aparte, en `campaign_manifest`.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from tests.test_sello_en_transaccion_m74e_r12 import RUTAS_FALSAS, _campana_validada
from tools import campaign_state as cs
from tools import campaign_txn as txn

RAIZ = Path(__file__).resolve().parents[1]
CASOS = ("otro_sello", "identidad_copiada", "ausente", "otra_campana", "sucio")


def _orden(ruta: Path, cmd: str, manifiesto: Path | None) -> list[str]:
    publicar = ["--release-sha", "b" * 40] if cmd == "publish" else []
    return ["--path", str(ruta), cmd, *publicar, *(["--manifest", str(manifiesto)] if manifiesto else [])]


def _sello_falso(campana_sellada) -> dict:
    falso = campana_sellada.sello()
    falso["protocol"]["run_metadata"]["seed"] = 999999
    for k in ("code", "data", "governance"):
        falso["inputs"][k] = dict.fromkeys(RUTAS_FALSAS, "0" * 64)
        falso["counts"][k] = len(RUTAS_FALSAS)
    return falso


def _forjar(caso: str, manifiesto: Path, raiz: Path, campana_sellada) -> None:
    """Falsifica DESPUÉS de `validated`, que es cuando la auditoría lo hizo."""
    datos = json.loads(manifiesto.read_text(encoding="utf-8"))
    if caso == "otro_sello":  # R12: manifiesto y sello bien formados de la MISMA campaña, con otro hash
        campana_sellada(raiz, campaign_id=datos["campaign_id"], sello=_sello_falso(campana_sellada))
    elif caso == "identidad_copiada":  # el manifiesto intacto —campos cruzados iguales—; el sello en disco, sustituido
        (raiz / datos["preflight"]).write_text(json.dumps(_sello_falso(campana_sellada)), encoding="utf-8")
    elif caso == "ausente":
        manifiesto.unlink()
    elif caso == "otra_campana":
        manifiesto.write_text(json.dumps({**datos, "campaign_id": "rederiv_aaaaaaa_20990101T000000"}), encoding="utf-8")
    elif caso == "sucio":
        manifiesto.write_text(json.dumps({**datos, "dirty": True}), encoding="utf-8")
    else:
        raise AssertionError(caso)


def test_RED_publishable_exige_el_manifiesto(tmp_path: Path, campana_sellada) -> None:
    """Sin manifiesto, la función que decide publicar ya no puede llamarse: no hay valor por defecto que lo omita."""
    ruta, _, _ = _campana_validada(tmp_path, campana_sellada)
    assert inspect.signature(txn.publishable).parameters["manifest"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        txn.publishable(ruta)  # type: ignore[call-arg]


@pytest.mark.parametrize("cmd", ["guard", "publish"])
def test_RED_sin_manifiesto_ni_guard_ni_publish_arrancan(tmp_path: Path, campana_sellada, cmd: str) -> None:
    ruta, _, _ = _campana_validada(tmp_path, campana_sellada)
    antes = cs.read(ruta)
    with pytest.raises(SystemExit) as salida:
        txn.main(_orden(ruta, cmd, None))
    assert salida.value.code == 2
    assert antes is not None and antes["status"] == "validated" and cs.read(ruta) == antes


@pytest.mark.parametrize("caso", CASOS)
@pytest.mark.parametrize("cmd", ["guard", "publish"])
def test_RED_un_manifiesto_falsificado_bloquea_guard_y_publish(
    tmp_path: Path, campana_sellada, cmd: str, caso: str
) -> None:
    ruta, manifiesto, raiz = _campana_validada(tmp_path, campana_sellada)
    antes = cs.read(ruta)
    _forjar(caso, manifiesto, raiz, campana_sellada)
    assert txn.main(_orden(ruta, cmd, manifiesto)) == txn.EXIT_BLOCKED
    assert antes is not None and cs.read(ruta) == antes, "bloquear no toca la transacción: sigue en validated"


def test_RED_la_identidad_copiada_con_sello_falso_la_caza_la_propia_transaccion(
    tmp_path: Path, campana_sellada
) -> None:
    """★ El caso que el cierre mínimo dejaba abierto: los cuatro campos cruzados coinciden y sólo el sello en disco miente."""
    ruta, manifiesto, raiz = _campana_validada(tmp_path, campana_sellada)
    _forjar("identidad_copiada", manifiesto, raiz, campana_sellada)
    ok, motivo = txn.publishable(ruta, manifest=manifiesto)
    assert not ok and "sello" in motivo, motivo


def test_control_guard_y_publish_con_el_manifiesto_legitimo(tmp_path: Path, campana_sellada) -> None:
    ruta, manifiesto, _ = _campana_validada(tmp_path, campana_sellada)
    assert txn.main(_orden(ruta, "guard", manifiesto)) == txn.EXIT_OK
    assert txn.main(_orden(ruta, "publish", manifiesto)) == txn.EXIT_OK
    assert (cs.read(ruta) or {})["status"] == "published"
    assert txn.main(_orden(ruta, "guard", manifiesto)) == txn.EXIT_BLOCKED, "publicar consume el permiso"


def test_RED_publish_consulta_la_puerta_antes_de_escribir(
    tmp_path: Path, campana_sellada, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El ORDEN es la garantía: con el sello falso `mark_published` ni se llama; con el legítimo, después de la puerta."""
    orden: list[str] = []
    puerta, escritura = txn.publishable, cs.mark_published

    def puerta_espiada(*a, **k):
        orden.append("puerta")
        return puerta(*a, **k)

    def escritura_espiada(*a, **k):
        orden.append("escritura")
        return escritura(*a, **k)

    monkeypatch.setattr(txn, "publishable", puerta_espiada)
    monkeypatch.setattr(cs, "mark_published", escritura_espiada)

    ruta, manifiesto, raiz = _campana_validada(tmp_path / "falso", campana_sellada)
    _forjar("identidad_copiada", manifiesto, raiz, campana_sellada)
    assert txn.main(_orden(ruta, "publish", manifiesto)) == txn.EXIT_BLOCKED
    assert orden == ["puerta"]

    orden.clear()
    ruta, manifiesto, _ = _campana_validada(tmp_path / "control", campana_sellada)
    assert txn.main(_orden(ruta, "publish", manifiesto)) == txn.EXIT_OK
    assert orden == ["puerta", "escritura"]


def test_RED_sync_all_pasa_el_manifiesto_a_guard_y_a_publish(tmp_path: Path) -> None:
    """Se EJECUTAN las dos funciones reales de `sync_all.sh` con un intérprete falso que registra sus argumentos."""
    guion = (RAIZ / "experiments" / "sync_all.sh").read_text(encoding="utf-8").splitlines()
    funciones = [ln for ln in guion if ln.startswith(("txn_guard()", "txn_publish()"))]
    assert len(funciones) == 2, funciones
    falso = tmp_path / "ante_nf" / "bin" / "python"
    falso.parent.mkdir(parents=True)
    falso.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$REGISTRO"\n', encoding="utf-8")
    falso.chmod(0o755)
    registro = tmp_path / "registro.txt"
    guion_minimo = "\n".join(
        ["MANIFEST=reports/campaign/campaign_manifest.json", "CAMPAIGN_TXN=reports/campaign/campaign.json", *funciones]
        + ["txn_guard", "txn_publish"]
    )
    fin = subprocess.run(
        ["bash", "-c", guion_minimo], cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "REGISTRO": str(registro), "HOME": str(tmp_path)},
    )  # fmt: skip
    llamadas = registro.read_text(encoding="utf-8").splitlines() if registro.exists() else []
    subcomandos = [next(p for p in ll.split() if p in ("guard", "publish")) for ll in llamadas]
    assert subcomandos == ["guard", "publish"], (llamadas, fin.stderr)
    for llamada in llamadas:
        assert "--manifest reports/campaign/campaign_manifest.json" in llamada, llamada
