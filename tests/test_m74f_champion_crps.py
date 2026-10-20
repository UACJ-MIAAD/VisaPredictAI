"""M74-F · C5 — CRPS de la receta campeona acreditado por una transacción complementaria.

La cuarta campaña llevó a ``key_facts`` el CRPS de julio (deuda 1) y `run_champion_crps` sólo sabe sellar
dentro de una campaña ``running``: la madre ya está ``computed``. Opción A del autor (12-oct): un propósito
`champion_crps` de complementaria, sin lanes ni importaciones, cuyo lector re-acredita el recibo y la
sustancia del CSV.

Integración con las APIs REALES (contrato, CLI `run_complement`, `run_champion_crps.main`, git real y la
DuckDB del builder real, como `test_m74f_complemento_cableado.py`). Sólo el cálculo por serie
(`_score_series`) es un doble determinista: es el productor caro. Ninguna prueba toca `reports/` vivo.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from tests.test_m74f_complemento_cableado import (  # noqa: F401 — `mundo` y el autouse son fixtures
    MADRE,
    _cli,
    _codigo_del_checkout,
    _en_curso,
    _escribir,
    _git,
    _huellas,
    mundo,
)

pytest.importorskip("darts")

import run_champion_crps as rcc

from vp_model import artifact_receipt as ar
from vp_model import champion, dataset
from vp_model import complement as cx

ID_C5 = "compl_m74f_c5_t1"
RECETAS = {"FAD": ["theta", "ets", "sarima"], "DFF": ["sarima"]}
MANIFIESTO = json.dumps({t: {"models": m, "agg": "median"} for t, m in RECETAS.items()}, indent=2) + "\n"
COLUMNAS = ["table", "country", "category", "n_holdout_F", "crps", "cov95", "k95",
            "cov95_ci_lo", "cov95_ci_hi", "n_series", "insufficient_n", "recipe"]  # fmt: skip


@pytest.fixture
def m5(mundo: dict) -> dict:  # noqa: F811 — la fixture importada
    """El mundo de C4/C3 con la receta campeona versionada (lo que C5 sella como entrada congelada)."""
    _escribir(mundo["compl"], cx.MANIFIESTO_CAMPEON_REL, MANIFIESTO)
    _git(mundo["compl"], "add", "-A")
    _git(mundo["compl"], "commit", "-qm", "receta campeona")
    return mundo


def _catalogo(m: dict, tabla: str) -> list[tuple[str, str]]:
    cat = dataset.list_series(table=tabla, block="family", db_path=m["compl"] / cx.DUCKDB_REL)
    return [(str(r.country), str(r.category)) for r in cat.itertuples()]


def _fila(tabla: str, pais: str, cat: str) -> dict:
    n, k = 24, 20 + (len(pais) + len(cat)) % 4
    return {"table": tabla, "country": pais, "category": cat, "n_holdout_F": n,
            "crps": round(10 + len(pais) * 1.37 + len(cat), 2), "cov95": round(k / n, 4), "k95": k}  # fmt: skip


def _productor(monkeypatch, m: dict, *, omitir: tuple = (), durante=None) -> None:
    """`_score_series` doble; el resto de `run_champion_crps` (catálogo, CSV, sello) es el real."""
    root = m["compl"]
    monkeypatch.setattr(rcc, "REPORTS", root / "reports")
    monkeypatch.setattr(rcc, "OUT", root / cx.CRPS_REL)
    monkeypatch.setattr(champion, "MANIFEST", root / cx.MANIFIESTO_CAMPEON_REL)
    monkeypatch.chdir(root)  # `dataset.DB_PATH` es relativa: el operador corre desde la raíz del árbol

    def puntuar(recipe, country, category, table, num_samples):
        assert list(recipe) == RECETAS[table] and num_samples == champion.CRPS_PROTOCOLO["num_samples"]
        if durante is not None:
            durante(root)
        if (table, country, category) in omitir:
            return None, "ValueError: only 3 F residuals in selection region"
        return _fila(table, country, category), None

    monkeypatch.setattr(rcc, "_score_series", puntuar)


def _c5_planificada(m: dict, capsys) -> None:
    assert _cli(m, "import", "--parent-root", str(m["madre"]), "--purpose", "champion_crps", capsys=capsys)[0] == 0
    rc, out = _cli(m, "plan", "--parent-root", str(m["madre"]), "--id", ID_C5, "--purpose", "champion_crps",
                   capsys=capsys)  # fmt: skip
    assert rc == 0 and out["status"] == "planned" and out["lanes"] == 0 and out["imports"] == [], out


def _c5_en_curso(m: dict, capsys) -> None:
    _c5_planificada(m, capsys)
    rc, out = _cli(m, "start", "--parent-root", str(m["madre"]), capsys=capsys)
    assert rc == 0 and out["status"] == "running", out


def _c5_calculada(m: dict, monkeypatch, capsys, **kw) -> None:
    _c5_en_curso(m, capsys)
    _productor(monkeypatch, m, **kw)
    rcc.main(["--complement"])


def _cerrar(m: dict, capsys) -> tuple[int, dict]:
    return _cli(m, "close", "--parent-root", str(m["madre"]), capsys=capsys)


def _filas(m: dict) -> list[dict]:
    return list(csv.DictReader(io.StringIO((m["compl"] / cx.CRPS_REL).read_text())))


def _recibo(m: dict) -> dict:
    return json.loads(ar.receipt_path(m["compl"] / cx.CRPS_REL).read_text())


def _reescribir(m: dict, filas: list[dict], *, total=None, **recibo) -> None:
    """Reescribe el CSV y RE-SELLA su recibo (sha coherente): el lector tiene que cazar la sustancia."""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=COLUMNAS, extrasaction="ignore")
    w.writeheader()
    for tabla in ("FAD", "DFF"):
        series = [f for f in filas if f["table"] == tabla and f["country"] != "ALL"]
        for f in series:
            w.writerow(f)
        n, k = sum(int(f["n_holdout_F"]) for f in series), sum(int(f["k95"]) for f in series)
        fila_total = {"table": tabla, "country": "ALL", "category": "ALL", "n_holdout_F": n,
                      "crps": round(sum(float(f["crps"]) for f in series) / len(series), 2),
                      "cov95": round(k / n, 4), "k95": k, "n_series": len(series), "recipe": "+".join(RECETAS[tabla])}  # fmt: skip
        if total is not None:
            fila_total = total(tabla, fila_total)
        w.writerow(fila_total)
    destino = m["compl"] / cx.CRPS_REL
    destino.write_text(out.getvalue())
    acta = _recibo(m)
    ar.seal(destino, schema=recibo.get("schema", acta["schema"]), campaign_id=recibo.get("campaign_id", acta["campaign_id"]),
            code_sha=acta["code_sha"], panel_sha256=acta["panel_sha256"], protocol=recibo.get("protocol", acta["protocol"]),
            coverage=recibo.get("coverage", acta["coverage"]))  # fmt: skip


# ═══════════════════════════════ control válido
def test_c5_ciclo_completo_cli_productor_real_y_lector(m5: dict, monkeypatch, capsys) -> None:
    _c5_calculada(m5, monkeypatch, capsys)
    acta = _recibo(m5)
    doc = json.loads((m5["compl"] / cx.RUTA_REL).read_text())
    assert acta["campaign_id"] == ID_C5 and acta["code_sha"] == doc["code"]["source_git_sha"]
    assert acta["protocol"] == champion.CRPS_PROTOCOLO and acta["coverage"] == {"tables": ["DFF", "FAD"], "skipped": []}
    assert doc["inputs"] == {cx.MANIFIESTO_CAMPEON_REL: cx.ar.sha256_file(m5["compl"] / cx.MANIFIESTO_CAMPEON_REL)}
    for tabla in ("FAD", "DFF"):
        series = {(f["country"], f["category"]) for f in _filas(m5) if f["table"] == tabla and f["country"] != "ALL"}
        assert series == set(_catalogo(m5, tabla)) and series, "el catálogo entero, puntuado"
    assert _cerrar(m5, capsys)[1]["status"] == "computed"
    assert _cli(m5, "accept", "--parent-root", str(m5["madre"]), capsys=capsys)[1]["status"] == "accredited"
    assert _cli(m5, "verify", "--parent-root", str(m5["madre"]), capsys=capsys)[0] == 0
    assert _huellas(m5["madre"]) == m5["huellas_madre"]


def test_c5_una_tabla_sin_ninguna_serie_puntuada_no_se_acredita(m5: dict, monkeypatch, capsys) -> None:
    """Todas las series de DFF fallan: la omisión queda declarada con su motivo, pero sin CRPS no hay cifra."""
    omitidas = tuple(("DFF", p, c) for p, c in _catalogo(m5, "DFF"))
    _c5_calculada(m5, monkeypatch, capsys, omitir=omitidas)
    assert [(o["table"], o["country"], o["category"]) for o in _recibo(m5)["coverage"]["skipped"]] == list(omitidas)
    assert all(o["reason"].startswith("ValueError") for o in _recibo(m5)["coverage"]["skipped"])
    rc, out = _cerrar(m5, capsys)
    assert rc == 2 and "crps-sustancia" in out["codes"], out


def test_c5_fuera_de_complementaria_y_de_campana_escribe_sin_firmar(m5: dict, monkeypatch) -> None:
    """Control benigno: la corrida suelta de siempre sigue funcionando y queda heredada (sin recibo)."""
    _productor(monkeypatch, m5)
    rcc.main([])
    assert (m5["compl"] / cx.CRPS_REL).is_file() and not ar.receipt_path(m5["compl"] / cx.CRPS_REL).exists()


# ═══════════════════════════════ el productor se niega
def test_c5_sin_bandera_el_productor_se_niega_en_un_arbol_con_complementaria(m5: dict, monkeypatch, capsys) -> None:
    _c5_en_curso(m5, capsys)
    _productor(monkeypatch, m5)
    with pytest.raises(SystemExit, match="--complement"):
        rcc.main([])
    assert not (m5["compl"] / cx.CRPS_REL).exists()


@pytest.mark.parametrize(
    "args", [["--limit", "2"], ["--num-samples", "50"], ["--tables", "FAD"], ["--block", "employment"]]
)
def test_c5_solo_acredita_el_protocolo_completo(m5: dict, monkeypatch, capsys, args: list[str]) -> None:
    _c5_en_curso(m5, capsys)
    _productor(monkeypatch, m5)
    with pytest.raises(SystemExit, match="corrida completa"):
        rcc.main(["--complement", *args])
    assert not (m5["compl"] / cx.CRPS_REL).exists()


def test_c5_el_productor_solo_calcula_en_running(m5: dict, monkeypatch, capsys) -> None:
    _c5_planificada(m5, capsys)
    _productor(monkeypatch, m5)
    with pytest.raises(SystemExit, match="no-en-curso"):
        rcc.main(["--complement"])
    assert not (m5["compl"] / cx.CRPS_REL).exists()


def test_c5_el_productor_rechaza_una_complementaria_de_otro_proposito(m5: dict, monkeypatch, capsys) -> None:
    _en_curso(m5, "deep_pi_conformal", capsys, cid="compl_m74f_c3_t1")
    _productor(monkeypatch, m5)
    with pytest.raises(SystemExit, match="proposito"):
        rcc.main(["--complement"])
    assert not (m5["compl"] / cx.CRPS_REL).exists()


def test_c5_un_commit_a_mitad_del_calculo_deja_el_csv_sin_firma(m5: dict, monkeypatch, capsys) -> None:
    hecho: list[int] = []

    def commit(root: Path) -> None:
        if not hecho:
            _escribir(root, "experiments/codigo.py", "X = 2\n")
            _git(root, "commit", "-qam", "cambio a mitad")
            hecho.append(1)

    _c5_en_curso(m5, capsys)
    _productor(monkeypatch, m5, durante=commit)
    with pytest.raises(SystemExit, match="no se firma"):
        rcc.main(["--complement"])
    assert not ar.receipt_path(m5["compl"] / cx.CRPS_REL).exists()


# ═══════════════════════════════ el plan y el inicio
def test_c5_sin_receta_campeona_no_planifica(mundo: dict, capsys) -> None:  # noqa: F811
    assert _cli(mundo, "plan", "--parent-root", str(mundo["madre"]), "--id", ID_C5, "--purpose", "champion_crps",
                capsys=capsys)[1]["codes"] == ["entrada-congelada"]  # fmt: skip
    assert not (mundo["compl"] / cx.RUTA_REL).exists()


def test_c5_la_api_no_planifica_sin_la_receta_entre_las_congeladas(m5: dict) -> None:
    """Por la API directa (sin el CLI, que siempre la declara): el contrato exige la receta sellada."""
    with pytest.raises(cx.ComplementoError) as e:
        cx.planificar(m5["compl"], parent_root=m5["madre"], complement_id=ID_C5, purpose="champion_crps",
                      lanes=[], seed=1, frozen_inputs={})  # fmt: skip
    assert e.value.codigos == {"entrada-congelada"}
    assert not (m5["compl"] / cx.RUTA_REL).exists()


def test_c5_receta_cambiada_tras_el_plan_bloquea_el_inicio(m5: dict, capsys) -> None:
    _c5_planificada(m5, capsys)
    otra = json.dumps(
        {"FAD": {"models": ["theta", "sarima"], "agg": "median"}, "DFF": {"models": ["sarima"], "agg": "median"}}
    )
    _escribir(m5["compl"], cx.MANIFIESTO_CAMPEON_REL, otra)
    assert (m5["compl"] / cx.MANIFIESTO_CAMPEON_REL).read_text() != MANIFIESTO
    rc, out = _cli(m5, "start", "--parent-root", str(m5["madre"]), capsys=capsys)
    assert rc == 2 and "entrada-congelada" in out["codes"]


def test_c5_un_recibo_previo_al_inicio_no_se_cuela(m5: dict, capsys) -> None:
    _c5_planificada(m5, capsys)
    _escribir(m5["compl"], cx.CRPS_REL, "table,crps\nFAD,1\n")
    _escribir(m5["compl"], cx.CRPS_REL + ".receipt.json", "{}\n")
    rc, out = _cli(m5, "start", "--parent-root", str(m5["madre"]), capsys=capsys)
    assert rc == 2 and "salidas-previas" in out["codes"]


# ═══════════════════════════════ el lector re-acredita al cerrar
def _sin_recibo(m: dict) -> None:
    ar.receipt_path(m["compl"] / cx.CRPS_REL).unlink()


def _csv_tocado(m: dict) -> None:
    p = m["compl"] / cx.CRPS_REL
    p.write_text(p.read_text().replace(",24,", ",23,", 1))


def _otra_identidad(m: dict) -> None:
    _reescribir(m, _filas(m), campaign_id="compl_otra")


def _otro_protocolo(m: dict) -> None:
    _reescribir(m, _filas(m), protocol={**champion.CRPS_PROTOCOLO, "num_samples": 50})


def _total_inflado(m: dict) -> None:
    _reescribir(m, _filas(m), total=lambda t, f: {**f, "crps": round(f["crps"] * 0.5, 2)})


def _receta_ajena(m: dict) -> None:
    _reescribir(m, _filas(m), total=lambda t, f: {**f, "recipe": "theta"} if t == "FAD" else f)


def _serie_ajena(m: dict) -> None:
    _reescribir(m, _filas(m) + [_fila("FAD", "vietnam", "F1")])


def _no_finito(m: dict) -> None:
    """Un CRPS infinito con su fila ALL coherente (también infinita): sólo la guarda de finitud lo ve."""
    filas = _filas(m)
    i = next(n for n, f in enumerate(filas) if f["table"] == "FAD" and f["country"] != "ALL")
    filas[i] = {**filas[i], "crps": "inf"}
    _reescribir(m, filas)
    assert "inf" in (m["compl"] / cx.CRPS_REL).read_text()


def _otro_esquema(m: dict) -> None:
    _reescribir(m, _filas(m), schema="champion-crps-receipt/0")


def _tablas_parciales(m: dict) -> None:
    _reescribir(m, _filas(m), coverage={"tables": ["FAD"], "skipped": []})


def _fila_incoherente(m: dict) -> None:
    """La cobertura de una fila no es k95/n; la fila ALL (que se rehace con k y n) sigue cuadrando."""
    filas = _filas(m)
    i = next(n for n, f in enumerate(filas) if f["table"] == "DFF" and f["country"] != "ALL")
    filas[i] = {**filas[i], "cov95": "0.5"}
    _reescribir(m, filas)


def _serie_repetida(m: dict) -> None:
    filas = _filas(m)
    _reescribir(m, filas + [next(f for f in filas if f["table"] == "FAD" and f["country"] != "ALL")])


def _dos_filas_all(m: dict) -> None:
    p = m["compl"] / cx.CRPS_REL
    linea = next(lin for lin in p.read_text().splitlines() if lin.startswith("FAD,ALL,"))
    p.write_text(p.read_text() + linea + "\n")
    acta = _recibo(m)
    ar.seal(p, schema=acta["schema"], campaign_id=acta["campaign_id"], code_sha=acta["code_sha"],
            panel_sha256=acta["panel_sha256"], protocol=acta["protocol"], coverage=acta["coverage"])  # fmt: skip


@pytest.mark.parametrize(
    ("mutar", "codigo"),
    [
        (_sin_recibo, "crps-recibo"),
        (_csv_tocado, "crps-recibo"),
        (_otra_identidad, "crps-identidad"),
        (_otro_protocolo, "crps-protocolo"),
        (_total_inflado, "crps-sustancia"),
        (_receta_ajena, "crps-sustancia"),
        (_serie_ajena, "crps-cobertura"),
        (_no_finito, "crps-sustancia"),
        (_otro_esquema, "crps-recibo"),
        (_tablas_parciales, "crps-protocolo"),
        (_fila_incoherente, "crps-sustancia"),
        (_serie_repetida, "crps-sustancia"),
        (_dos_filas_all, "crps-sustancia"),
    ],
)
def test_c5_el_lector_no_cree_al_escritor(m5: dict, monkeypatch, capsys, mutar, codigo: str) -> None:
    _c5_calculada(m5, monkeypatch, capsys)
    mutar(m5)
    rc, out = _cerrar(m5, capsys)
    assert rc == 2 and codigo in out["codes"], out
    assert json.loads((m5["compl"] / cx.RUTA_REL).read_text())["status"] == "failed"


def test_c5_sin_csv_no_cierra(m5: dict, capsys) -> None:
    _c5_en_curso(m5, capsys)
    rc, out = _cerrar(m5, capsys)
    assert rc == 2 and out["codes"] == ["crps-resultados"]


def test_c5_el_lector_rechaza_el_csv_tocado_despues_de_acreditar(m5: dict, monkeypatch, capsys) -> None:
    _c5_calculada(m5, monkeypatch, capsys)
    assert _cerrar(m5, capsys)[0] == 0
    assert _cli(m5, "accept", "--parent-root", str(m5["madre"]), capsys=capsys)[0] == 0
    _csv_tocado(m5)
    rc, out = _cli(m5, "verify", "--parent-root", str(m5["madre"]), capsys=capsys)
    assert rc == 2 and "crps-recibo" in out["codes"]
    assert json.loads((m5["compl"] / cx.RUTA_REL).read_text())["status"] == "accredited", "leer no escribe"


# ═══════════════════════════════ cobertura sobre un catálogo de varias series (doble del catálogo)
CATALOGO = [("mexico", "F1"), ("india", "F1"), ("china", "F2A")]


@pytest.fixture
def raiz_cobertura(tmp_path: Path, monkeypatch) -> Path:
    """Un árbol con la receta y un catálogo de tres series por tabla: lo que el mini panel no tiene."""
    import pandas as pd

    _escribir(tmp_path, cx.MANIFIESTO_CAMPEON_REL, MANIFIESTO)
    monkeypatch.setattr(dataset, "list_series", lambda **kw: pd.DataFrame(CATALOGO, columns=["country", "category"]))
    return tmp_path


def _csv_cobertura(root: Path, puntuadas: dict[str, list[tuple[str, str]]]) -> Path:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=COLUMNAS, extrasaction="ignore")
    w.writeheader()
    for tabla, series in puntuadas.items():
        filas = [_fila(tabla, p, c) for p, c in series]
        for f in filas:
            w.writerow(f)
        n, k = sum(f["n_holdout_F"] for f in filas), sum(f["k95"] for f in filas)
        w.writerow({"table": tabla, "country": "ALL", "category": "ALL", "n_holdout_F": n, "k95": k,
                    "crps": round(sum(f["crps"] for f in filas) / len(filas), 2), "cov95": round(k / n, 4),
                    "n_series": len(filas), "recipe": "+".join(RECETAS[tabla])})  # fmt: skip
    return _escribir(root, cx.CRPS_REL, out.getvalue())


def _omitida(tabla: str, serie: tuple[str, str], motivo: str = "ValueError: sin F") -> dict:
    return {"table": tabla, "country": serie[0], "category": serie[1], "reason": motivo}


def _codigos(root: Path, puntuadas: dict, omitidas: list) -> set[str]:
    return {c for c, _ in cx.sustancia_crps(root, _csv_cobertura(root, puntuadas), omitidas)}


def test_cobertura_puntuadas_mas_omitidas_declaradas_igual_al_catalogo(raiz_cobertura: Path) -> None:
    puntuadas = {"FAD": CATALOGO, "DFF": CATALOGO[:2]}
    assert _codigos(raiz_cobertura, puntuadas, [_omitida("DFF", CATALOGO[2])]) == set()


def test_cobertura_una_serie_no_vale_por_el_catalogo_entero(raiz_cobertura: Path) -> None:
    assert _codigos(raiz_cobertura, {"FAD": CATALOGO[:1], "DFF": CATALOGO[:1]}, []) == {"crps-cobertura"}


@pytest.mark.parametrize("motivo", ["", "   "])
def test_cobertura_una_omision_sin_motivo_no_se_declara(raiz_cobertura: Path, motivo: str) -> None:
    puntuadas = {"FAD": CATALOGO, "DFF": CATALOGO[:2]}
    assert _codigos(raiz_cobertura, puntuadas, [_omitida("DFF", CATALOGO[2], motivo)]) == {"crps-cobertura"}


def test_cobertura_una_serie_no_puede_estar_puntuada_y_omitida(raiz_cobertura: Path) -> None:
    puntuadas = {"FAD": CATALOGO, "DFF": CATALOGO}
    assert _codigos(raiz_cobertura, puntuadas, [_omitida("DFF", CATALOGO[0])]) == {"crps-cobertura"}


def test_cobertura_sin_lista_de_omitidas_no_se_acredita(raiz_cobertura: Path) -> None:
    assert _codigos(raiz_cobertura, {"FAD": CATALOGO, "DFF": CATALOGO}, None) == {"crps-cobertura"}  # type: ignore[arg-type]
