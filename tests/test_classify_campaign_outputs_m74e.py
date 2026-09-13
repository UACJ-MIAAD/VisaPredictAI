"""M74-E · La clasificación de salidas es un CONJUNTO de etiquetas, y alcanza a los punteros DVC.

Dos defectos de mi propia primera versión, ambos con la misma firma: el clasificador daba un
veredicto tranquilizador sobre cosas que no había mirado.

* **Decidía con `continue`.** El primer predicado que acertaba ganaba y los demás no llegaban a
  evaluarse: un artefacto **vacío Y construido sobre insumos rancios** salía sólo «incompleta», y
  quien leyera esa lista concluiría que basta re-ejecutar su etapa. Son predicados independientes.
* **No veía los punteros DVC.** El inventario salía de `git status reports/` filtrado a
  `.csv/.json/.jsonl`, así que `models.dvc` y `mlflow.db.dvc` —las dos salidas MAYORES de la
  campaña— caían por los dos filtros a la vez. Y el puntero no es el artefacto: fechar el texto de
  unos cientos de bytes en vez del árbol que hay detrás es mirar el sobre en lugar de la carta.

Cada prueba de aquí falla contra esa versión anterior.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from tools import classify_campaign_outputs as cc  # noqa: E402

ARRANQUE = datetime(2026, 9, 12, 1, 21, 50, tzinfo=UTC)
ANTES = ARRANQUE - timedelta(days=70)
DESPUES = ARRANQUE + timedelta(hours=3)


def _fechar(p: Path, cuando: datetime) -> None:
    os.utime(p, (cuando.timestamp(), cuando.timestamp()))


def _escribir(p: Path, texto: str, cuando: datetime) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(texto, encoding="utf-8")
    _fechar(p, cuando)
    return p


@pytest.fixture
def escena(tmp_path: Path) -> Path:
    """Un checkout con su transacción sellada. Nada más: cada prueba pone lo que necesita."""
    raiz = tmp_path / "checkout"
    (raiz / "reports" / "campaign").mkdir(parents=True)
    (raiz / "reports" / "campaign" / "campaign.json").write_text(
        json.dumps({"campaign_id": "c1", "started_at": ARRANQUE.isoformat().replace("+00:00", "Z")}),
        encoding="utf-8",
    )
    return raiz


def _clasificar(raiz: Path, rutas: list[str]) -> dict:
    return cc.classify(rutas, root=raiz, txn=raiz / "reports" / "campaign" / "campaign.json")


# ═══════════════════════════════ 1 · las etiquetas se ACUMULAN
def test_vacia_y_con_insumos_rancios_lleva_LAS_DOS_etiquetas(escena: Path) -> None:
    """★ El defecto exacto: `continue` tras la primera etiqueta escondía la segunda.

    Un artefacto a medias construido sobre insumos de otra añada es las dos cosas, y la diferencia
    es operativa: si sólo dice «incompleta», alguien re-ejecuta su etapa y hereda la contaminación.
    """
    _escribir(escena / "reports/eval/holdout_forecasts_FAD.csv", "a,b\n1,2\n", ANTES)  # insumo RANCIO
    _escribir(escena / "reports/eval/deep_pi_FAD.csv", "model,pi\n", DESPUES)  # sólo cabecera
    v = _clasificar(escena, ["reports/eval/deep_pi_FAD.csv"])["reports/eval/deep_pi_FAD.csv"]
    assert set(v["clases"]) == {cc.INCOMPLETA, cc.CONTAMINADA}
    assert "cabecera" in v["razones"][cc.INCOMPLETA]
    assert "holdout_forecasts_FAD.csv" in v["razones"][cc.CONTAMINADA]


def test_completa_nunca_convive_con_otra_etiqueta(escena: Path) -> None:
    """`completa` es la ausencia de hallazgos, no un hallazgo más: o va sola o no va."""
    _escribir(escena / "reports/eval/holdout_forecasts_FAD.csv", "a,b\n1,2\n", DESPUES)
    _escribir(escena / "reports/eval/deep_pi_FAD.csv", "model,pi\nx,1\n", DESPUES)
    v = _clasificar(escena, ["reports/eval/deep_pi_FAD.csv"])["reports/eval/deep_pi_FAD.csv"]
    assert v["clases"] == [cc.COMPLETA]


def test_cabecera_sin_filas_es_incompleta_aunque_los_insumos_esten_frescos(escena: Path) -> None:
    """Insumos impecables no vuelven utilizable un artefacto que no tiene una sola fila."""
    _escribir(escena / "reports/eval/holdout_forecasts_FAD.csv", "a,b\n1,2\n", DESPUES)
    _escribir(escena / "reports/eval/deep_pi_FAD.csv", "model,pi\n", DESPUES)
    v = _clasificar(escena, ["reports/eval/deep_pi_FAD.csv"])["reports/eval/deep_pi_FAD.csv"]
    assert v["clases"] == [cc.INCOMPLETA]


# ═══════════════════════════════ 2 · el puntero DVC no es el artefacto
def test_el_puntero_se_resuelve_a_su_carga_util(escena: Path) -> None:
    """`models.dvc` son 300 bytes de texto; lo que tiene añada es el árbol `models/`."""
    assert cc.payload_of("models.dvc") == "models"
    assert cc.payload_of("mlflow.db.dvc") == "mlflow.db"
    assert cc.payload_of("reports/eval/x.csv") == "reports/eval/x.csv"


def test_un_arbol_de_anadas_mezcladas_esta_contaminado(escena: Path) -> None:
    """★ El caso REAL: `sync_all LOCAL` re-hasheó `models.dvc` sobre un árbol mixto.

    Medido en la escena de `rederiv_1022c9d_20260911T212150`: 46 archivos de junio y julio
    conviviendo con 301 del día de la campaña, bajo un único puntero fresco.
    """
    _escribir(escena / "models/FAD/ets_mexico_F1/model.pkl", "viejo", ANTES)
    _escribir(escena / "models/FAD/arima_mexico_F1/model.pkl", "nuevo", DESPUES)
    _escribir(escena / "models.dvc", "outs:\n- path: models\n", DESPUES)
    v = _clasificar(escena, ["models.dvc"])["models.dvc"]
    assert v["payload"] == "models"
    assert v["clases"] == [cc.CONTAMINADA]
    assert "1 de 2" in v["razones"][cc.CONTAMINADA] and "MEZCLADAS" in v["razones"][cc.CONTAMINADA]


def test_un_arbol_enteramente_fresco_esta_completo(escena: Path) -> None:
    """Contraprueba: sin la contraparte, «contaminada» podría ser el veredicto de todo árbol."""
    _escribir(escena / "models/FAD/arima_mexico_F1/model.pkl", "nuevo", DESPUES)
    _escribir(escena / "models.dvc", "outs:\n- path: models\n", DESPUES)
    assert _clasificar(escena, ["models.dvc"])["models.dvc"]["clases"] == [cc.COMPLETA]


def test_un_puntero_fresco_sobre_una_carga_vieja_queda_FUERA_de_la_campana(escena: Path) -> None:
    """★ La discriminación fina: fechar el sobre en vez de la carta invierte el veredicto.

    Tocar el `.dvc` sin recalcular nada (un `dvc commit` a secas, un `touch`) deja el puntero
    fresquísimo y el artefacto intacto. Quien fechara el puntero lo daría por producido HOY.
    """
    _escribir(escena / "mlflow.db", "binario viejo", ANTES)
    _escribir(escena / "mlflow.db.dvc", "outs:\n- path: mlflow.db\n", DESPUES)
    v = _clasificar(escena, ["mlflow.db.dvc"])["mlflow.db.dvc"]
    assert v["clases"] == [cc.FUERA], "se fechó el puntero, no la carga útil"


def test_el_mtime_del_directorio_no_decide_por_el_arbol(escena: Path) -> None:
    """Un directorio con mtime viejo puede guardar archivos nuevos tres niveles abajo, y al revés.

    En la escena real `models/FAD` tenía mtime de agosto y dentro había archivos de junio: el mtime
    de un directorio sólo registra altas y bajas de sus hijos DIRECTOS.
    """
    _escribir(escena / "models/FAD/arima_mexico_F1/model.pkl", "nuevo", DESPUES)
    _escribir(escena / "models.dvc", "outs:\n- path: models\n", DESPUES)
    _fechar(escena / "models" / "FAD", ANTES)  # el directorio miente
    _fechar(escena / "models", ANTES)
    assert _clasificar(escena, ["models.dvc"])["models.dvc"]["clases"] == [cc.COMPLETA]


# ═══════════════════════════════ 3 · fail-closed y el inventario real
def test_un_artefacto_no_declarado_aborta(escena: Path) -> None:
    """Lo no declarado no se supone limpio: así se colaron las cifras contaminadas."""
    _escribir(escena / "reports/eval/invento_nuevo.csv", "a\n1\n", DESPUES)
    with pytest.raises(SystemExit, match="NO está declarado"):
        _clasificar(escena, ["reports/eval/invento_nuevo.csv"])


def test_un_puntero_dvc_no_declarado_tambien_aborta(escena: Path) -> None:
    """Resolver la carga útil no puede volverse una puerta trasera al fail-closed."""
    _escribir(escena / "data/otro.bin", "x", DESPUES)
    cc.DVC_PAYLOAD["data/otro.bin.dvc"] = "data/otro.bin"
    try:
        with pytest.raises(SystemExit, match="NO está declarado"):
            _clasificar(escena, ["data/otro.bin.dvc"])
    finally:
        del cc.DVC_PAYLOAD["data/otro.bin.dvc"]


def test_el_inventario_alcanza_la_raiz_y_no_filtra_por_extension(escena: Path) -> None:
    """★ RED del segundo defecto: `git status reports/` + filtro de extensión perdía los punteros.

    Se monta un repositorio de verdad y se comprueba que el inventario trae el `.dvc` de la RAÍZ,
    un no rastreado y una ruta con espacio (que `--porcelain` entrecomilla y rompería un `split`).
    """
    repo = escena
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    _escribir(repo / "models.dvc", "v1\n", DESPUES)
    _escribir(repo / "reports/eval/con espacio.csv", "a\n", DESPUES)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    # ⚠️ El contenido nuevo tiene que CAMBIAR DE TAMAÑO. Mi primera versión escribía "v2\n" sobre
    # "v1\n" —tres bytes por tres— y le reponía el mtime: git compara tamaño y mtime antes de leer
    # el contenido, así que daba el archivo por intacto y la prueba culpaba al producto.
    _escribir(repo / "models.dvc", "outs:\n- path: models\n  md5: cambiado\n", DESPUES)  # modificado
    _escribir(repo / "reports/eval/nuevo.csv", "a\n", DESPUES)  # no rastreado
    (repo / "reports/eval/con espacio.csv").write_text("b\n", encoding="utf-8")

    tocadas = cc._rutas_tocadas(repo)
    assert "models.dvc" in tocadas, "el puntero de la raíz no entró al inventario"
    assert "reports/eval/nuevo.csv" in tocadas
    assert "reports/eval/con espacio.csv" in tocadas, "la ruta con espacio se partió"


def test_el_comando_sale_en_rojo_cuando_hay_contaminacion(escena: Path) -> None:
    """Un clasificador que informa de contaminación y devuelve 0 es fail-open en una tubería."""
    _escribir(escena / "reports/eval/holdout_forecasts_FAD.csv", "a,b\n1,2\n", ANTES)
    _escribir(escena / "reports/eval/deep_pi_FAD.csv", "model,pi\nx,1\n", DESPUES)
    rc = cc.main(["--root", str(escena), "reports/eval/deep_pi_FAD.csv"])
    assert rc == 1
    _escribir(escena / "reports/eval/holdout_forecasts_FAD.csv", "a,b\n1,2\n", DESPUES)
    assert cc.main(["--root", str(escena), "reports/eval/deep_pi_FAD.csv"]) == 0


def test_un_artefacto_ausente_es_incompleto_y_nombra_su_carga_util(escena: Path) -> None:
    """La etapa falló y no dejó nada; el mensaje tiene que decir QUÉ ruta falta, no el puntero."""
    v = _clasificar(escena, ["models.dvc"])["models.dvc"]
    assert v["clases"] == [cc.INCOMPLETA] and "models" in v["razones"][cc.INCOMPLETA]


def test_con_json_la_salida_es_SOLO_el_documento(escena: Path, capsys) -> None:
    """Un resumen para humanos pegado detrás del JSON lo vuelve imparseable (`jq`: «Extra data»)."""
    _escribir(escena / "reports/eval/holdout_forecasts_FAD.csv", "a,b\n1,2\n", ANTES)
    _escribir(escena / "reports/eval/deep_pi_FAD.csv", "model,pi\nx,1\n", DESPUES)
    assert cc.main(["--root", str(escena), "--json", "reports/eval/deep_pi_FAD.csv"]) == 1
    cap = capsys.readouterr()
    doc = json.loads(cap.out)  # ★ si el resumen se cuela, esto revienta
    assert doc["reports/eval/deep_pi_FAD.csv"]["clases"] == [cc.CONTAMINADA]
    assert "CONTAMINADO" in cap.err, "el resumen tiene que seguir emitiéndose, sólo que por stderr"
