"""G7 · desbloqueo (fase 2) — revalidar una madre `validated` con un `/3` nuevo, conservando el cómputo.

El recibo `/3` liga un árbol EXACTO. Si el candidato tiene que cambiar después de firmar (p. ej., para que el CI pase),
la madre validada sólo podía ir a `published` (con el árbol viejo) o a `failed` (perdiendo ~30 h de cómputo). La
revalidación es una operación EXPLÍCITA, no un `validated → validated` genérico:

* sólo sobre `validated` (`published` y `failed` siguen terminales), sin volver a sellar el cómputo;
* con un `/3` nuevo, acreditado en vivo contra la misma madre, el mismo cómputo y la reconciliación, y posterior al
  permiso que sustituye;
* conservando el permiso anterior: copia exacta del estado sustituido, del recibo anterior y un registro que encadena
  anterior → nuevo, creados ANTES de promocionar el estado y bajo el lock de la campaña;
* con CAS sobre la revisión y la huella de los bytes del estado; la revisión sube exactamente una vez;
* el permiso anterior deja de ser consumible, y un registro preparado sin promocionar (interrupción) cierra la puerta
  hasta completar la revalidación.

Mundos sintéticos con las APIs reales (máquina de estados, vínculo, reconciliación con los lectores reales de E3).
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from tests.test_g7_publicacion_reconciliada import (  # noqa: F401 — fixtures y ayudantes
    KF,
    _commit,
    _git,
    _guard,
    _recibo3,
    _squash_con_el_mismo_arbol,
    _validar_cli,
    candidato,
    cerrada,
    validada,
)
from tests.test_g7_reconciliacion import (  # noqa: F401 — fixtures que encadena `cerrada`
    _cerrado,
    _codigo_del_checkout,
    _ensamblar,
    _resellar_plan,
    c5,
    cab,
    con_e3,
    cx,
    m5,
    mundo,
    rc,
    rm,
    score,
)
from tools import campaign_state as cs
from tools import campaign_txn as txn


def _manifiesto(m: dict) -> Path:
    return m["txn"].with_name("campaign_manifest.json")


def _otro_candidato(m: dict, nombre: str = "ci") -> str:
    """Un candidato NUEVO con los mismos reportes (p. ej., pruebas portables): otro commit y otro árbol."""
    (m["cand"] / "tests").mkdir(exist_ok=True)
    (m["cand"] / "tests" / f"{nombre}.txt").write_text(f"{nombre}\n", encoding="utf-8")
    return _commit(m["cand"], f"candidato {nombre}")


def _recibo_nuevo(m: dict, destino: Path, **cambios: str) -> Path:
    """Un /3 escrito DESPUÉS del permiso vigente (la marca de tiempo tiene resolución de segundos)."""
    import datetime as dt

    vigente = (cs.read(m["txn"]) or {})["validated_at"]
    despues = dt.datetime.fromisoformat(vigente.replace("Z", "+00:00")) + dt.timedelta(minutes=1)
    return _recibo3(m, destino, **{"reviewed_at": despues.strftime("%Y-%m-%dT%H:%M:%SZ"), **cambios})


def _revalidar_cli(m: dict, recibo: Path, monkeypatch, capsys) -> tuple[int, str]:
    monkeypatch.setattr(txn, "_run_consistency", lambda: (True, ""))
    capsys.readouterr()
    rc_ = txn.main(["--path", str(m["txn"]), "revalidate", "--receipt", str(recibo)])
    return rc_, capsys.readouterr().err


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _historial(m: dict) -> Path:
    return m["txn"].parent / cs.HISTORIAL


@pytest.fixture
def revalidable(validada: dict, tmp_path: Path) -> dict:  # noqa: F811 — la fixture importada
    """La madre validada sobre el candidato 1, el candidato 2 ya commiteado y su `/3` escrito (sin revalidar)."""
    m = dict(validada)
    m["arbol1"] = _git(m["cand"], "rev-parse", "HEAD^{tree}")
    m["estado1"] = m["txn"].read_bytes()
    _otro_candidato(m)
    m["arbol2"] = _git(m["cand"], "rev-parse", "HEAD^{tree}")
    m["recibo2"] = _recibo_nuevo(m, tmp_path / "recibo3_nuevo.json")
    return m


# ═══════════════════════════════ control
def test_control_revalidar_conserva_el_computo_y_solo_publica_el_arbol_nuevo(
    revalidable: dict, monkeypatch, capsys
) -> None:
    m = revalidable
    computado = (m["txn"].parent / cs.COMPUTED_SNAPSHOT).read_bytes()
    antes = cs.read(m["txn"]) or {}
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    assert rc_ == 0, err
    despues = cs.read(m["txn"]) or {}
    assert despues["status"] == "validated" and despues["revision"] == antes["revision"] + 1
    assert despues["computed_sha256"] == antes["computed_sha256"]
    assert (m["txn"].parent / cs.COMPUTED_SNAPSHOT).read_bytes() == computado
    assert despues["validation_receipt_sha256"] == _sha(m["recibo2"])
    # el permiso anterior se conserva byte a byte en el historial
    h = _historial(m)
    rev = antes["revision"]
    assert (h / f"r{rev:04d}.campaign.json").read_bytes() == m["estado1"]
    assert _sha(h / f"r{rev:04d}.recibo.json") == antes["validation_receipt_sha256"]
    registro = json.loads((h / f"r{rev:04d}-r{rev + 1:04d}.revalidation.json").read_text())
    assert registro["previous_receipt_sha256"] == antes["validation_receipt_sha256"]
    assert registro["receipt_sha256"] == despues["validation_receipt_sha256"]
    assert registro["from_state_sha256"] == hashlib.sha256(m["estado1"]).hexdigest()
    ok, motivo = _guard(m)
    assert ok, motivo
    # el árbol viejo ya no publica; el nuevo sí
    viejo = _git(m["cand"], "commit-tree", m["arbol1"], "-m", "squash viejo")
    with pytest.raises(txn.PublicationBlocked, match="árbol"):
        txn.publish(m["txn"], manifest=_manifiesto(m), release_sha=viejo)
    nuevo = _squash_con_el_mismo_arbol(m)
    doc = txn.publish(m["txn"], manifest=_manifiesto(m), release_sha=nuevo)
    assert doc["status"] == "published" and doc["revision"] == despues["revision"] + 1


def test_control_dos_revalidaciones_sucesivas_encadenan_el_historial(revalidable: dict, tmp_path, monkeypatch, capsys):
    m = revalidable
    assert _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)[0] == 0
    _otro_candidato(m, "ci2")
    rc_, err = _revalidar_cli(m, _recibo_nuevo(m, tmp_path / "recibo3_tercero.json"), monkeypatch, capsys)
    assert rc_ == 0, err
    assert len(list(_historial(m).glob("*.revalidation.json"))) == 2
    ok, motivo = _guard(m)
    assert ok, motivo


# ═══════════════════════════════ la operación es explícita y no abre `validated → validated` genérico
def test_validate_sigue_sin_aceptar_una_madre_ya_validada(revalidable: dict, monkeypatch, capsys) -> None:
    rc_, err = _validar_cli(revalidable, revalidable["recibo2"], monkeypatch, capsys)
    assert rc_ != 0 and "validated" in err, err


def test_la_maquina_no_admite_validated_a_validated_por_la_transicion_generica(revalidable: dict) -> None:
    with pytest.raises(ValueError, match="no permitida"):
        cs._transition(revalidable["txn"], "validated", {"reviewed_by": "Otra Persona"})


@pytest.mark.parametrize("estado", ["published", "failed"])
def test_un_estado_terminal_no_se_revalida(revalidable: dict, monkeypatch, capsys, estado: str) -> None:
    m = revalidable
    if estado == "published":
        _git(
            m["cand"], "reset", "-q", "--hard", "HEAD~1"
        )  # el candidato vuelve al commit que el permiso vigente revisó
        txn.publish(
            m["txn"], manifest=_manifiesto(m), release_sha=_git(m["cand"], "commit-tree", m["arbol1"], "-m", "s")
        )
    else:
        cs.mark_failed(m["txn"], failed_stage="prueba", failed_at=txn.now_rfc3339(), reason="cerrar")
    antes = m["txn"].read_bytes()
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    assert rc_ != 0 and estado in err, err
    assert m["txn"].read_bytes() == antes and not _historial(m).exists()


# ═══════════════════════════════ el recibo nuevo
def test_un_recibo_reciclado_no_revalida(revalidable: dict, monkeypatch, capsys) -> None:
    rc_, err = _revalidar_cli(revalidable, revalidable["recibo"], monkeypatch, capsys)
    assert rc_ != 0 and "recicla" in err, err


def test_un_recibo_anterior_al_permiso_que_sustituye_no_revalida(revalidable: dict, tmp_path, monkeypatch, capsys):
    m = revalidable
    validado = (cs.read(m["txn"]) or {})["validated_at"]
    recibo = _recibo3(m, tmp_path / "r_viejo.json", reviewed_at=validado)
    rc_, err = _revalidar_cli(m, recibo, monkeypatch, capsys)
    assert rc_ != 0 and "posterior" in err, err


@pytest.mark.parametrize(
    ("cambio", "patron"),
    [
        ({"schema": "campaign-validation-receipt/2"}, "exige el /3"),
        ({"decision": "rechazada"}, "RECHAZA"),
        ({"campaign_id": "rederiv_otra"}, "campaign_id"),
        ({"candidate_tree_sha": "0" * 40}, "candidato"),
        ({"reconciliation_sha256": "0" * 64}, "reconciliación"),
        ({"mother_computed_sha256": "0" * 64}, "cómputo"),
    ],
)
def test_un_recibo_que_no_acredita_no_revalida(revalidable: dict, tmp_path, monkeypatch, capsys, cambio, patron):
    m = revalidable
    antes = m["txn"].read_bytes()
    recibo = tmp_path / "r.json"
    datos = {**json.loads(m["recibo2"].read_text()), **cambio}
    if cambio.get("schema") == "campaign-validation-receipt/2":
        datos = {k: v for k, v in datos.items() if k in txn.RECEIPT_KEYS}
    recibo.write_text(json.dumps(datos), encoding="utf-8")
    rc_, err = _revalidar_cli(m, recibo, monkeypatch, capsys)
    assert rc_ != 0 and patron in err, err
    assert m["txn"].read_bytes() == antes and not _historial(m).exists()


def test_un_candidato_que_no_es_la_reconciliacion_no_revalida(revalidable: dict, tmp_path, monkeypatch, capsys):
    m = revalidable
    cab._escribir(m["cand"], KF, '{"crps_fad": 999}\n')
    _commit(m["cand"], "otra cifra")
    rc_, err = _revalidar_cli(m, _recibo_nuevo(m, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "distintos" in err, err


def test_el_permiso_anterior_alterado_impide_revalidar(revalidable: dict, monkeypatch, capsys) -> None:
    """El recibo que se sustituye tiene que seguir siendo el que el estado sella: su copia es la evidencia."""
    m = revalidable
    m["recibo"].write_text(m["recibo"].read_text().replace("Javier Rebull", "Javier  Rebull"), encoding="utf-8")
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    assert rc_ != 0 and "anterior" in err, err


def test_la_evidencia_del_computo_alterada_impide_revalidar(revalidable: dict, monkeypatch, capsys) -> None:
    m = revalidable
    copia = m["txn"].parent / cs.COMPUTED_SNAPSHOT
    copia.chmod(0o644)
    copia.write_bytes(copia.read_bytes().replace(b'"computed"', b'"computed" '))
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    assert rc_ != 0 and "cómputo" in err, err


def test_revalidar_registra_el_hash_de_los_bytes_que_acredito(revalidable: dict, monkeypatch, capsys) -> None:
    """El recibo cambia en disco DESPUÉS de leerse: lo registrado es lo acreditado, y la puerta lo nota."""
    m = revalidable
    leidos, real = [], Path.read_bytes
    original = m["recibo2"].read_bytes()

    def una_lectura(self, *a, **k):
        datos = real(self, *a, **k)
        if self == m["recibo2"].resolve() and not leidos:
            leidos.append(datos)
            self.write_bytes(original.replace(b"Javier Rebull", b"Otra Persona"))
        return datos

    monkeypatch.setattr(Path, "read_bytes", una_lectura)
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    monkeypatch.undo()
    assert leidos and rc_ == 0, err
    assert (cs.read(m["txn"]) or {})["validation_receipt_sha256"] == hashlib.sha256(original).hexdigest()
    ok, motivo = _guard(m)
    assert not ok and "cambió" in motivo, motivo


# ═══════════════════════════════ CAS: revisión Y huella de los bytes
def _args(m: dict, recibo: Path) -> dict:
    acta = json.loads(recibo.read_text())
    return {
        "validation_receipt_sha256": _sha(recibo),
        "validation_receipt_path": str(recibo.resolve()),
        "reviewed_by": acta["reviewed_by"],
        "decision": acta["decision"],
        "validated_at": txn.now_rfc3339(),
    }


def test_una_revision_obsoleta_no_revalida(revalidable: dict) -> None:
    m = revalidable
    estado = m["txn"].read_bytes()
    obj = json.loads(estado)
    with pytest.raises(ValueError, match="revisi"):
        cs.revalidate(m["txn"], **_args(m, m["recibo2"]), expected_revision=obj["revision"] - 1,
                      expected_state_sha256=hashlib.sha256(estado).hexdigest())  # fmt: skip
    assert m["txn"].read_bytes() == estado


def test_un_estado_con_la_misma_revision_y_otros_bytes_no_revalida(revalidable: dict) -> None:
    """CAS sobre la huella: una reescritura que conserva `revision` también se rechaza."""
    m = revalidable
    acreditado = m["txn"].read_bytes()
    obj = json.loads(acreditado)
    cs.atomic_write(m["txn"], {**obj, "decision": "aprobada "})  # misma revisión, otro contenido
    with pytest.raises(ValueError, match="huella"):
        cs.revalidate(m["txn"], **_args(m, m["recibo2"]), expected_revision=obj["revision"],
                      expected_state_sha256=hashlib.sha256(acreditado).hexdigest())  # fmt: skip


def test_dos_revalidaciones_concurrentes_sobre_la_misma_revision_solo_una_gana(revalidable: dict, tmp_path) -> None:
    m = revalidable
    _otro_candidato(m, "rival")
    recibo3 = _recibo_nuevo(m, tmp_path / "rival.json")
    estado = m["txn"].read_bytes()
    obj = json.loads(estado)
    salida: list[object] = []
    barrera = threading.Barrier(2)

    def correr(recibo: Path) -> None:
        barrera.wait()
        try:
            salida.append(cs.revalidate(m["txn"], **_args(m, recibo), expected_revision=obj["revision"],
                                        expected_state_sha256=hashlib.sha256(estado).hexdigest()))  # fmt: skip
        except ValueError as exc:
            salida.append(exc)

    hilos = [threading.Thread(target=correr, args=(r,)) for r in (m["recibo2"], recibo3)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    ganadas = [s for s in salida if isinstance(s, dict)]
    assert len(ganadas) == 1 and len(salida) == 2, salida
    final = cs.read(m["txn"]) or {}
    assert final["revision"] == obj["revision"] + 1
    assert len(list(_historial(m).glob("*.revalidation.json"))) == 1


def test_revalidar_frente_a_publicar_nunca_hace_ambas_cosas(revalidable: dict) -> None:
    m = revalidable
    estado = m["txn"].read_bytes()
    obj = json.loads(estado)
    viejo = _git(m["cand"], "commit-tree", m["arbol1"], "-m", "squash viejo")
    salida: dict[str, object] = {}
    barrera = threading.Barrier(2)

    def publicar() -> None:
        barrera.wait()
        try:
            salida["publicar"] = txn.publish(m["txn"], manifest=_manifiesto(m), release_sha=viejo)
        except (ValueError, txn.PublicationBlocked) as exc:
            salida["publicar"] = exc

    def revalidar() -> None:
        barrera.wait()
        try:
            salida["revalidar"] = cs.revalidate(m["txn"], **_args(m, m["recibo2"]), expected_revision=obj["revision"],
                                                expected_state_sha256=hashlib.sha256(estado).hexdigest())  # fmt: skip
        except ValueError as exc:
            salida["revalidar"] = exc

    hilos = [threading.Thread(target=publicar), threading.Thread(target=revalidar)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    exitos = [k for k, v in salida.items() if isinstance(v, dict)]
    assert len(exitos) == 1, salida
    final = cs.read(m["txn"]) or {}
    if exitos == ["publicar"]:
        assert final["status"] == "published" and not _historial(m).exists()
    else:
        assert final["status"] == "validated" and final["validation_receipt_sha256"] == _sha(m["recibo2"])


# ═══════════════════════════════ interrupción y recuperación
def test_una_caida_entre_historial_y_estado_cierra_la_puerta_hasta_completar(
    revalidable: dict, tmp_path, monkeypatch, capsys
) -> None:
    m = revalidable
    antes = m["txn"].read_bytes()

    def caida(*_a, **_k):
        raise OSError("caída simulada al promocionar el estado")

    monkeypatch.setattr(cs, "atomic_write", caida)
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    monkeypatch.undo()
    assert rc_ != 0 and m["txn"].read_bytes() == antes, err
    assert list(_historial(m).glob("*.revalidation.json")), "el registro se prepara ANTES de promocionar"
    # el registro huérfano no autoriza el permiso nuevo NI revive el anterior
    ok, motivo = _guard(m)
    assert not ok and "sin promocionar" in motivo, motivo
    # otro recibo no puede pisar la revalidación preparada
    _otro_candidato(m, "intruso")
    rc_, err = _revalidar_cli(m, _recibo_nuevo(m, tmp_path / "otro.json"), monkeypatch, capsys)
    assert rc_ != 0 and "preparada" in err, err
    # el reintento con el mismo recibo completa (y la puerta vuelve a autorizar sólo el árbol de ese recibo)
    _git(m["cand"], "reset", "-q", "--hard", "HEAD~1")
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    assert rc_ == 0, err
    ok, motivo = _guard(m)
    assert ok, motivo


def test_una_caida_al_escribir_el_registro_no_deja_un_registro_a_medias(revalidable: dict, monkeypatch, capsys):
    m = revalidable
    real = cs._crear_exacto

    def caida_en_registro(destino: Path, datos: bytes) -> None:
        if destino.name.endswith(".revalidation.json"):
            raise OSError("caída simulada al escribir el registro")
        real(destino, datos)

    monkeypatch.setattr(cs, "_crear_exacto", caida_en_registro)
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    monkeypatch.undo()
    assert rc_ != 0, err
    assert not list(_historial(m).glob("*.revalidation.json"))
    # sin registro no hay revalidación: el permiso vigente sigue siendo el anterior y su historia se acredita
    assert m["txn"].read_bytes() == m["estado1"]
    assert cs.historial_problemas(m["txn"], cs.read(m["txn"]) or {}) == []
    assert _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)[0] == 0
    ok, motivo = _guard(m)
    assert ok, motivo


# ═══════════════════════════════ el historial es evidencia: la puerta lo acredita
@pytest.fixture
def revalidada(revalidable: dict, monkeypatch, capsys) -> dict:
    rc_, err = _revalidar_cli(revalidable, revalidable["recibo2"], monkeypatch, capsys)
    assert rc_ == 0, err
    return revalidable


def _el_registro(m: dict) -> Path:
    return next(_historial(m).glob("*.revalidation.json"))


@pytest.mark.parametrize("pieza", ["registro", "estado_previo", "recibo_previo"])
def test_un_historial_alterado_cierra_la_puerta(revalidada: dict, pieza: str) -> None:
    m = revalidada
    h = _historial(m)
    destino = {"registro": _el_registro(m), "estado_previo": next(h.glob("r*.campaign.json")),
               "recibo_previo": next(h.glob("r*.recibo.json"))}[pieza]  # fmt: skip
    destino.chmod(0o644)
    destino.write_bytes(destino.read_bytes().replace(b"Javier Rebull", b"Javier  Rebull"))
    ok, motivo = _guard(m)
    assert not ok and "historial" in motivo, motivo


def test_un_registro_borrado_cierra_la_puerta(revalidada: dict) -> None:
    _el_registro(revalidada).unlink()
    ok, motivo = _guard(revalidada)
    assert not ok and "historial" in motivo, motivo


def test_volver_a_mano_al_estado_anterior_no_revive_el_permiso_viejo(revalidada: dict) -> None:
    m = revalidada
    m["txn"].write_bytes(m["estado1"])  # «rollback» manual al estado sustituido
    ok, motivo = _guard(m)
    assert not ok and "sin promocionar" in motivo, motivo


# ═══════════════════════════════ controles adicionales del plan (fase 2)
def test_revalidar_frente_a_fallar_nunca_revalida_un_estado_terminal(revalidable: dict) -> None:
    m = revalidable
    estado = m["txn"].read_bytes()
    obj = json.loads(estado)
    salida: dict[str, object] = {}
    barrera = threading.Barrier(2)

    def fallar() -> None:
        barrera.wait()
        try:
            salida["fallar"] = cs.mark_failed(m["txn"], failed_stage="prueba", failed_at=txn.now_rfc3339(), reason="x")
        except ValueError as exc:
            salida["fallar"] = exc

    def revalidar() -> None:
        barrera.wait()
        try:
            salida["revalidar"] = cs.revalidate(
                m["txn"], **_args(m, m["recibo2"]), expected_revision=obj["revision"],
                expected_state_sha256=hashlib.sha256(estado).hexdigest(),
            )  # fmt: skip
        except ValueError as exc:
            salida["revalidar"] = exc

    hilos = [threading.Thread(target=fallar), threading.Thread(target=revalidar)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    final = cs.read(m["txn"]) or {}
    assert isinstance(salida["fallar"], dict), salida  # fallar siempre puede: validated → failed es legal
    assert final["status"] == "failed"
    if isinstance(salida["revalidar"], dict):  # revalidó primero: la revisión subió una vez y luego falló
        assert final["revision"] == obj["revision"] + 2
    else:
        assert final["revision"] == obj["revision"] + 1 and not list(_historial(m).glob("*.revalidation.json"))


def test_una_caida_al_copiar_el_estado_no_compromete_nada(revalidable: dict, monkeypatch, capsys) -> None:
    m = revalidable
    real = cs._crear_exacto

    def caida_en_estado(destino: Path, datos: bytes) -> None:
        if destino.name.endswith(".campaign.json"):
            raise OSError("caída simulada al copiar el estado sustituido")
        real(destino, datos)

    monkeypatch.setattr(cs, "_crear_exacto", caida_en_estado)
    rc_, err = _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)
    monkeypatch.undo()
    assert rc_ != 0 and m["txn"].read_bytes() == m["estado1"], err
    assert cs.historial_problemas(m["txn"], cs.read(m["txn"]) or {}) == []
    assert _revalidar_cli(m, m["recibo2"], monkeypatch, capsys)[0] == 0


def test_un_recibo_con_otro_manifiesto_no_revalida(revalidable: dict, tmp_path, monkeypatch, capsys) -> None:
    m = revalidable
    recibo = _recibo_nuevo(m, tmp_path / "r.json", release_manifest_sha256="0" * 64)
    rc_, err = _revalidar_cli(m, recibo, monkeypatch, capsys)
    assert rc_ != 0 and "manifiesto" in err, err


def test_publicar_rechaza_una_reescritura_con_la_misma_revision(revalidable: dict, monkeypatch) -> None:
    """CAS sobre la huella en `publish`: entre la puerta y la escritura alguien reescribe el estado sin subir `revision`."""
    m = revalidable
    _git(m["cand"], "reset", "-q", "--hard", "HEAD~1")
    puerta_real = txn._puerta

    def puerta_y_reescritura(*a, **k):
        resultado = puerta_real(*a, **k)
        obj = json.loads(m["txn"].read_bytes())
        cs.atomic_write(m["txn"], {**obj, "decision": obj["decision"] + " "})  # misma revisión, otro contenido
        return resultado

    monkeypatch.setattr(txn, "_puerta", puerta_y_reescritura)
    squash = _git(m["cand"], "commit-tree", m["arbol1"], "-m", "squash")
    with pytest.raises(ValueError, match="huella"):
        txn.publish(m["txn"], manifest=_manifiesto(m), release_sha=squash)
    assert (cs.read(m["txn"]) or {})["status"] == "validated"


def test_el_historial_es_create_only_y_solo_admite_el_mismo_contenido(tmp_path: Path) -> None:
    destino = tmp_path / "r0002.recibo.json"
    cs._crear_exacto(destino, b"uno\n")
    cs._crear_exacto(destino, b"uno\n")  # reintento idéntico: aceptado
    with pytest.raises(ValueError, match="otros bytes"):
        cs._crear_exacto(destino, b"dos\n")
    assert destino.read_bytes() == b"uno\n" and not list(tmp_path.glob(".hist.*"))
