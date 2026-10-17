"""M74-F · G7 · B5a — publicar una campaña reconciliada sin saltarse su transacción.

RED (paso 1 de B5a, 16-oct): la única vía a publicar es `campaign_txn.publish` sobre la madre, pero validarla reescribe
su `campaign.json`, y complementarias y reconciliaciones fijan el hash de ese archivo: validar la madre las dejaba
sin acreditar a todas. Además, una madre `published` no era una madre admitida.

Diseño (`Prompts/…/b5a/DISENO_PUBLICACION_RECONCILIADA_2026-10-16.md`): al pasar a `validated`, la máquina guarda una
copia inmutable y exacta del `campaign.json` en `computed` (la EVIDENCIA del cómputo) y los lectores acreditan esa copia
contra el estado vivo. El hash original se conserva; ninguna comparación se relaja a `campaign_id`.

Mundos sintéticos con las APIs reales (máquina de estados, lectores de complementarias y reconciliación con los
lectores reales de E3). La madre protegida no se toca.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.test_g7_reconciliacion import (  # noqa: F401 — fixtures
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

T_VAL = "2026-10-16T20:00:00+00:00"
T_PUB = "2026-10-16T21:00:00+00:00"


def _lectores() -> dict:
    return {"acreditar_lanes": score.acreditar_recibos,
            "puntuar_e3": lambda src, madre: score.puntuar(root=src, complemento=madre)}  # fmt: skip


@pytest.fixture
def cerrada(con_e3: dict, capsys) -> dict:  # noqa: F811 — la fixture importada
    """Madre `computed`, C4/C5/C6 `accredited` y una reconciliación `derived` sobre las tres."""
    rc_, out = _ensamblar(con_e3, capsys, con_e3["c5"], con_e3["c6"], con_e3["c4"])
    assert rc_ == 0, out
    _cerrado(con_e3)
    m = {**con_e3, "txn": con_e3["madre"] / cx.CAMPANA_REL}
    m["sha_computado"] = cx.leer_padre(m["madre"])["campaign_json_sha256"]
    m["bytes_computado"] = m["txn"].read_bytes()
    _toda_la_cadena_se_acredita(m)  # control
    return m


def _toda_la_cadena_se_acredita(m: dict) -> None:
    """La reconciliación re-acredita la madre, su evidencia, sus capas y CADA fuente (C4, C5, C6) con su lector."""
    rc.acreditar(m["recon"], parent_root=m["madre"], **_lectores())


def _validar(m: dict) -> dict:
    return cs.mark_validated(m["txn"], validation_receipt_sha256="d" * 64, validation_receipt_path="/recibo.json",
                             reviewed_by="Javier Rebull", validated_at=T_VAL, decision="aprobada")  # fmt: skip


def _publicar(m: dict) -> dict:
    return cs._transition(m["txn"], "published", {"published_at": T_PUB, "release_sha": "e" * 40})


def _codigos_padre(m: dict) -> set[str]:
    try:
        cx.leer_padre(m["madre"])
    except cx.ComplementoError as exc:
        return {c for c, _ in exc.problemas}
    return set()


# ═══════════════════════════════ condición 1 · la evidencia del cómputo sobrevive a la publicación
def test_validar_la_madre_no_rompe_la_cadena_g7(cerrada: dict) -> None:
    """RED: el hash que fijan C4/C5/C6 y la reconciliación es el del cómputo, y validar no puede cambiarlo."""
    _validar(cerrada)
    assert cx.leer_padre(cerrada["madre"])["campaign_json_sha256"] == cerrada["sha_computado"]
    _toda_la_cadena_se_acredita(cerrada)


def test_la_evidencia_del_computo_es_la_copia_exacta_y_su_hash_queda_en_el_estado(cerrada: dict) -> None:
    doc = _validar(cerrada)
    copia = cerrada["txn"].parent / cs.COMPUTED_SNAPSHOT
    assert copia.read_bytes() == cerrada["bytes_computado"]
    assert doc["computed_sha256"] == cerrada["sha_computado"]
    assert cerrada["txn"].read_bytes() != cerrada["bytes_computado"], "el estado vivo sí avanza"


# ═══════════════════════════════ condición 3 · el ciclo completo, también `published`
def test_publicar_la_madre_no_la_saca_de_los_lectores(cerrada: dict) -> None:
    _validar(cerrada)
    _publicar(cerrada)
    padre = cx.leer_padre(cerrada["madre"])
    assert padre["status"] == "published" and padre["campaign_json_sha256"] == cerrada["sha_computado"]
    _toda_la_cadena_se_acredita(cerrada)


# ═══════════════════════════════ adversariales de la evidencia
def test_una_copia_del_computo_tocada_no_se_acredita(cerrada: dict) -> None:
    _validar(cerrada)
    copia = cerrada["txn"].parent / cs.COMPUTED_SNAPSHOT
    assert not os.access(copia, os.W_OK), "la copia nace de sólo lectura"
    copia.chmod(0o644)  # quien la toca tiene que quitarle la protección primero
    copia.write_bytes(copia.read_bytes().replace(b'"revision"', b'"revision" ', 1))
    assert "padre-evidencia" in _codigos_padre(cerrada)


def test_sin_la_copia_del_computo_una_madre_validada_no_se_acredita(cerrada: dict) -> None:
    _validar(cerrada)
    (cerrada["txn"].parent / cs.COMPUTED_SNAPSHOT).unlink()
    assert "padre-evidencia" in _codigos_padre(cerrada)


def _reescribir_vivo(m: dict, **cambios) -> None:
    doc = json.loads(m["txn"].read_text())
    doc.update(cambios)
    m["txn"].write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def test_un_estado_vivo_que_ya_no_describe_el_computo_no_se_acredita(cerrada: dict) -> None:
    """Copia intacta, pero el estado vivo cuenta OTRO cómputo (otra hora de cierre): no son la misma corrida."""
    _validar(cerrada)
    _reescribir_vivo(cerrada, completed_at="2026-10-16T19:59:00+00:00")
    assert "padre-evidencia" in _codigos_padre(cerrada)


def test_un_hash_de_computo_forjado_no_se_acredita(cerrada: dict) -> None:
    _validar(cerrada)
    _reescribir_vivo(cerrada, computed_sha256="0" * 64)
    assert "padre-evidencia" in _codigos_padre(cerrada)


def test_una_copia_previa_con_otros_bytes_impide_validar(cerrada: dict) -> None:
    """Create-only: si ya hay una «evidencia» que no es este cómputo, la transición se niega y nada cambia."""
    copia = cerrada["txn"].parent / cs.COMPUTED_SNAPSHOT
    copia.write_text('{"otra": "evidencia"}\n')
    with pytest.raises(ValueError, match="evidencia del cómputo"):
        _validar(cerrada)
    assert cerrada["txn"].read_bytes() == cerrada["bytes_computado"]


def test_la_evidencia_no_existe_mientras_la_madre_esta_en_computed(cerrada: dict) -> None:
    assert not (cerrada["txn"].parent / cs.COMPUTED_SNAPSHOT).exists()
    assert "computed_sha256" not in json.loads(cerrada["txn"].read_text())


def test_archivar_la_transaccion_se_lleva_su_evidencia(cerrada: dict, tmp_path) -> None:
    """Si la copia se quedara, la SIGUIENTE campaña heredaría una evidencia ajena."""
    from tools import campaign_txn as txn

    _validar(cerrada)
    cs.mark_failed(cerrada["txn"], failed_stage="prueba", failed_at=T_PUB, reason="archivar")
    final = txn.archive_if_terminal(cerrada["txn"], tmp_path / "archivo")
    assert final is not None and not (cerrada["txn"].parent / cs.COMPUTED_SNAPSHOT).exists()
    assert (final.parent / f"{final.stem}.computed.json").read_bytes() == cerrada["bytes_computado"]


# ═══════════════════════════════ condiciones 2 y 4 · la autorización ligada al resultado y al candidato
# RED: el recibo `campaign-validation-receipt/3` no existe; `validate` sólo liga la identidad de la madre, `guard` sólo
# el `git_dirty` SELLADO y `publish` acepta cualquier commit como `release_sha`.
import hashlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import run_reconciliation as cli_rc  # noqa: E402

from tools import campaign_txn as txn  # noqa: E402
from vp_model import vinculo_publicacion as vp  # noqa: E402

INTERNOS = ("reports/campaign/reconciliation", "reports/campaign/complement_evidence")
MANIFIESTO_RELEASE = "reports/release/release_manifest.json"


def _git(raiz: Path, *args: str) -> str:
    fin = subprocess.run(["git", "-C", str(raiz), "-c", "user.name=Prueba", "-c", "user.email=p@ejemplo.org", *args],
                         capture_output=True, text=True, check=True)  # fmt: skip
    return fin.stdout.strip()


def _commit(raiz: Path, mensaje: str) -> str:
    _git(raiz, "add", "-A")
    _git(raiz, "commit", "-q", "-m", mensaje)
    return _git(raiz, "rev-parse", "HEAD")


@pytest.fixture
def candidato(cerrada: dict, tmp_path: Path) -> dict:
    return _con_candidato(cerrada, tmp_path / "candidato")


def _con_candidato(cerrada: dict, cand: Path) -> dict:
    """La rama de integración: `reports/` de la reconciliación (sin lo interno ni lo que el contrato de exportación
    destina a otro sitio) + manifiesto y tarjeta del corte."""
    cand.mkdir()
    _git(cand, "init", "-q")
    for f in sorted((cerrada["recon"] / "reports").rglob("*")):
        rel = str(f.relative_to(cerrada["recon"]))
        if f.is_file() and not rel.startswith(INTERNOS) and rel != str(rc.RUTA_REL) and rel not in vp.EXPORTACION:
            (cand / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, cand / rel)
    cab._escribir(cand, MANIFIESTO_RELEASE, '{"release_id": "2026-10-prueba-0123456789ab"}\n')
    cab._escribir(cand, "reports/governance/MODEL_CARD.md", "# tarjeta del corte\n")
    _commit(cand, "candidato")
    return {**cerrada, "cand": cand}


def _sha_archivo(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _recibo3(m: dict, destino: Path, **cambios: str) -> Path:
    estado = cs.read(m["txn"]) or {}
    datos = {
        "schema": "campaign-validation-receipt/3",
        **{k: estado[k] for k in ("campaign_id", "source_git_sha", "panel_sha256", "input_seal_sha256")},
        "reviewed_by": "Javier Rebull",
        "decision": "aprobada",
        "reviewed_at": txn.now_rfc3339(),
        "mother_computed_sha256": m["sha_computado"],
        "reconciliation_root": str(m["recon"].resolve()),
        "reconciliation_id": json.loads((m["recon"] / rc.RUTA_REL).read_text())["reconciliation_id"],
        "reconciliation_sha256": _sha_archivo(m["recon"] / rc.RUTA_REL),
        "candidate_root": str(m["cand"].resolve()),
        "candidate_git_sha": _git(m["cand"], "rev-parse", "HEAD"),
        "candidate_tree_sha": _git(m["cand"], "rev-parse", "HEAD^{tree}"),
        "release_manifest_sha256": _sha_archivo(m["cand"] / MANIFIESTO_RELEASE),
        "release_id": "2026-10-prueba-0123456789ab",
    }
    datos.update(cambios)
    destino.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return destino


def _validar_cli(m: dict, recibo: Path, monkeypatch, capsys) -> tuple[int, str]:
    monkeypatch.setattr(txn, "_run_consistency", lambda: (True, ""))
    capsys.readouterr()
    rc_ = txn.main(["--path", str(m["txn"]), "validate", "--receipt", str(recibo)])
    return rc_, capsys.readouterr().err


def _guard(m: dict) -> tuple[bool, str]:
    return txn.publishable(m["txn"], manifest=m["txn"].with_name("campaign_manifest.json"))


def _squash_con_el_mismo_arbol(m: dict) -> str:
    """Lo que deja un squash-merge fiel: OTRO commit con EXACTAMENTE el árbol revisado."""
    arbol = _git(m["cand"], "rev-parse", "HEAD^{tree}")
    return _git(m["cand"], "commit-tree", arbol, "-m", "squash")


@pytest.fixture
def validada(candidato: dict, tmp_path: Path, monkeypatch, capsys) -> dict:
    recibo = _recibo3(candidato, tmp_path / "recibo3.json")
    rc_, err = _validar_cli(candidato, recibo, monkeypatch, capsys)
    assert rc_ == 0, err
    return {**candidato, "recibo": recibo}


def test_control_el_ciclo_completo_valida_guarda_y_publica_el_resultado_revisado(validada: dict) -> None:
    """Control: el recibo /3 valida, la puerta abre, el squash con el árbol revisado publica y la cadena sigue viva."""
    assert (cs.read(validada["txn"]) or {})["status"] == "validated"
    ok, motivo = _guard(validada)
    assert ok, motivo
    squash = _squash_con_el_mismo_arbol(validada)
    doc = txn.publish(validada["txn"], manifest=validada["txn"].with_name("campaign_manifest.json"), release_sha=squash)
    assert doc["status"] == "published" and doc["release_sha"] == squash
    _toda_la_cadena_se_acredita(validada)


def test_un_squash_con_otro_arbol_no_publica_ni_consume_el_permiso(validada: dict, tmp_path: Path) -> None:
    """Un merge que NO reproduce el árbol revisado (otra cifra) se niega; el permiso sigue en `validated`."""
    clon = tmp_path / "clon"
    subprocess.run(["git", "clone", "-q", str(validada["cand"]), str(clon)], check=True)
    cab._escribir(clon, "reports/governance/key_facts.json", '{"cambiado": "tras revisar"}\n')
    otro = _commit(clon, "no revisado")
    _git(validada["cand"], "fetch", "-q", str(clon), otro)
    with pytest.raises(txn.PublicationBlocked, match="árbol"):
        txn.publish(validada["txn"], manifest=validada["txn"].with_name("campaign_manifest.json"), release_sha=otro)
    assert (cs.read(validada["txn"]) or {})["status"] == "validated"


@pytest.mark.parametrize(
    ("cambio", "patron"),
    [
        ({"reconciliation_sha256": "0" * 64}, "reconciliación"),
        ({"reconciliation_id": "recon_otra"}, "reconciliación"),
        ({"mother_computed_sha256": "0" * 64}, "cómputo"),
        ({"candidate_git_sha": "0" * 40}, "candidato"),
        ({"candidate_tree_sha": "0" * 40}, "candidato"),
        ({"release_manifest_sha256": "0" * 64}, "manifiesto"),
        ({"release_id": "2026-10-otro"}, "release_id"),
        ({"reconciliation_root": "relativa/recon"}, "absoluta"),
    ],
)
def test_un_recibo3_que_no_describe_el_resultado_no_valida(
    candidato: dict, tmp_path: Path, monkeypatch, capsys, cambio: dict, patron: str
) -> None:
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json", **cambio), monkeypatch, capsys)
    assert rc_ != 0 and patron in err, err
    assert (cs.read(candidato["txn"]) or {})["status"] == "computed"


def test_un_candidato_que_no_es_la_reconciliacion_no_valida(candidato: dict, tmp_path, monkeypatch, capsys) -> None:
    """Un byte distinto en una salida del candidato, aunque el recibo describa ESE commit, no es lo revisado."""
    cab._escribir(candidato["cand"], "reports/governance/key_facts.json", '{"crps_fad": 99}\n')
    _commit(candidato["cand"], "otra cifra")
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "key_facts.json" in err, err


def test_un_candidato_con_un_reporte_de_mas_no_valida(candidato: dict, tmp_path, monkeypatch, capsys) -> None:
    cab._escribir(candidato["cand"], "reports/eval/intruso.csv", "x\n")
    _commit(candidato["cand"], "intruso")
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "intruso.csv" in err, err


def test_un_candidato_sucio_de_verdad_cierra_la_puerta(validada: dict) -> None:
    """El `git_dirty` sellado de la madre no dice nada del árbol que publica: se mira el árbol real."""
    (validada["cand"] / "suelto.txt").write_text("sin commitear\n")
    ok, motivo = _guard(validada)
    assert not ok and "sucio" in motivo, motivo


def test_un_candidato_que_avanza_tras_validar_cierra_la_puerta(validada: dict) -> None:
    cab._escribir(validada["cand"], "docs/nota.md", "otro commit\n")
    _commit(validada["cand"], "avanza")
    ok, motivo = _guard(validada)
    assert not ok and "candidato" in motivo, motivo


def test_una_reconciliacion_tocada_tras_validar_cierra_la_puerta(validada: dict) -> None:
    salida = validada["recon"] / "reports/governance/key_facts.json"
    salida.write_text(salida.read_text() + " ")
    ok, motivo = _guard(validada)
    assert not ok and "reconciliación" in motivo, motivo


# ═══════════════════════════════ ramas de las guardas que la mutación exige ejercitar
def test_una_copia_ajena_junto_a_una_madre_computed_no_se_acredita(cerrada: dict) -> None:
    (cerrada["txn"].parent / cs.COMPUTED_SNAPSHOT).write_text('{"otro": "cómputo"}\n')
    assert "padre-evidencia" in _codigos_padre(cerrada)


def test_el_hash_del_computo_no_se_reescribe_en_una_transicion(cerrada: dict) -> None:
    _validar(cerrada)
    with pytest.raises(ValueError, match="inmutable"):
        cs._transition(cerrada["txn"], "published",
                       {"published_at": T_PUB, "release_sha": "e" * 40, "computed_sha256": "0" * 64})  # fmt: skip


def test_el_esquema_exige_la_evidencia_solo_desde_validated() -> None:
    from tests import campana_valida

    base = {"campaign_id": "c", "source_git_sha": "a" * 40, "panel_sha256": "sha256:" + "b" * 64}
    validada = campana_valida.transaccion(**base, status="validated")
    assert cs.validate_schema({k: v for k, v in validada.items() if k != "computed_sha256"})
    computada = campana_valida.transaccion(**base, status="computed")
    assert cs.validate_schema({**computada, "computed_sha256": "f" * 64})


def test_la_puerta_no_abre_con_la_evidencia_del_computo_manipulada(validada: dict) -> None:
    copia = validada["txn"].parent / cs.COMPUTED_SNAPSHOT
    copia.chmod(0o644)
    copia.write_bytes(copia.read_bytes() + b" ")
    ok, motivo = _guard(validada)
    assert not ok and "evidencia del cómputo" in motivo, motivo


def test_una_reconciliacion_sin_cerrar_no_describe_un_resultado(con_e3: dict, tmp_path, monkeypatch, capsys) -> None:  # noqa: F811
    assert _ensamblar(con_e3, capsys, con_e3["c5"], con_e3["c6"], con_e3["c4"])[0] == 0  # `assembled`, sin cerrar
    m = {**con_e3, "txn": con_e3["madre"] / cx.CAMPANA_REL}
    m["sha_computado"] = cx.leer_padre(m["madre"])["campaign_json_sha256"]
    cand = tmp_path / "cand_sin_cerrar"
    cand.mkdir()
    _git(cand, "init", "-q")
    cab._escribir(cand, MANIFIESTO_RELEASE, '{"release_id": "2026-10-prueba-0123456789ab"}\n')
    _commit(cand, "vacío")
    rc_, err = _validar_cli(
        {**m, "cand": cand}, _recibo3({**m, "cand": cand}, tmp_path / "r.json"), monkeypatch, capsys
    )
    assert rc_ != 0 and "derived" in err, err


def test_un_candidato_sin_un_canonico_no_valida(candidato: dict, tmp_path, monkeypatch, capsys) -> None:
    _git(candidato["cand"], "rm", "-q", "reports/latex/key_facts.tex")
    _commit(candidato["cand"], "sin key_facts.tex")
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "faltan ['reports/latex/key_facts.tex']" in err, err


def test_un_candidato_que_arrastra_lo_interno_de_la_reconciliacion_no_valida(
    candidato: dict, tmp_path, monkeypatch, capsys
) -> None:
    """La evidencia copiada SÍ está en el inventario de la reconciliación, pero nunca viaja al candidato."""
    interno = "reports/campaign/complement_evidence/SHA256SUMS"
    cab._escribir(candidato["cand"], interno, (candidato["recon"] / interno).read_text())
    _commit(candidato["cand"], "con lo interno")
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "SHA256SUMS" in err, err


# ═══════════════════════════════ supervivientes de la primera ronda de mutación (13 de 17 en el lector)
def _forjar_copia(m: dict, cambio) -> None:
    """Reescribe la copia del cómputo (quitándole la protección) y re-sella su hash en el estado vivo."""
    copia = m["txn"].parent / cs.COMPUTED_SNAPSHOT
    copia.chmod(0o644)
    doc = json.loads(copia.read_text())
    cambio(doc)
    copia.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    _reescribir_vivo(m, computed_sha256=hashlib.sha256(copia.read_bytes()).hexdigest())


@pytest.mark.parametrize(
    "cambio",
    [
        pytest.param(lambda d: d.update(status="running", revision=0), id="copia-que-no-es-computed"),
        pytest.param(lambda d: d.update(reviewed_by="Alguien Previo"), id="publicacion-ya-llena-en-el-computo"),
    ],
)
def test_una_copia_forjada_y_resellada_no_se_acredita(cerrada: dict, cambio) -> None:
    _validar(cerrada)
    _forjar_copia(cerrada, cambio)
    assert "padre-evidencia" in _codigos_padre(cerrada)


def test_un_estado_vivo_con_claves_que_el_computo_no_tenia_no_se_acredita(cerrada: dict) -> None:
    _validar(cerrada)
    _reescribir_vivo(cerrada, exit_code=0)
    assert "padre-evidencia" in _codigos_padre(cerrada)


def test_un_estado_vivo_que_no_es_posterior_al_computo_no_se_acredita(cerrada: dict) -> None:
    _validar(cerrada)
    _reescribir_vivo(cerrada, revision=json.loads(cerrada["bytes_computado"])["revision"])
    assert "padre-evidencia" in _codigos_padre(cerrada)


def test_la_puerta_exige_la_evidencia_antes_que_el_recibo(cerrada: dict, tmp_path) -> None:
    """La evidencia del cómputo la exige la propia puerta, no sólo el vínculo del /3: un `validated` con cualquier
    recibo y la copia manipulada cae por la EVIDENCIA, antes de que el recibo se mire."""
    estado = cs.read(cerrada["txn"]) or {}
    recibo = tmp_path / "recibo2.json"
    recibo.write_text(json.dumps({"schema": txn.RECEIPT_SCHEMA, "reviewed_by": "Javier Rebull",
                                  "decision": "aprobada", "reviewed_at": txn.now_rfc3339(),
                                  **{k: estado[k] for k in txn.IDENTIDAD_RECIBO}}))  # fmt: skip
    cs.mark_validated(cerrada["txn"], validation_receipt_sha256=txn.sha256_file(recibo),
                      validation_receipt_path=str(recibo.resolve()), reviewed_by="Javier Rebull",
                      validated_at=txn.now_rfc3339(), decision="aprobada")  # fmt: skip
    ok, motivo = _guard(cerrada)
    assert not ok and "/3" in motivo, motivo  # control: con la evidencia intacta, cae por el recibo /2
    copia = cerrada["txn"].parent / cs.COMPUTED_SNAPSHOT
    copia.chmod(0o644)
    copia.write_bytes(copia.read_bytes() + b" ")
    ok, motivo = _guard(cerrada)
    assert not ok and "evidencia del cómputo" in motivo, motivo


def test_una_reconciliacion_de_otra_madre_se_nombra_asi(candidato: dict, tmp_path, monkeypatch, capsys) -> None:
    """Su lector también la rechazaría, pero el diagnóstico tiene que decir QUÉ no cuadra: la madre."""
    _resellar_plan(candidato["recon"], lambda d: d["parent"].update(campaign_json_sha256="0" * 64))
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "otra madre" in err, err


def test_sobre_una_madre_publicada_ya_no_se_ensambla_otra_reconciliacion(cerrada: dict) -> None:
    """Leer lo ya hecho, sí; producir piezas nuevas sobre un resultado ya servido, no."""
    _validar(cerrada)
    _publicar(cerrada)
    with pytest.raises(cx.ComplementoError) as e:
        cx.leer_padre(cerrada["madre"], admitidos=cx.PADRE_PLANIFICABLE)
    assert {c for c, _ in e.value.problemas} == {"padre-estado"}
    with pytest.raises(rc.ReconciliacionError) as r:
        rc._padre(cerrada["madre"])
    assert r.value.codigos == {"padre-estado"}
    _toda_la_cadena_se_acredita(cerrada)  # y lo ya hecho se sigue acreditando


# ═══════════════════════════════ auditoría de B5a (17-oct): tres rutas que `b261300` dejaba abiertas
def _recibo2(m: dict, destino: Path) -> Path:
    estado = cs.read(m["txn"]) or {}
    destino.write_text(json.dumps({"schema": txn.RECEIPT_SCHEMA, "reviewed_by": "Javier Rebull",
                                   "decision": "aprobada", "reviewed_at": txn.now_rfc3339(),
                                   **{k: estado[k] for k in txn.IDENTIDAD_RECIBO}}))  # fmt: skip
    return destino


def _otro_commit(m: dict, tmp_path: Path) -> str:
    """Un commit del candidato con OTRA cifra (no revisada), traído al repo candidato sin tocar su árbol."""
    clon = tmp_path / "clon_ataque"
    subprocess.run(["git", "clone", "-q", str(m["cand"]), str(clon)], check=True)
    cab._escribir(clon, "reports/governance/key_facts.json", '{"crps_fad": 1}\n')
    otro = _commit(clon, "no revisado")
    _git(m["cand"], "fetch", "-q", str(clon), otro)
    return otro


def test_ruta1_un_recibo2_ya_no_valida_una_campana(candidato: dict, tmp_path, monkeypatch, capsys) -> None:
    """RED: con `/2` se validaba la madre reconciliada y después se publicaba un resultado cambiado."""
    rc_, err = _validar_cli(candidato, _recibo2(candidato, tmp_path / "r2.json"), monkeypatch, capsys)
    assert rc_ != 0 and "/3" in err, err
    assert (cs.read(candidato["txn"]) or {})["status"] == "computed"


def test_ruta1_un_estado_validado_con_un_recibo2_no_publica(candidato: dict, tmp_path) -> None:
    """Aunque un `validated` con `/2` llegue al disco (escrito a mano o heredado), no autoriza publicar nada."""
    recibo = _recibo2(candidato, tmp_path / "r2.json")
    cs.mark_validated(candidato["txn"], validation_receipt_sha256=txn.sha256_file(recibo),
                      validation_receipt_path=str(recibo.resolve()), reviewed_by="Javier Rebull",
                      validated_at=txn.now_rfc3339(), decision="aprobada")  # fmt: skip
    ok, motivo = _guard(candidato)
    assert not ok and "/3" in motivo, motivo
    with pytest.raises(txn.PublicationBlocked):
        txn.publish(candidato["txn"], manifest=candidato["txn"].with_name("campaign_manifest.json"),
                    release_sha=_otro_commit(candidato, tmp_path))  # fmt: skip
    assert (cs.read(candidato["txn"]) or {})["status"] == "validated"


def test_ruta2_cambiar_el_recibo_durante_la_puerta_no_publica_otro_arbol(validada: dict, tmp_path, monkeypatch) -> None:
    """RED: tras acreditar el hash del `/3`, se sustituía por un `/2` antes de la relectura y se publicaba otro árbol."""
    otro = _otro_commit(validada, tmp_path)
    original = txn._receipt_matches

    def acreditar_y_cambiar(obj):
        resultado = original(obj)
        _recibo2(validada, validada["recibo"])  # el atacante cambia el archivo justo después de que se acreditara
        return resultado

    monkeypatch.setattr(txn, "_receipt_matches", acreditar_y_cambiar)
    with pytest.raises(txn.PublicationBlocked):
        txn.publish(validada["txn"], manifest=validada["txn"].with_name("campaign_manifest.json"), release_sha=otro)
    assert (cs.read(validada["txn"]) or {})["status"] == "validated"


def test_ruta2_validar_registra_el_hash_de_los_bytes_que_acredito(
    candidato: dict, tmp_path, monkeypatch, capsys
) -> None:
    recibo = _recibo3(candidato, tmp_path / "r3.json")
    acreditados = recibo.read_bytes()
    original = txn.vp.problema

    def vincular_y_cambiar(*args, **kwargs):
        motivo = original(*args, **kwargs)
        _recibo2(candidato, recibo)  # cambia DESPUÉS de acreditar y ANTES de registrar el hash
        return motivo

    monkeypatch.setattr(txn.vp, "problema", vincular_y_cambiar)
    rc_, err = _validar_cli(candidato, recibo, monkeypatch, capsys)
    estado = cs.read(candidato["txn"]) or {}
    assert rc_ != 0 or estado["validation_receipt_sha256"] == hashlib.sha256(acreditados).hexdigest(), (rc_, err)


@pytest.mark.parametrize(
    "rel",
    ["reports/eval/crps_fad.csv", "reports/eval/cohort_scan.json"],
    ids=["artefacto-de-una-fuente", "capa-de-la-madre"],
)
def test_ruta3_un_candidato_incompleto_no_valida(candidato: dict, tmp_path, monkeypatch, capsys, rel: str) -> None:
    """RED: sin un artefacto de C6 o sin una capa de la madre, el candidato se validaba. Las figuras re-derivadas
    ya no van aquí: el contrato `EXPORTACION` las destina al documental."""
    _git(candidato["cand"], "rm", "-q", rel)
    _commit(candidato["cand"], f"sin {rel}")
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "faltan" in err and Path(rel).name in err, err


@pytest.mark.parametrize("donde", [".gitignore", ".git/info/exclude"])
def test_ruta3_las_reglas_de_ignorado_no_eximen_de_evidencia(
    candidato: dict, tmp_path, monkeypatch, capsys, donde: str
) -> None:
    """RED (auditoría 17-oct): sin `crps_fad.csv` ni `key_facts.json`, el candidato validaba si esas rutas estaban
    ignoradas, incluso en `.git/info/exclude`, FUERA del commit revisado."""
    faltantes = ["reports/eval/crps_fad.csv", "reports/governance/key_facts.json"]
    for rel in faltantes:
        _git(candidato["cand"], "rm", "-q", "--cached", rel)
        (candidato["cand"] / rel).unlink()
    destino = candidato["cand"] / donde
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(faltantes) + "\n")
    _commit(candidato["cand"], "sin evidencia, ignorada")
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "crps_fad.csv" in err and "key_facts.json" in err, err


def test_control_las_salidas_del_contrato_no_viajan_a_datos(candidato: dict, tmp_path, monkeypatch, capsys) -> None:
    """Benigno: las figuras que el contrato destina al documental (o a uso local) no se exigen en datos, sin
    necesidad de ninguna regla de ignorado."""
    versionados = set(_git(candidato["cand"], "ls-files").split())
    assert not versionados & set(vp.EXPORTACION), "el candidato, como la rama de datos real, no lleva esas salidas"
    assert not (candidato["cand"] / ".gitignore").exists(), "y no hace falta ninguna regla de ignorado"
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ == 0, err


def test_el_contrato_de_exportacion_cubre_exactamente_las_figuras_re_derivadas() -> None:
    """Contrastado con lo que la reconciliación declara: una figura nueva sin destino rompe esta prueba."""
    assert set(vp.EXPORTACION) == set(rc.SALIDAS_FIGURA)
    assert {d for d, _ in vp.EXPORTACION.values()} == {"documental", "local"}


def test_una_salida_destinada_al_documental_no_se_cuela_en_datos(
    candidato: dict, tmp_path, monkeypatch, capsys
) -> None:
    fig = "reports/latex/Figures/results_ranking_mase.pdf"
    cab._escribir(candidato["cand"], fig, (candidato["recon"] / fig).read_text())
    _commit(candidato["cand"], "una figura del documental en datos")
    rc_, err = _validar_cli(candidato, _recibo3(candidato, tmp_path / "r.json"), monkeypatch, capsys)
    assert rc_ != 0 and "sobran" in err and "results_ranking_mase.pdf" in err, err


# ═══════════════════════════════ opción A · una sola vía: también una campaña SIN complementarias
@pytest.fixture
def sin_complementarias(rm: dict, capsys, tmp_path) -> dict:  # noqa: F811
    """La madre reconciliada consigo misma: reconciliación SIN fuentes, cerrada, y su candidato."""
    capsys.readouterr()
    rc_ = cli_rc.main(["--root", str(rm["recon"]), "assemble", "--parent-root", str(rm["madre"]), "--evidence",
                       "reports/campaign/complement_evidence/SHA256SUMS", "--id", "recon_sin_fuentes"])  # fmt: skip
    assert rc_ == 0, capsys.readouterr()
    _cerrado(rm)
    m = {**rm, "txn": rm["madre"] / cx.CAMPANA_REL}
    m["sha_computado"] = cx.leer_padre(m["madre"])["campaign_json_sha256"]
    return _con_candidato(m, tmp_path / "candidato_sin_fuentes")


def test_una_campana_sin_complementarias_se_valida_y_publica_por_la_misma_via(
    sin_complementarias: dict, tmp_path, monkeypatch, capsys
) -> None:
    m = sin_complementarias
    assert rc.leer(m["recon"])["sources"] == [] and rc.leer(m["recon"])["status"] == "derived"
    rc_, err = _validar_cli(m, _recibo3(m, tmp_path / "r3.json"), monkeypatch, capsys)
    assert rc_ == 0, err
    ok, motivo = _guard(m)
    assert ok, motivo
    squash = _squash_con_el_mismo_arbol(m)
    doc = txn.publish(m["txn"], manifest=m["txn"].with_name("campaign_manifest.json"), release_sha=squash)
    assert doc["status"] == "published"
    rc.acreditar(m["recon"], parent_root=m["madre"], **_lectores())
