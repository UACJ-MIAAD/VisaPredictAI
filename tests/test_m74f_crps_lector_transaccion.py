"""M74-F · #66 y #67 — el consumidor del CRPS acredita la TRANSACCIÓN del recibo; los conteos son enteros.

#66. `champion.crps_provenance` devolvía ``acreditado`` para cualquier recibo con el esquema, el sha y el
panel correctos cuando no había campaña ``running``: el ``campaign_id`` era una cadena que nadie resolvía.
La C5 fallida (`compl_0e60930_c5_20261012T110036`) salía ``acreditado``. Ahora el recibo tiene que
resolverse contra una transacción del MISMO árbol:

* la campaña (`reports/campaign/campaign.json`) de ese id, ``running``/``computed``/``validated``, con el
  código y el panel del recibo; o
* la complementaria ``champion_crps`` de ese id, ``accredited``, que su lector (`complement.acreditar`)
  vuelve a acreditar entera —madre, código, panel, DuckDB, entradas congeladas y CSV— con la raíz de la
  madre que da el consumidor.

#67. `sustancia_crps` convertía los conteos con ``int(float(...))``: ``n_holdout_F=24.5`` pasaba como 24.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

import tests.test_m74f_champion_crps as c5
from tests.test_m74f_champion_crps import _c5_calculada, _cerrar, _cli, _recibo

# Las fixtures del mundo git real de C5, registradas también en este módulo (incluido el autouse que mide
# `codigo_cargado` contra este checkout).
mundo, m5, _codigo_del_checkout = c5.mundo, c5.m5, c5._codigo_del_checkout

pytest.importorskip("darts")

from vp_model import artifact_receipt as ar  # noqa: E402
from vp_model import champion  # noqa: E402
from vp_model import complement as cx  # noqa: E402

CSV = "table,country,category,crps\nFAD,ALL,ALL,32.1\nDFF,ALL,ALL,31.3\n"
SHA = "a" * 40


# ═══════════════════════════════ mundo simple: la campaña del árbol
@pytest.fixture
def arbol(tmp_path: Path):
    reports = tmp_path / "reports"
    (reports / "eval").mkdir(parents=True)
    panel = tmp_path / "panel.csv"
    panel.write_text("country,category,value\nmexico,F1,1\n")
    return reports, panel


def _sellar(reports: Path, panel: Path, campaign_id: str, code_sha: str = SHA) -> None:
    csv_p = reports / "eval" / "crps_champion.csv"
    csv_p.write_text(CSV)
    ar.seal(csv_p, schema=champion.CRPS_RECEIPT_SCHEMA, campaign_id=campaign_id, code_sha=code_sha,
            panel_sha256="sha256:" + ar.sha256_file(panel), protocol={"tables": ["FAD", "DFF"]},
            coverage={"tables": ["DFF", "FAD"]})  # fmt: skip


def _campana(reports: Path, panel: Path, campaign_id: str, status: str, code_sha: str = SHA) -> None:
    (reports / "campaign").mkdir(parents=True, exist_ok=True)
    (reports / "campaign" / "campaign.json").write_text(json.dumps({
        "status": status, "campaign_id": campaign_id, "source_git_sha": code_sha,
        "panel_sha256": "sha256:" + ar.sha256_file(panel)}))  # fmt: skip


def _estado(reports: Path, panel: Path, **kw) -> dict:
    return champion.crps_provenance("FAD", reports=reports, panel=panel, **kw)


def test_un_recibo_que_ninguna_transaccion_resuelve_no_acredita(arbol) -> None:
    """El agujero de #66: un id inventado, sin campaña ni complementaria, salía `acreditado`."""
    reports, panel = arbol
    _sellar(reports, panel, "rederiv_inventada")
    p = _estado(reports, panel)
    assert p["status"] == "heredado" and p["value"] is None, p
    assert "rederiv_inventada" in p["reason"] and p["inherited_value"] == pytest.approx(32.1)


@pytest.mark.parametrize("status", ["failed", "planned", "abortada"])
def test_una_campana_que_no_produjo_no_presta_su_identidad(arbol, status: str) -> None:
    reports, panel = arbol
    _sellar(reports, panel, "rederiv_x")
    _campana(reports, panel, "rederiv_x", status)
    p = _estado(reports, panel)
    assert p["status"] == "heredado" and status in p["reason"], p


def test_la_campana_de_ese_id_con_otro_codigo_no_acredita(arbol) -> None:
    reports, panel = arbol
    _sellar(reports, panel, "rederiv_x", code_sha="b" * 40)
    _campana(reports, panel, "rederiv_x", "running")
    assert _estado(reports, panel)["status"] == "heredado"


def test_una_campana_ilegible_no_acredita(arbol) -> None:
    reports, panel = arbol
    _sellar(reports, panel, "rederiv_x")
    (reports / "campaign").mkdir()
    (reports / "campaign" / "campaign.json").write_text('{"status": "running", "status": "computed"}')
    assert _estado(reports, panel)["status"] == "heredado"


@pytest.mark.parametrize("status", ["running", "computed", "validated"])
def test_la_campana_que_lo_produjo_no_basta_si_el_recibo_no_supera_la_validacion(arbol, status: str) -> None:
    """Auditoría C6: el camino de campaña sólo miraba la identidad. Un recibo de juguete (protocolo parcial, sin
    DuckDB contra la que medir la sustancia) ya no se acredita aunque su campaña exista. El control válido, con
    productor real y DuckDB, está en `test_c5_en_campana_*`."""
    reports, panel = arbol
    _sellar(reports, panel, "rederiv_x")
    _campana(reports, panel, "rederiv_x", status)
    p = _estado(reports, panel)
    assert p["status"] == "heredado" and "crps-protocolo" in p["reason"], p


# ═══════════════════════════════ mundo git real: la complementaria C5
def _c5(m: dict) -> tuple[Path, Path]:
    return m["compl"] / "reports", m["compl"] / cx.PANEL_REL


def test_la_complementaria_fallida_no_acredita_su_crps(m5: dict, monkeypatch, capsys) -> None:
    """El caso real: C5 abortada (`failed`) con un CSV y un recibo íntegros salía `acreditado`."""
    _c5_calculada(m5, monkeypatch, capsys)
    cx.fallar(m5["compl"], "distribución degenerada")
    p = _estado(*_c5(m5))
    assert p["status"] == "heredado" and "failed" in p["reason"], p


def test_la_complementaria_calculada_pero_no_aceptada_no_acredita(m5: dict, monkeypatch, capsys) -> None:
    _c5_calculada(m5, monkeypatch, capsys)
    assert _cerrar(m5, capsys)[1]["status"] == "computed"
    p = _estado(*_c5(m5), parent_root=m5["madre"])
    assert p["status"] == "heredado" and "computed" in p["reason"], p


def test_la_complementaria_aceptada_sin_la_raiz_de_su_madre_no_acredita(m5: dict, monkeypatch, capsys) -> None:
    """Sin madre no se re-acredita nada: el estado escrito `accredited` no basta."""
    _c5_calculada(m5, monkeypatch, capsys)
    _cerrar(m5, capsys)
    assert _cli(m5, "accept", "--parent-root", str(m5["madre"]), capsys=capsys)[1]["status"] == "accredited"
    p = _estado(*_c5(m5))
    assert p["status"] == "heredado" and "madre" in p["reason"], p


def test_una_complementaria_de_otro_proposito_no_acredita_el_crps(m5: dict, monkeypatch, capsys) -> None:
    """Un id que coincide no basta: sólo una complementaria `champion_crps` responde por un CRPS."""
    _aceptada(m5, monkeypatch, capsys)
    leer = cx.leer
    monkeypatch.setattr(cx, "leer", lambda root: {**leer(root), "purpose": "e3"})
    p = _estado(*_c5(m5), parent_root=m5["madre"])
    assert p["status"] == "heredado" and "'e3'" in p["reason"], p


def _aceptada(m: dict, monkeypatch, capsys) -> None:
    _c5_calculada(m, monkeypatch, capsys)
    _cerrar(m, capsys)
    assert _cli(m, "accept", "--parent-root", str(m["madre"]), capsys=capsys)[1]["status"] == "accredited"


def test_control_la_complementaria_aceptada_y_reacreditada_lo_acredita(m5: dict, monkeypatch, capsys) -> None:
    _aceptada(m5, monkeypatch, capsys)
    p = _estado(*_c5(m5), parent_root=m5["madre"])
    assert p["status"] == "acreditado" and p["receipt_campaign_id"] == _recibo(m5)["campaign_id"], p
    assert p["transaction"] == {"kind": "complement", "status": "accredited"}


def test_la_madre_cambiada_despues_de_aceptar_no_acredita(m5: dict, monkeypatch, capsys) -> None:
    _aceptada(m5, monkeypatch, capsys)
    madre = m5["madre"] / cx.CAMPANA_REL
    madre.write_text(madre.read_text().replace('"computed"', '"validated"'))
    p = _estado(*_c5(m5), parent_root=m5["madre"])
    assert p["status"] == "heredado" and "padre-cambio" in p["reason"], p


def test_el_arbol_cambiado_despues_de_aceptar_no_acredita(m5: dict, monkeypatch, capsys) -> None:
    """El lector re-acredita el árbol: una entrada congelada tocada tras aceptar invalida el CRPS."""
    _aceptada(m5, monkeypatch, capsys)
    receta = m5["compl"] / cx.MANIFIESTO_CAMPEON_REL
    receta.write_text(receta.read_text().replace('"median"', '"mean"', 1))
    p = _estado(*_c5(m5), parent_root=m5["madre"])
    assert p["status"] == "heredado" and "entrada-congelada" in p["reason"], p


def test_un_recibo_de_otro_id_junto_a_una_complementaria_no_acredita(m5: dict, monkeypatch, capsys) -> None:
    _aceptada(m5, monkeypatch, capsys)
    destino = m5["compl"] / cx.CRPS_REL
    acta = _recibo(m5)
    ar.seal(destino, schema=acta["schema"], campaign_id="compl_otra", code_sha=acta["code_sha"],
            panel_sha256=acta["panel_sha256"], protocol=acta["protocol"], coverage=acta["coverage"])  # fmt: skip
    p = _estado(*_c5(m5), parent_root=m5["madre"])
    # rechazado por la identidad, antes de re-acreditar (el lector también lo cazaría, por `crps-identidad`)
    assert p["status"] == "heredado" and "la complementaria de este árbol es" in p["reason"], p
    assert "compl_otra" in p["reason"] and c5.ID_C5 in p["reason"]


def test_el_veredicto_entrega_la_raiz_de_la_madre_al_lector(tmp_path: Path, monkeypatch) -> None:
    """El consumidor real: `run_champion_challenger --crps-parent-root` llega a `crps_provenance`, y el `.md`
    y el `.json` cuentan la MISMA procedencia (una sola lectura por tabla)."""
    import run_champion_challenger as rch

    vistas: list[tuple[str, object]] = []

    def procedencia(table: str, **kw: object) -> dict:
        vistas.append((table, kw.get("parent_root")))
        return {"status": "heredado", "value": None, "inherited_value": 32.1, "reason": f"motivo-{table}"}

    monkeypatch.setattr(rch, "REPORTS", tmp_path)
    monkeypatch.setattr(champion, "load_manifest", lambda: {"FAD": "r", "DFF": "r"})
    monkeypatch.setattr(champion, "evaluate", lambda t, r: champion.Verdict(t, "naive1", 0.1, 0.09))
    monkeypatch.setattr(champion, "crps_provenance", procedencia)
    monkeypatch.setattr("sys.argv", ["run_champion_challenger.py", "--crps-parent-root", str(tmp_path / "madre")])
    rch.main()
    assert vistas == [("FAD", tmp_path / "madre"), ("DFF", tmp_path / "madre")]
    payload = json.loads((tmp_path / "governance" / "champion_challenger.json").read_text())
    assert payload["FAD"]["champion_crps_provenance"]["reason"] == "motivo-FAD"
    assert (
        "CRPS heredado 32.1 (no recalculado: motivo-DFF)"
        in (tmp_path / "governance" / "champion_challenger.md").read_text()
    )


# ═══════════════════════════════ #67: conteos enteros
def _parchear(m: dict, campo: str, valor: str, *, fila_all: bool = False) -> None:
    """Cambia un campo de la primera fila (serie o ALL) de FAD y RE-SELLA el recibo con sha coherente."""
    destino = m["compl"] / cx.CRPS_REL
    filas = list(csv.DictReader(io.StringIO(destino.read_text())))
    objetivo = next(f for f in filas if f["table"] == "FAD" and (f["country"] == "ALL") == fila_all)
    assert objetivo[campo] != valor
    objetivo[campo] = valor
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(filas[0]))
    w.writeheader()
    w.writerows(filas)
    destino.write_text(out.getvalue())
    acta = _recibo(m)
    ar.seal(destino, schema=acta["schema"], campaign_id=acta["campaign_id"], code_sha=acta["code_sha"],
            panel_sha256=acta["panel_sha256"], protocol=acta["protocol"], coverage=acta["coverage"])  # fmt: skip


def _codigos(m: dict) -> set[str]:
    doc = json.loads((m["compl"] / cx.RUTA_REL).read_text())
    return {c for c, _ in cx.verificar_crps(m["compl"], doc)}


@pytest.mark.parametrize(("campo", "valor"), [("n_holdout_F", "24.5"), ("k95", "20.5"), ("k95", "2.05e1")])
def test_un_conteo_fraccionario_no_pasa(m5: dict, monkeypatch, capsys, campo: str, valor: str) -> None:
    """El agujero de #67: `int(24.5) = 24` dejaba la fila coherente con `cov95 = k/24`."""
    _c5_calculada(m5, monkeypatch, capsys)
    assert _codigos(m5) == set(), "control: lo que escribe el productor real se acredita"
    _parchear(m5, campo, valor)
    assert "crps-sustancia" in _codigos(m5)


@pytest.mark.parametrize(("campo", "valor", "fila_all"), [("n_holdout_F", "24.0", False), ("k95", "2e1", False),
                                                           ("n_series", "1", True)])  # fmt: skip
def test_control_un_conteo_entero_en_otra_grafia_pasa(
    m5: dict, monkeypatch, capsys, campo: str, valor: str, fila_all: bool
) -> None:
    """El productor escribe `n_series` como `25.0` (pandas, columna con NaN): el valor entero es lo que cuenta."""
    _c5_calculada(m5, monkeypatch, capsys)
    _parchear(m5, campo, valor, fila_all=fila_all)
    assert _codigos(m5) == set()


# ═══════════════════════════════ auditoría C6: el camino de campaña valida protocolo, forma y sustancia
def _campana_c5(m: dict, monkeypatch, cid: str = "rederiv_c5_t1") -> None:
    root = m["compl"]
    head = c5._git(root, "rev-parse", "HEAD").strip()
    c5._escribir(root, "reports/campaign/campaign.json", json.dumps({
        "status": "running", "campaign_id": cid, "source_git_sha": head,
        "panel_sha256": "sha256:" + ar.sha256_file(root / cx.PANEL_REL)}))  # fmt: skip
    monkeypatch.setenv("CAMPAIGN_ID", cid)
    monkeypatch.setenv("CAMPAIGN_SHA", head)


def _producida_en_campana(m: dict, monkeypatch) -> None:
    _campana_c5(m, monkeypatch)
    c5._productor(monkeypatch, m)
    c5.rcc.main([])
    assert _recibo(m)["campaign_id"] == "rederiv_c5_t1"


def _estado_c5(m: dict, **kw) -> dict:
    return champion.crps_provenance("FAD", reports=m["compl"] / "reports", panel=m["compl"] / cx.PANEL_REL, **kw)


@pytest.mark.parametrize("status", ["running", "computed", "validated"])
def test_c5_en_campana_control_el_productor_real_se_acredita(m5: dict, monkeypatch, status: str) -> None:
    _producida_en_campana(m5, monkeypatch)
    txn = m5["compl"] / cx.CAMPANA_REL
    txn.write_text(txn.read_text().replace('"running"', f'"{status}"'))
    p = _estado_c5(m5)
    assert p["status"] == "acreditado" and p["transaction"] == {"kind": "campaign", "status": status}, p


def _filas_c5(m: dict) -> list[dict]:
    return list(csv.DictReader(io.StringIO((m["compl"] / cx.CRPS_REL).read_text())))


@pytest.mark.parametrize(
    "mutar",
    [
        lambda m: c5._reescribir(m, _filas_c5(m), protocol={**champion.CRPS_PROTOCOLO, "num_samples": 50}),
        lambda m: c5._reescribir(m, [{**f, "crps": "-4.0"} if f["country"] != "ALL" else f for f in _filas_c5(m)]),
        lambda m: c5._reescribir(m, _filas_c5(m), total=lambda t, fila: {**fila, "crps": fila["crps"] + 9}),
        lambda m: c5._reescribir(m, _filas_c5(m), coverage=["invalida"]),
        lambda m: c5._reescribir(m, _filas_c5(m), protocol="completo"),
    ],
    ids=["otras_muestras", "crps_negativo", "total_inflado", "coverage_lista", "protocolo_texto"],
)
def test_c5_en_campana_el_consumidor_valida_el_recibo(m5: dict, monkeypatch, mutar) -> None:
    _producida_en_campana(m5, monkeypatch)
    mutar(m5)
    p = _estado_c5(m5)
    assert p["status"] == "heredado" and p["value"] is None and "crps-" in p["reason"], p


def test_c5_complementaria_con_coverage_mal_tipada_es_heredado_y_no_lanza(m5: dict, monkeypatch, capsys) -> None:
    _aceptada(m5, monkeypatch, capsys)
    c5._reescribir(m5, _filas_c5(m5), coverage=["invalida"])
    p = _estado(*_c5(m5), parent_root=m5["madre"])
    assert p["status"] == "heredado" and "crps-recibo" in p["reason"], p


def _csv_c5_editado_sin_resellar(m: dict) -> None:
    destino = m["compl"] / cx.CRPS_REL
    filas = _filas_c5(m)
    i = next(i for i, f in enumerate(filas) if f["country"] != "ALL")
    filas[i] = {**filas[i], "crps": f"{float(filas[i]['crps']) + 0.0:.3f}"}  # misma cifra, otra grafía
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(filas[0]))
    w.writeheader()
    w.writerows(filas)
    assert out.getvalue() != destino.read_text()
    destino.write_text(out.getvalue())


@pytest.mark.parametrize(
    ("mutar", "motivo"),
    [
        (lambda m: c5._reescribir(m, _filas_c5(m), schema=champion.CRPS_BASE_RECEIPT_SCHEMA), "esquema"),
        (_csv_c5_editado_sin_resellar, "cambió"),
    ],  # fmt: skip
    ids=["esquema_ajeno", "csv_editado"],
)
def test_c5_en_campana_un_recibo_coherente_con_otro_esquema_o_sha_es_heredado(
    m5: dict, monkeypatch, mutar, motivo
) -> None:
    """Sustancia y protocolo intactos: sólo la comprobación de esquema o de sha del consumidor lo caza."""
    _producida_en_campana(m5, monkeypatch)
    mutar(m5)
    p = _estado_c5(m5)
    assert p["status"] == "heredado" and motivo in p["reason"], p


def test_c5_en_campana_sin_duckdb_legible_es_heredado_y_no_lanza(m5: dict, monkeypatch) -> None:
    """Sin catálogo contra el que medir la cobertura no hay sustancia que acreditar: problema, nunca excepción."""
    _producida_en_campana(m5, monkeypatch)
    (m5["compl"] / cx.DUCKDB_REL).unlink()
    p = _estado_c5(m5)
    assert p["status"] == "heredado" and "crps-cobertura" in p["reason"], p
