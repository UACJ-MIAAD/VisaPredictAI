"""M74-F · C6 — contrato y acreditación del CRPS de referencia (`crps_fad.csv`).

`run_crps_baseline.py` escribía `crps_fad.csv` (CRPS de ARIMA, SARIMA y DeepAR sobre el hold-out FAD) sin recibo ni
lector: su único consumidor, el panel CRPS de F4, leía lo que hubiera en disco. C6 le da el mismo trato que C5 al CRPS
campeón: un propósito `baseline_crps` de complementaria (sin lanes ni importaciones), un recibo propio con el protocolo
completo (tabla, bloque, modelos, `num_samples` y semilla), un lector que re-acredita identidad, protocolo y sustancia
(modelos × catálogo de la DuckDB sellada, con las omisiones declaradas) y un consumidor (`baseline_crps_provenance`)
que sólo entrega las medias como vigentes si una transacción del MISMO árbol las acredita.

Integración con las APIs reales (contrato, CLI `run_complement`, `run_crps_baseline.main`, git real y la DuckDB del
builder real). Sólo el CRPS por modelo y serie (`_score`) es un doble determinista: es el productor caro.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

import tests.test_m74f_complemento_cableado as cab
from tests.test_m74f_complemento_cableado import _cli, _git, _huellas

pytest.importorskip("darts")

import run_crps_baseline as rcb

from vp_model import artifact_receipt as ar
from vp_model import champion, dataset
from vp_model import complement as cx
from vp_model.config import PROBABILISTIC, RANDOM_SEED

# Las fixtures del mundo git real de C4/C5, registradas también en este módulo (con el autouse del código).
mundo, _codigo_del_checkout = cab.mundo, cab._codigo_del_checkout

ID_C6 = "compl_m74f_c6_t1"
MODELOS = sorted(PROBABILISTIC)


def _catalogo(m: dict) -> list[tuple[str, str]]:
    cat = dataset.list_series(table="FAD", block="family", db_path=m["compl"] / cx.DUCKDB_REL)
    return [(str(r.country), str(r.category)) for r in cat.itertuples()]


def _crps(model: str, country: str, category: str) -> float:
    return round(20.0 + 3 * MODELOS.index(model) + len(country) * 0.7 + len(category), 6)


def _productor(monkeypatch, m: dict, *, falla: tuple = (), durante=None) -> None:
    """`_score` doble; el resto de `run_crps_baseline` (catálogo, CSV, sello) es el real."""
    root = m["compl"]
    monkeypatch.setattr(rcb, "REPORTS", root / "reports")
    monkeypatch.chdir(root)  # `dataset.DB_PATH` es relativa: el operador corre desde la raíz del árbol

    def puntuar(model, country, category, table, num_samples):
        assert table == "FAD" and num_samples == champion.CRPS_BASE_PROTOCOLO["num_samples"]
        if durante is not None:
            durante(root)
        if (model, country, category) in falla:
            raise ValueError("serie demasiado corta para el hold-out")
        return _crps(model, country, category)

    monkeypatch.setattr(rcb, "_score", puntuar)


def _planificada(m: dict, capsys) -> None:
    assert _cli(m, "import", "--parent-root", str(m["madre"]), "--purpose", "baseline_crps", capsys=capsys)[0] == 0
    rc, out = _cli(m, "plan", "--parent-root", str(m["madre"]), "--id", ID_C6, "--purpose", "baseline_crps",
                   capsys=capsys)  # fmt: skip
    assert rc == 0 and out["status"] == "planned" and out["lanes"] == 0 and out["imports"] == [], out


def _en_curso(m: dict, capsys) -> None:
    _planificada(m, capsys)
    rc, out = _cli(m, "start", "--parent-root", str(m["madre"]), capsys=capsys)
    assert rc == 0 and out["status"] == "running", out


def _calculada(m: dict, monkeypatch, capsys, **kw) -> None:
    _en_curso(m, capsys)
    _productor(monkeypatch, m, **kw)
    rcb.main(["--complement"])


def _cerrar(m: dict, capsys) -> tuple[int, dict]:
    return _cli(m, "close", "--parent-root", str(m["madre"]), capsys=capsys)


def _aceptada(m: dict, monkeypatch, capsys, **kw) -> None:
    _calculada(m, monkeypatch, capsys, **kw)
    assert _cerrar(m, capsys)[1]["status"] == "computed"
    assert _cli(m, "accept", "--parent-root", str(m["madre"]), capsys=capsys)[1]["status"] == "accredited"


def _csv(m: dict) -> Path:
    return m["compl"] / cx.CRPS_BASE_REL


def _filas(m: dict) -> list[dict]:
    return list(csv.DictReader(io.StringIO(_csv(m).read_text())))


def _recibo(m: dict) -> dict:
    return json.loads(ar.receipt_path(_csv(m)).read_text())


def _reescribir(m: dict, filas: list[dict], **recibo) -> None:
    """Reescribe el CSV y RE-SELLA el recibo con sha coherente: el lector tiene que cazar la sustancia."""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["model", "country", "category", "crps"])
    w.writeheader()
    w.writerows(filas)
    _csv(m).write_text(out.getvalue())
    acta = _recibo(m)
    ar.seal(_csv(m), schema=recibo.get("schema", acta["schema"]), campaign_id=recibo.get("campaign_id", acta["campaign_id"]),
            code_sha=acta["code_sha"], panel_sha256=acta["panel_sha256"], protocol=recibo.get("protocol", acta["protocol"]),
            coverage=recibo.get("coverage", acta["coverage"]))  # fmt: skip


def _codigos(m: dict) -> set[str]:
    doc = json.loads((m["compl"] / cx.RUTA_REL).read_text())
    return {c for c, _ in cx.verificar_crps_base(m["compl"], doc)}


def _estado(m: dict, **kw) -> dict:
    return champion.baseline_crps_provenance(reports=m["compl"] / "reports", panel=m["compl"] / cx.PANEL_REL, **kw)


# ═══════════════════════════════ protocolo
def test_el_protocolo_declara_tabla_bloque_modelos_muestras_y_semilla() -> None:
    assert champion.CRPS_BASE_PROTOCOLO == {
        "table": "FAD", "block": "family", "models": MODELOS, "num_samples": 200, "seed": RANDOM_SEED}  # fmt: skip


# ═══════════════════════════════ ciclo completo (control válido)
def test_c6_ciclo_completo_cli_productor_real_lector_y_consumidor(mundo: dict, monkeypatch, capsys) -> None:
    _aceptada(mundo, monkeypatch, capsys)
    acta = _recibo(mundo)
    doc = json.loads((mundo["compl"] / cx.RUTA_REL).read_text())
    assert acta["schema"] == champion.CRPS_BASE_RECEIPT_SCHEMA and acta["campaign_id"] == ID_C6
    assert acta["code_sha"] == doc["code"]["source_git_sha"] and acta["protocol"] == champion.CRPS_BASE_PROTOCOLO
    assert acta["coverage"] == {"table": "FAD", "skipped": []}
    assert {(f["model"], f["country"], f["category"]) for f in _filas(mundo)} == {
        (mo, p, c) for mo in MODELOS for p, c in _catalogo(mundo)}  # fmt: skip
    assert _cli(mundo, "verify", "--parent-root", str(mundo["madre"]), capsys=capsys)[0] == 0
    p = _estado(mundo, parent_root=mundo["madre"])
    esperado = {mo: sum(_crps(mo, pa, c) for pa, c in _catalogo(mundo)) / len(_catalogo(mundo)) for mo in MODELOS}
    assert p["status"] == "acreditado" and p["transaction"] == {"kind": "complement", "status": "accredited"}, p
    assert p["means"] == pytest.approx(esperado) and p["inherited_means"] is None
    assert _huellas(mundo["madre"]) == mundo["huellas_madre"]


def test_c6_una_omision_declarada_con_motivo_se_acredita(mundo: dict, monkeypatch, capsys) -> None:
    """Control: una serie que falla queda como NaN en el CSV y declarada en el recibo con su motivo."""
    pais, cat = _catalogo(mundo)[0]
    _aceptada(mundo, monkeypatch, capsys, falla=(("sarima", pais, cat),))
    assert _recibo(mundo)["coverage"]["skipped"] == [
        {"model": "sarima", "country": pais, "category": cat, "reason": "ValueError: serie demasiado corta para el hold-out"}]  # fmt: skip
    fila = next(f for f in _filas(mundo) if (f["model"], f["country"], f["category"]) == ("sarima", pais, cat))
    assert fila["crps"] == ""
    assert _estado(mundo, parent_root=mundo["madre"])["status"] == "acreditado"


def test_c6_corrida_suelta_fuera_de_transaccion_escribe_sin_firmar(mundo: dict, monkeypatch) -> None:
    _productor(monkeypatch, mundo)
    rcb.main([])
    assert _csv(mundo).is_file() and not ar.receipt_path(_csv(mundo)).exists()
    assert _estado(mundo)["status"] == "heredado"


# ═══════════════════════════════ el productor se niega
def test_c6_sin_bandera_el_productor_se_niega_en_un_arbol_con_complementaria(mundo: dict, monkeypatch, capsys) -> None:
    _en_curso(mundo, capsys)
    _productor(monkeypatch, mundo)
    with pytest.raises(SystemExit, match="--complement"):
        rcb.main([])
    assert not _csv(mundo).exists()


@pytest.mark.parametrize("args", [["--table", "DFF"], ["--block", "employment"]])
def test_c6_solo_acredita_el_protocolo_completo(mundo: dict, monkeypatch, capsys, args: list[str]) -> None:
    _en_curso(mundo, capsys)
    _productor(monkeypatch, mundo)
    with pytest.raises(SystemExit, match="corrida completa"):
        rcb.main(["--complement", *args])
    assert not _csv(mundo).exists()


def test_c6_el_productor_solo_calcula_en_running(mundo: dict, monkeypatch, capsys) -> None:
    _planificada(mundo, capsys)
    _productor(monkeypatch, mundo)
    with pytest.raises(SystemExit, match="no-en-curso"):
        rcb.main(["--complement"])
    assert not _csv(mundo).exists()


def test_c6_un_commit_a_mitad_de_calculo_deja_el_csv_sin_firma(mundo: dict, monkeypatch, capsys) -> None:
    hecho: list[int] = []

    def commit(root: Path) -> None:
        if not hecho:
            cab._escribir(root, "experiments/codigo.py", "X = 2\n")
            _git(root, "commit", "-qam", "a mitad")
            hecho.append(1)

    _en_curso(mundo, capsys)
    _productor(monkeypatch, mundo, durante=commit)
    with pytest.raises(SystemExit, match="no se firma"):
        rcb.main(["--complement"])
    assert not ar.receipt_path(_csv(mundo)).exists()


def test_c6_el_plan_rechaza_un_recibo_previo(mundo: dict, monkeypatch, capsys) -> None:
    _planificada(mundo, capsys)
    cab._escribir(mundo["compl"], cx.CRPS_BASE_REL, "model,country,category,crps\n")
    cab._escribir(mundo["compl"], str(ar.receipt_path(Path(cx.CRPS_BASE_REL))), "{}")
    rc, out = _cli(mundo, "start", "--parent-root", str(mundo["madre"]), capsys=capsys)
    assert rc == 2 and "salidas-previas" in out["codes"], out


# ═══════════════════════════════ el lector re-acredita (recibo re-sellado con sha coherente)
def _sin_recibo(m):
    ar.receipt_path(_csv(m)).unlink()


def _csv_tocado(m):
    _csv(m).write_text(_csv(m).read_text().replace(",2", ",3", 1))


def _otra_identidad(m):
    _reescribir(m, _filas(m), campaign_id="compl_otra")


def _otro_esquema(m):
    _reescribir(m, _filas(m), schema=champion.CRPS_RECEIPT_SCHEMA)


def _otras_muestras(m):
    _reescribir(m, _filas(m), protocol={**champion.CRPS_BASE_PROTOCOLO, "num_samples": 50})


def _otra_semilla(m):
    _reescribir(m, _filas(m), protocol={**champion.CRPS_BASE_PROTOCOLO, "seed": 7})


def _sin_un_modelo(m):
    _reescribir(m, [f for f in _filas(m) if f["model"] != "deepar"],
                protocol={**champion.CRPS_BASE_PROTOCOLO, "models": ["arima", "sarima"]})  # fmt: skip


def _modelo_ajeno(m):
    _reescribir(m, [*_filas(m), {**_filas(m)[0], "model": "theta"}])


def _serie_ausente(m):
    _reescribir(m, _filas(m)[1:])


def _serie_ajena(m):
    _reescribir(m, [*_filas(m), {**_filas(m)[0], "country": "atlantis"}])


def _fila_repetida(m):
    _reescribir(m, [*_filas(m), _filas(m)[0]])


def _nan_sin_declarar(m):
    _reescribir(m, [{**f, "crps": ""} if i == 0 else f for i, f in enumerate(_filas(m))])


def _declarada_pero_con_valor(m):
    f = _filas(m)[0]
    _reescribir(m, _filas(m), coverage={"table": "FAD", "skipped": [
        {"model": f["model"], "country": f["country"], "category": f["category"], "reason": "x"}]})  # fmt: skip


def _omision_sin_motivo(m):
    f = _filas(m)[0]
    _reescribir(m, [{**f, "crps": ""}, *_filas(m)[1:]], coverage={"table": "FAD", "skipped": [
        {"model": f["model"], "country": f["country"], "category": f["category"], "reason": " "}]})  # fmt: skip


def _crps_negativo(m):
    _reescribir(m, [{**f, "crps": "-1.5"} if i == 0 else f for i, f in enumerate(_filas(m))])


def _crps_infinito(m):
    _reescribir(m, [{**f, "crps": "inf"} if i == 0 else f for i, f in enumerate(_filas(m))])


def _otra_tabla(m):
    _reescribir(m, _filas(m), coverage={"table": "DFF", "skipped": []})


@pytest.mark.parametrize(
    ("mutar", "codigo"),
    [
        (_sin_recibo, "crps-recibo"),
        (_csv_tocado, "crps-recibo"),
        (_otra_identidad, "crps-identidad"),
        (_otro_esquema, "crps-recibo"),
        (_otras_muestras, "crps-protocolo"),
        (_otra_semilla, "crps-protocolo"),
        (_sin_un_modelo, "crps-protocolo"),
        (_modelo_ajeno, "crps-cobertura"),
        (_serie_ausente, "crps-cobertura"),
        (_serie_ajena, "crps-cobertura"),
        (_fila_repetida, "crps-cobertura"),
        (_nan_sin_declarar, "crps-cobertura"),
        (_declarada_pero_con_valor, "crps-cobertura"),
        (_omision_sin_motivo, "crps-cobertura"),
        (_crps_negativo, "crps-sustancia"),
        (_crps_infinito, "crps-sustancia"),
        (_otra_tabla, "crps-protocolo"),
    ],  # fmt: skip
    ids=lambda x: x.__name__ if callable(x) else x,
)
def test_c6_el_lector_rechaza(mundo: dict, monkeypatch, capsys, mutar, codigo: str) -> None:
    _calculada(mundo, monkeypatch, capsys)
    assert _codigos(mundo) == set(), "control: lo que escribe el productor real se acredita"
    mutar(mundo)
    assert codigo in _codigos(mundo)
    rc, out = _cerrar(mundo, capsys)
    assert rc == 2 and codigo in out["codes"], out


def test_c6_sin_csv_no_cierra(mundo: dict, monkeypatch, capsys) -> None:
    _calculada(mundo, monkeypatch, capsys)
    _csv(mundo).unlink()
    assert "crps-resultados" in _codigos(mundo)


# ═══════════════════════════════ el consumidor acredita la transacción
def test_c6_consumidor_sin_la_raiz_de_la_madre_es_heredado(mundo: dict, monkeypatch, capsys) -> None:
    _aceptada(mundo, monkeypatch, capsys)
    p = _estado(mundo)
    assert p["status"] == "heredado" and p["means"] is None and "madre" in p["reason"], p
    assert set(p["inherited_means"]) == set(MODELOS)


def test_c6_consumidor_complementaria_fallida_o_solo_calculada_es_heredado(mundo: dict, monkeypatch, capsys) -> None:
    _calculada(mundo, monkeypatch, capsys)
    assert _cerrar(mundo, capsys)[1]["status"] == "computed"
    assert "computed" in _estado(mundo, parent_root=mundo["madre"])["reason"]
    cx.fallar(mundo["compl"], "abortada a mano")
    p = _estado(mundo, parent_root=mundo["madre"])
    assert p["status"] == "heredado" and "failed" in p["reason"], p


def test_c6_consumidor_arbol_cambiado_tras_aceptar_es_heredado(mundo: dict, monkeypatch, capsys) -> None:
    _aceptada(mundo, monkeypatch, capsys)
    cab._escribir(mundo["compl"], "experiments/codigo.py", "X = 3\n")
    p = _estado(mundo, parent_root=mundo["madre"])
    assert p["status"] == "heredado" and "codigo-sucio" in p["reason"], p


def test_c6_un_recibo_de_baseline_no_lo_acredita_una_complementaria_de_crps_campeon(
    mundo: dict, monkeypatch, capsys
) -> None:
    """Un id que coincide no basta: sólo una complementaria `baseline_crps` responde por `crps_fad.csv`."""
    _aceptada(mundo, monkeypatch, capsys)
    leer = cx.leer
    monkeypatch.setattr(cx, "leer", lambda root: {**leer(root), "purpose": "champion_crps"})
    p = _estado(mundo, parent_root=mundo["madre"])
    assert p["status"] == "heredado" and "'champion_crps'" in p["reason"], p


# ═══════════════════════════════ consumidor · campaña del árbol (mundo simple)
@pytest.fixture
def arbol(tmp_path: Path):
    reports = tmp_path / "reports"
    (reports / "eval").mkdir(parents=True)
    panel = tmp_path / "panel.csv"
    panel.write_text("country,category,value\nmexico,F1,1\n")
    csv_p = reports / "eval" / "crps_fad.csv"
    csv_p.write_text("model,country,category,crps\narima,mexico,F1,10.0\ndeepar,mexico,F1,8.0\nsarima,mexico,F1,\n")
    ar.seal(csv_p, schema=champion.CRPS_BASE_RECEIPT_SCHEMA, campaign_id="rederiv_x", code_sha="a" * 40,
            panel_sha256="sha256:" + ar.sha256_file(panel), protocol=champion.CRPS_BASE_PROTOCOLO,
            coverage={"table": "FAD", "skipped": []})  # fmt: skip
    return reports, panel


def _campana(reports: Path, panel: Path, status: str, campaign_id: str = "rederiv_x") -> None:
    (reports / "campaign").mkdir(parents=True, exist_ok=True)
    (reports / "campaign" / "campaign.json").write_text(json.dumps({
        "status": status, "campaign_id": campaign_id, "source_git_sha": "a" * 40,
        "panel_sha256": "sha256:" + ar.sha256_file(panel)}))  # fmt: skip


def test_c6_consumidor_sin_transaccion_es_heredado(arbol) -> None:
    reports, panel = arbol
    p = champion.baseline_crps_provenance(reports=reports, panel=panel)
    assert p["status"] == "heredado" and "rederiv_x" in p["reason"], p
    assert p["inherited_means"] == {"arima": 10.0, "deepar": 8.0, "sarima": None}


@pytest.mark.parametrize("status", ["running", "computed", "validated"])
def test_c6_consumidor_la_campana_sola_no_acredita_un_csv_que_no_supera_la_validacion(arbol, status: str) -> None:
    """Auditoría C6: el fixture de juguete deja una celda vacía SIN omisión declarada y no hay DuckDB contra la que
    medir la sustancia; antes salía `acreditado` sólo porque la campaña existía. El control válido, con el productor
    real y la DuckDB del mundo, es `test_c6_en_campana_el_runbook_firma_con_la_identidad_de_la_campana`."""
    reports, panel = arbol
    _campana(reports, panel, status)
    p = champion.baseline_crps_provenance(reports=reports, panel=panel)
    assert p["status"] == "heredado" and "crps-" in p["reason"] and p["means"] is None, p


def test_c6_consumidor_campana_fallida_es_heredado(arbol) -> None:
    reports, panel = arbol
    _campana(reports, panel, "failed")
    assert champion.baseline_crps_provenance(reports=reports, panel=panel)["status"] == "heredado"


def test_c6_consumidor_esquema_del_campeon_no_vale_para_el_baseline(arbol) -> None:
    reports, panel = arbol
    _campana(reports, panel, "running")
    csv_p = reports / "eval" / "crps_fad.csv"
    acta = json.loads(ar.receipt_path(csv_p).read_text())
    ar.seal(csv_p, schema=champion.CRPS_RECEIPT_SCHEMA, campaign_id=acta["campaign_id"], code_sha=acta["code_sha"],
            panel_sha256=acta["panel_sha256"], protocol=acta["protocol"], coverage=acta["coverage"])  # fmt: skip
    assert champion.baseline_crps_provenance(reports=reports, panel=panel)["status"] == "heredado"


def test_c6_consumidor_sin_csv_es_ausente(tmp_path: Path) -> None:
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    p = champion.baseline_crps_provenance(reports=tmp_path / "reports", panel=tmp_path / "panel.csv")
    assert p["status"] == "ausente" and p["means"] is None and p["inherited_means"] is None


# ═══════════════════════════════ productor dentro de una campaña running (el camino del runbook, etapa 5)
def _campana_en_curso(m: dict, monkeypatch, cid: str = "rederiv_c6_t1") -> None:
    root = m["compl"]
    head = _git(root, "rev-parse", "HEAD").strip()
    cab._escribir(root, "reports/campaign/campaign.json", json.dumps({
        "status": "running", "campaign_id": cid, "source_git_sha": head,
        "panel_sha256": "sha256:" + ar.sha256_file(root / cx.PANEL_REL)}))  # fmt: skip
    monkeypatch.setenv("CAMPAIGN_ID", cid)
    monkeypatch.setenv("CAMPAIGN_SHA", head)


@pytest.mark.parametrize("status", ["running", "computed", "validated"])
def test_c6_en_campana_el_runbook_firma_con_la_identidad_de_la_campana(mundo: dict, monkeypatch, status: str) -> None:
    _campana_en_curso(mundo, monkeypatch)
    _productor(monkeypatch, mundo)
    rcb.main([])
    acta = _recibo(mundo)
    assert acta["campaign_id"] == "rederiv_c6_t1" and acta["protocol"] == champion.CRPS_BASE_PROTOCOLO
    txn = mundo["compl"] / cx.CAMPANA_REL
    txn.write_text(txn.read_text().replace('"running"', f'"{status}"'))
    p = _estado(mundo)
    assert p["status"] == "acreditado" and p["transaction"] == {"kind": "campaign", "status": status}, p


@pytest.mark.parametrize(
    "mutar",
    [
        _otras_muestras,
        _otra_semilla,
        _crps_negativo,
        _fila_repetida,
        _nan_sin_declarar,
        _otra_tabla,
        lambda m: _reescribir(m, _filas(m), coverage=["invalida"]),
        lambda m: _reescribir(m, _filas(m), protocol="completo"),
    ],  # fmt: skip
    ids=[
        "otras_muestras",
        "otra_semilla",
        "crps_negativo",
        "fila_repetida",
        "nan_sin_declarar",
        "otra_tabla",
        "coverage_lista",
        "protocolo_texto",
    ],  # fmt: skip
)
def test_c6_en_campana_el_consumidor_valida_el_recibo(mundo: dict, monkeypatch, mutar) -> None:
    """Auditoría C6: con la campaña del árbol válida, un recibo re-sellado con protocolo, forma o sustancia rotos
    tiene que salir `heredado` (antes: `acreditado`), y nunca lanzar."""
    _campana_en_curso(mundo, monkeypatch)
    _productor(monkeypatch, mundo)
    rcb.main([])
    mutar(mundo)
    p = _estado(mundo)
    assert p["status"] == "heredado" and p["means"] is None and "crps-" in p["reason"], p


def test_c6_complementaria_con_coverage_mal_tipada_es_heredado_y_no_lanza(mundo: dict, monkeypatch, capsys) -> None:
    _aceptada(mundo, monkeypatch, capsys)
    _reescribir(mundo, _filas(mundo), coverage=["invalida"])
    assert "crps-recibo" in _codigos(mundo)
    p = _estado(mundo, parent_root=mundo["madre"])
    assert p["status"] == "heredado" and "crps-recibo" in p["reason"], p


def test_c6_en_campana_otra_tabla_no_se_firma(mundo: dict, monkeypatch) -> None:
    """`--table DFF` escribe `crps_dff.csv`, que no es el protocolo acreditable: sin recibo."""
    _campana_en_curso(mundo, monkeypatch)
    _productor(monkeypatch, mundo)
    calls: list[str] = []

    def puntuar(model, country, category, table, num_samples) -> float:
        calls.append(table)
        return 1.0

    monkeypatch.setattr(rcb, "_score", puntuar)
    rcb.main(["--table", "DFF"])
    salida = mundo["compl"] / "reports" / "eval" / "crps_dff.csv"
    assert set(calls) == {"DFF"} and salida.is_file() and not ar.receipt_path(salida).exists()


def test_c6_en_campana_con_entorno_de_otra_corrida_no_se_firma(mundo: dict, monkeypatch) -> None:
    _campana_en_curso(mundo, monkeypatch)
    monkeypatch.setenv("CAMPAIGN_ID", "rederiv_otra")
    _productor(monkeypatch, mundo)
    rcb.main([])
    assert _csv(mundo).is_file() and not ar.receipt_path(_csv(mundo)).exists()


def _csv_editado_sin_resellar(m):
    """Una cifra cambiada a otra igual de válida, sin re-sellar: sólo el sha del recibo lo delata."""
    filas = _filas(m)
    filas[0] = {**filas[0], "crps": str(float(filas[0]["crps"]) + 1.0)}
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["model", "country", "category", "crps"])
    w.writeheader()
    w.writerows(filas)
    _csv(m).write_text(out.getvalue())


@pytest.mark.parametrize(
    ("mutar", "motivo"),
    [(_otro_esquema, "esquema"), (_csv_editado_sin_resellar, "cambió")],
    ids=["esquema_ajeno", "csv_editado"],
)
def test_c6_en_campana_un_recibo_coherente_con_otro_esquema_o_sha_es_heredado(
    mundo: dict, monkeypatch, mutar, motivo: str
) -> None:
    """Sustancia y protocolo intactos: sólo la comprobación de esquema o de sha del consumidor lo caza."""
    _campana_en_curso(mundo, monkeypatch)
    _productor(monkeypatch, mundo)
    rcb.main([])
    mutar(mundo)
    p = _estado(mundo)
    assert p["status"] == "heredado" and motivo in p["reason"], p


def test_c6_en_campana_sin_duckdb_legible_es_heredado_y_no_lanza(mundo: dict, monkeypatch) -> None:
    _campana_en_curso(mundo, monkeypatch)
    _productor(monkeypatch, mundo)
    rcb.main([])
    (mundo["compl"] / cx.DUCKDB_REL).unlink()
    p = _estado(mundo)
    assert p["status"] == "heredado" and "crps-cobertura" in p["reason"], p
