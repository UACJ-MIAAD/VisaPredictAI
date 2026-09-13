"""M74-E-R1 · `holdout_forecasts` tiene productor, recibo y re-acreditación al leer.

El agujero, encontrado auditando el cierre de M74-E: el runbook canónico anunciaba en su etapa 3
«holdout_forecasts frescos» y en la 4 «combinadores sobre holdouts frescos», **y nadie los
escribía**. El único escritor era `vp_model/persist_forecasts.py`, que `run_rederivation.sh` no
invocaba nunca; su entrada era `persist_missing()`, que ante un CSV con los nombres esperados
devolvía **el archivo intacto**. Una re-derivación completa terminaba en verde con ensembles,
conformal, stacking, FFORMA, campeón y tablas de significancia calculados sobre la añada anterior.

★ **Medido sobre el artefacto vivo (26-ago) contra el panel de hoy**, que es lo que fija el RED de
integración: FAD **472 claves ausentes y 448 no esperadas**; DFF **754 y 346**. No era sólo «de otra
añada»: su ventana de hold-out dejó de existir cuando el panel creció con ago/sep-2026.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

# ⚠️ OCTAVA reincidencia de la trampa, cazada por su guardián: `vp_model.persist_forecasts` arrastra
# `darts` y el job base sólo instala `.[dev]`. No se salta el archivo entero —la mitad de estas
# pruebas leen texto y AST y deben correr en los DOS jobs—, sino cada prueba que toca el producto.
MODELADO = pytest.mark.skipif(importlib.util.find_spec("darts") is None, reason="extra `model` (darts) ausente")

RUNBOOK = RAIZ / "experiments" / "run_rederivation.sh"
CAMPANA = RAIZ / "experiments" / "run_campaign.sh"


def _vivas(p: Path) -> str:
    """Sólo lo que el shell ejecuta: un comentario que nombra un guion no lo invoca."""
    return "\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))


# ═══════════════════════════════ 1 · el camino real lo produce, y antes de consumirlo
def test_el_runbook_reconstruye_los_holdouts() -> None:
    """★ RED contra `41cb225`: allí ninguna etapa invocaba al único escritor."""
    vivo = _vivas(RUNBOOK)
    assert "vp_model.persist_forecasts" in vivo, (
        "el runbook no reconstruye holdout_forecasts; los combinadores consumirían la añada anterior"
    )


def test_se_reconstruye_ANTES_de_todos_sus_consumidores() -> None:
    """El orden ES la garantía. Cada consumidor, comprobado por separado."""
    vivo = _vivas(RUNBOOK)
    i_prod = vivo.index("vp_model.persist_forecasts")
    for consumidor in (
        "run_ensembles.py",
        "improve_conformal.py",
        "improve_stacking.py",
        "improve_fforma.py",
        "significance_tables.py",
        "run_champion_challenger.py",
    ):
        assert i_prod < vivo.index(consumidor), f"{consumidor} corre ANTES de que exista su insumo"


def test_la_campana_ya_no_calcula_ensembles_prematuros() -> None:
    """Corrían en la etapa 1 sobre holdouts que la 3.5 aún no había reconstruido: siempre viejos.

    Y desde que los consumidores se re-acreditan, dejarlo ahí ABORTARÍA la etapa 1.
    """
    assert "run_ensembles.py" not in _vivas(CAMPANA), "los ensembles prematuros siguen en run_campaign.sh"


def test_las_etiquetas_del_runbook_no_prometen_lo_que_no_hacen() -> None:
    """La etapa 3 anunciaba holdouts frescos que no producía: una etiqueta falsa es peor que
    ninguna, porque quien lee la bitácora deja de buscar."""
    texto = RUNBOOK.read_text(encoding="utf-8")
    i3 = texto.index('stage 3 "')
    i35 = texto.index('stage 3.5 "')
    assert "holdout_forecasts" not in texto[i3:i35].split("\n")[0]
    # ⚠️ Sobre la línea que ANUNCIA la duración, no sobre el archivo entero: el comentario que
    # explica por qué subió la estimación cita la cifra vieja, y buscarla en el texto acusaba a la
    # propia explicación. Tercera vez en este lote que un inventario por texto delata al que
    # documenta el patrón; se comprueba el ANUNCIO, que es el hecho.
    anuncio = next(ln for ln in texto.splitlines() if ln.startswith("# Uso (desde la raíz"))
    assert "≥16 h" in anuncio and "8-11" not in anuncio, anuncio


# ═══════════════════════════════ 2 · el productor: completo, atómico, fail-closed
def test_persist_missing_ya_no_existe() -> None:
    """★ Era la puerta por la que un CSV de julio sobrevivía a una campaña entera.

    Por AST, no por texto: la docstring del módulo lo NOMBRA al explicar por qué se retiró.
    """
    import ast

    arbol = ast.parse((RAIZ / "vp_model" / "persist_forecasts.py").read_text(encoding="utf-8"))
    definidas = {n.name for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)}
    assert "persist_missing" not in definidas
    assert "rebuild" in definidas and "read_accredited" in definidas


@MODELADO
def test_el_conjunto_esperado_se_deriva_del_panel_y_del_registro() -> None:
    """No de lo producido: preguntarle al sospechoso si está completo no es comprobar."""
    from vp_model import persist_forecasts as pf
    from vp_model.model_registry import HOLDOUT_POOL_MODELS

    esperado = pf.expected_keys("FAD")
    assert {m for m, _c, _k, _d in esperado} == set(HOLDOUT_POOL_MODELS)
    assert len(esperado) == 25 * len(HOLDOUT_POOL_MODELS) * 24, "25 series × pool × 24 meses de hold-out"


def test_el_pool_sale_del_registro_y_no_de_una_constante_local() -> None:
    """`CURATED` era la CUARTA lista independiente del repositorio."""
    import ast

    fuente = (RAIZ / "vp_model" / "persist_forecasts.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    asignadas = {
        t.id for n in ast.walk(arbol) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
    }
    assert "CURATED" not in asignadas, "volvió a aparecer una lista de modelos escrita a mano"
    assert "HOLDOUT_POOL_MODELS" in fuente


@MODELADO
def test_una_cobertura_incompleta_no_pasa() -> None:
    from vp_model import persist_forecasts as pf

    esperado = {("ets", "mexico", "F1", "2024-01-01"), ("theta", "mexico", "F1", "2024-01-01")}
    filas = [{"model": "ets", "country": "mexico", "category": "F1", "date": "2024-01-01", "actual": 1, "forecast": 2}]
    with pytest.raises(pf.HoldoutForecastsError, match="AUSENTE"):
        pf.audit(filas, esperado)


@MODELADO
def test_una_clave_duplicada_no_pasa_aunque_el_conteo_cuadre() -> None:
    """★ Un conteo es una coincidencia; un conjunto es una identidad."""
    from vp_model import persist_forecasts as pf

    esperado = {("ets", "mexico", "F1", "2024-01-01"), ("theta", "mexico", "F1", "2024-01-01")}
    fila = {"model": "ets", "country": "mexico", "category": "F1", "date": "2024-01-01", "actual": 1, "forecast": 2}
    with pytest.raises(pf.HoldoutForecastsError, match="DUPLICADA"):
        pf.audit([fila, dict(fila)], esperado)  # dos filas, el conteo cuadra


@MODELADO
def test_una_clave_no_esperada_no_pasa() -> None:
    """Las 448 y 346 «no esperadas» del artefacto vivo son fechas fuera de la ventana actual."""
    from vp_model import persist_forecasts as pf

    esperado = {("ets", "mexico", "F1", "2024-01-01")}
    filas = [
        {"model": "ets", "country": "mexico", "category": "F1", "date": "2024-01-01", "actual": 1, "forecast": 2},
        {"model": "ets", "country": "mexico", "category": "F1", "date": "2019-01-01", "actual": 1, "forecast": 2},
    ]
    with pytest.raises(pf.HoldoutForecastsError, match="NO ESPERADA"):
        pf.audit(filas, esperado)


@MODELADO
def test_sin_identidad_de_campana_el_productor_aborta() -> None:
    """Un artefacto sin procedencia es peor que ninguno: tiene el mismo aspecto que uno bueno."""
    fin = subprocess.run(
        [sys.executable, "-m", "vp_model.persist_forecasts"],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=300,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "PYTHONPATH": str(RAIZ)},
    )
    assert fin.returncode != 0
    # ⚠️ M74-E-R2 cambió el mensaje, y a mejor: la identidad ya no se le pide al entorno sino a la
    # TRANSACCIÓN, así que lo primero que falta es ella. La prueba sigue el hecho, no la cadena.
    salida = fin.stdout + fin.stderr
    assert "transacción de campaña" in salida or "CAMPAIGN_ID" in salida, salida


# ═══════════════════════════════ 3 · ★ el RED end-to-end contra el insumo anterior
@pytest.fixture
def escena(tmp_path: Path):
    """Un checkout de mentira con campaña sellada y un `holdout_forecasts` de OTRA añada."""
    import hashlib

    raiz = tmp_path / "repo"
    (raiz / "reports" / "campaign").mkdir(parents=True)
    (raiz / "reports" / "eval").mkdir(parents=True)
    (raiz / "data" / "processed").mkdir(parents=True)
    panel = raiz / "data" / "processed" / "visa_panel_long.csv"
    panel.write_text("country,category,table,value\nmexico,F1,FAD,1\n", encoding="utf-8")
    panel_sha = "sha256:" + hashlib.sha256(panel.read_bytes()).hexdigest()
    (raiz / "reports" / "campaign" / "campaign.json").write_text(
        json.dumps(
            {"campaign_id": "camp_r1", "source_git_sha": "b" * 40, "panel_sha256": panel_sha, "status": "running"}
        ),
        encoding="utf-8",
    )
    # el artefacto de la añada anterior: existe, es legible, y NO tiene recibo
    (raiz / "reports" / "eval" / "holdout_forecasts_FAD.csv").write_text(
        "model,country,category,date,actual,forecast\nets,mexico,F1,2019-01-01,1,2\n", encoding="utf-8"
    )
    return raiz, "camp_r1", "b" * 40, panel_sha


@MODELADO
def test_RED_el_consumidor_rechaza_el_holdout_de_la_anada_anterior(tmp_path, monkeypatch) -> None:
    """★ EL RED: contra `41cb225` esto PASABA — se leía con `pd.read_csv` y se puntuaba.

    El archivo está ahí, se abre y tiene columnas correctas. Lo único que le falta es proceder de
    esta campaña, y eso es exactamente lo que nadie comprobaba.

    ⚠️ M74-E-R3: la escena es ahora COHERENTE con el repositorio vivo (transacción `running`, HEAD
    y panel reales) y lo único que falta es el recibo. Antes se pasaba la identidad por parámetro,
    que resultó ser el atajo que R3 tuvo que cerrar.
    """
    from tests import holdout_fixture as hf
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch)
    (reports / "eval" / "holdout_forecasts_FAD.csv").write_text(
        "model,country,category,date,actual,forecast\nets,mexico,F1,2019-01-01,1,2\n", encoding="utf-8"
    )
    with pytest.raises(ar.ReceiptError, match="recibo"):
        pf.read_accredited("FAD", reports=reports)


@MODELADO
def test_un_recibo_de_OTRA_campana_no_acredita(tmp_path, monkeypatch) -> None:
    """El artefacto es impecable; el recibo dice que lo produjo otra corrida."""
    from tests import holdout_fixture as hf
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch)
    destino = hf.artefacto_completo(reports, "FAD", campaign_id="otra_campana")
    assert destino.is_file()
    with pytest.raises(ar.ReceiptError, match="otra corrida"):
        pf.read_accredited("FAD", reports=reports)


@MODELADO
def test_un_csv_tocado_despues_del_sellado_no_acredita(escena) -> None:
    """El recibo prueba que nadie lo tocó DESDE entonces; por eso se re-verifica al leer."""
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    raiz, cid, sha, panel_sha = escena
    destino = pf.artifact_path("FAD", raiz / "reports")
    ar.seal(
        destino,
        schema=pf.SCHEMA,
        campaign_id=cid,
        code_sha=sha,
        panel_sha256=panel_sha,
        protocol={**pf.PROTOCOL, "block": "family"},
        coverage={"n_rows": 1},
    )
    protocolo = {**pf.PROTOCOL, "block": "family"}
    # ⚠️ El control se hace sobre `ar.verify`, no sobre `read_accredited`: desde M74-E-R2 el lector
    # RECALCULA la cobertura contra el panel real, y este artefacto sintético —correctamente— ya no
    # la satisface. Lo que esta prueba fija es la detección del CAMBIO tras el sellado.
    ar.verify(destino, schema=pf.SCHEMA, campaign_id=cid, code_sha=sha, panel_sha256=panel_sha, protocol=protocolo)
    destino.write_text(destino.read_text(encoding="utf-8") + "ets,mexico,F1,2019-02-01,9,9\n", encoding="utf-8")
    with pytest.raises(ar.ReceiptError, match="cambió desde el sellado"):
        ar.verify(destino, schema=pf.SCHEMA, campaign_id=cid, code_sha=sha, panel_sha256=panel_sha, protocol=protocolo)


@MODELADO
def test_un_recibo_con_otro_regimen_no_acredita(tmp_path, monkeypatch) -> None:
    """Cambiar el hold-out o el bloque y reutilizar el artefacto sería comparar otra cosa."""
    import json

    from tests import holdout_fixture as hf
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    reports = hf.escena_coherente(tmp_path, monkeypatch)
    destino = hf.artefacto_completo(reports, "FAD")
    recibo = destino.with_name(destino.name + ".receipt.json")
    acta = json.loads(recibo.read_text(encoding="utf-8"))
    acta["protocol"]["holdout_months"] = 12
    recibo.write_text(json.dumps(acta), encoding="utf-8")
    with pytest.raises(ar.ReceiptError, match="otro régimen"):
        pf.read_accredited("FAD", reports=reports)


def test_fuera_de_una_campana_leer_es_un_error_explicito(escena) -> None:
    """Sin `CAMPAIGN_ID` no hay identidad que acreditar, y un valor por defecto sería consumir
    una añada cualquiera. Levanta nombrando la causa en vez de devolver algo plausible."""
    from vp_model import artifact_receipt as ar

    raiz, *_ = escena
    import os

    guardado = {k: os.environ.pop(k, None) for k in ("CAMPAIGN_ID", "CAMPAIGN_SHA")}
    try:
        with pytest.raises(ar.ReceiptError, match="CAMPAIGN_ID"):
            ar.campaign_identity(raiz / "reports", code_root=RAIZ)
    finally:
        for k, v in guardado.items():
            if v is not None:
                os.environ[k] = v


# ═══════════════════════════════ 4 · ningún consumidor se salta la puerta
def test_ningun_consumidor_lee_el_csv_a_pelo() -> None:
    """★ Eran NUEVE `pd.read_csv` repartidos (7 con el literal en el argumento + 2 por variable). Por AST: un docstring que lo mencione no cuenta."""
    import ast

    culpables = []
    for py in [*(RAIZ / "experiments").glob("*.py"), *(RAIZ / "vp_model").glob("*.py")]:
        if py.name == "persist_forecasts.py":
            continue  # el productor sí lee su propio artefacto, tras verificarlo
        arbol = ast.parse(py.read_text(encoding="utf-8"))
        for n in ast.walk(arbol):
            # ⚠️ Sólo las f-strings que son ARGUMENTO de una lectura. Mi primera versión marcaba
            # cualquier f-string que nombrara el artefacto, y acusó al MENSAJE DE ERROR de
            # `champion.py:165` («modelos ausentes en holdout_forecasts_FAD»), que no lee nada.
            if not (isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("read_csv", "read_parquet")):
                continue
            for trozo in ast.walk(n):
                if isinstance(trozo, ast.Constant) and "holdout_forecasts_" in str(trozo.value):
                    culpables.append(py.name)
    assert not culpables, f"leen el artefacto sin acreditarlo: {sorted(set(culpables))}"
