"""M74-E-R2 · el lector acreditaba la PROCEDENCIA y daba por buena la SUSTANCIA.

La auditoría del autor reprodujo tres ataques contra el cierre de R1 (`d7b9686`), y los tres
pasaban. Tenían la misma forma: verificar de dónde viene un artefacto y luego **creerle al escritor
lo que dice contener**.

1. **Un CSV de UNA fila con un recibo que declaraba 5 400** fue aceptado enteró por
   `read_accredited`: identidad y sha256 cuadraban, y la cobertura no se volvía a medir.
2. **`campaign_identity` aceptó una transacción `failed`** —el estado de la escena preservada— y un
   `CAMPAIGN_ID`/`CAMPAIGN_SHA` del entorno que **contradecían** `campaign.json`: de la transacción
   sólo leía el `panel_sha256`.
3. **`audit()` aceptó `actual=NaN` y `forecast=inf`**: comprobaba que estuvieran las filas correctas,
   no que dijeran algo. Un NaN se propaga a la media del ensemble; un infinito revienta el MASE.

Cada prueba de aquí es uno de esos ataques, y **todas pasan contra `d7b9686`**.
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

MODELADO = pytest.mark.skipif(importlib.util.find_spec("darts") is None, reason="extra `model` (darts) ausente")


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=RAIZ, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def escena(tmp_path: Path, monkeypatch):
    """Una campaña COHERENTE con el repositorio vivo: es el control del que parten los ataques.

    ⚠️ `importorskip` porque el sellado necesita `persist_forecasts`, que arrastra `darts`: el job
    base sólo instala `.[dev]`. Es la NOVENA vez que este repositorio pisa esa trampa, y otra vez
    la cazó su guardián antes que yo.
    """
    pytest.importorskip("darts")
    from vp_model import artifact_receipt as ar

    head = _head()
    panel_sha = "sha256:" + ar.sha256_file(RAIZ / ar.PANEL_REL)
    reports = tmp_path / "reports"
    (reports / "eval").mkdir(parents=True)
    (reports / "campaign").mkdir(parents=True)

    def sellar(*, status="running", cid="camp_r2", sha=head, psha=panel_sha, cobertura=None, esperado=None):
        from vp_model import persist_forecasts as pf

        (reports / "campaign" / "campaign.json").write_text(
            json.dumps({"campaign_id": cid, "panel_sha256": psha, "status": status, "source_git_sha": sha}),
            encoding="utf-8",
        )
        monkeypatch.setenv("CAMPAIGN_ID", cid)
        monkeypatch.setenv("CAMPAIGN_SHA", sha)
        destino = reports / "eval" / "holdout_forecasts_FAD.csv"
        destino.write_text(
            "model,country,category,date,actual,forecast\nets,mexico,F1,2024-01-01,1.0,2.0\n", encoding="utf-8"
        )
        ar.seal(
            destino,
            schema=pf.SCHEMA,
            campaign_id=cid,
            code_sha=sha,
            panel_sha256=psha,
            protocol={**pf.PROTOCOL, "block": "family"},
            coverage=cobertura or {"n_rows": 5400, "n_keys": 5400, "n_models": 9},
            expected_keys=esperado or {("ets", "mexico", "F1", "2024-01-01")},
        )
        return reports, cid, sha, psha

    return sellar


# ═══════════════════════════════ ATAQUE 1 · la cobertura declarada por el escritor
@MODELADO
def test_un_csv_de_una_fila_con_recibo_que_declara_5400_no_pasa(escena) -> None:
    """★ EL ataque. Contra `d7b9686` devolvía la única fila y el llamador seguía tan contento."""
    from vp_model import persist_forecasts as pf

    reports, _cid, _sha, _psha = escena()
    with pytest.raises(pf.HoldoutForecastsError, match="cobertura rota"):
        pf.read_accredited("FAD", reports=reports)


@MODELADO
def test_el_conjunto_esperado_se_RECALCULA_y_no_se_lee_del_recibo(escena) -> None:
    """Un recibo que fija otro universo no puede reinterpretar la cobertura a su favor."""
    from vp_model import persist_forecasts as pf

    reports, cid, sha, psha = escena()
    esperado = pf.expected_keys("FAD")
    assert len(esperado) == 5400, "el conjunto sale del panel, no del recibo"


def test_la_huella_del_conjunto_esperado_tiene_UNA_implementacion() -> None:
    """Sellar con una fórmula y releer con otra serían dos universos que parecen el mismo."""
    from vp_model import artifact_receipt as ar

    claves = {("a", "b", "c", "d"), ("e", "f", "g", "h")}
    assert ar.expected_keys_sha256(claves) == ar.expected_keys_sha256(set(reversed(sorted(claves))))


# ═══════════════════════════════ ATAQUE 2 · la identidad
def test_una_transaccion_failed_no_presta_su_identidad(escena) -> None:
    """La escena preservada está en `failed`: servía igual de identidad."""
    from vp_model import artifact_receipt as ar

    reports, *_ = escena(status="failed")
    with pytest.raises(ar.ReceiptError, match="'failed'"):
        ar.campaign_identity(reports, code_root=RAIZ)


@pytest.mark.parametrize("estado", ["computed", "validated", "published"])
def test_ningun_estado_terminal_presta_identidad(escena, estado) -> None:
    """No es sólo `failed`: una campaña que ya terminó no está produciendo artefactos."""
    from vp_model import artifact_receipt as ar

    reports, *_ = escena(status=estado)
    with pytest.raises(ar.ReceiptError, match="'running'"):
        ar.campaign_identity(reports, code_root=RAIZ)


def test_un_entorno_que_contradice_la_transaccion_no_pasa(escena, monkeypatch) -> None:
    """★ `CAMPAIGN_ID` del entorno decía otra cosa y ganaba: la identidad salía de ahí."""
    from vp_model import artifact_receipt as ar

    reports, *_ = escena()
    monkeypatch.setenv("CAMPAIGN_ID", "OTRA_CAMPANA_INVENTADA")
    with pytest.raises(ar.ReceiptError, match="otra corrida"):
        ar.campaign_identity(reports, code_root=RAIZ)


def test_el_sha_de_la_campana_debe_ser_el_HEAD_vivo(escena) -> None:
    """Commitear a mitad de campaña deja los artefactos con identidades mezcladas (bug de julio)."""
    from vp_model import artifact_receipt as ar

    reports, *_ = escena(sha="a" * 40)
    with pytest.raises(ar.ReceiptError):
        ar.campaign_identity(reports, code_root=RAIZ)


def test_el_panel_en_disco_debe_ser_el_panel_sellado(escena) -> None:
    """Si el panel cambió, los artefactos se calcularon sobre otros datos."""
    from vp_model import artifact_receipt as ar

    reports, *_ = escena(psha="sha256:" + "0" * 64)
    with pytest.raises(ar.ReceiptError, match="otros datos"):
        ar.campaign_identity(reports, code_root=RAIZ)


def test_la_identidad_sale_de_la_TRANSACCION_no_del_entorno(escena) -> None:
    """El control: coherente todo, y el valor devuelto es el de la transacción."""
    from vp_model import artifact_receipt as ar

    reports, cid, sha, psha = escena()
    assert ar.campaign_identity(reports, code_root=RAIZ) == (cid, sha, psha)


def test_un_recibo_con_claves_duplicadas_no_pasa(escena) -> None:
    """Un segundo `campaign_id` escondido en el JSON lo decidiría el orden del parser."""
    from vp_model import artifact_receipt as ar

    reports, *_ = escena()
    recibo = reports / "eval" / "holdout_forecasts_FAD.csv.receipt.json"
    recibo.write_text('{"schema": "a", "schema": "b"}', encoding="utf-8")
    with pytest.raises(ar.ReceiptError, match="duplicada"):
        ar.loads_strict(recibo.read_text(encoding="utf-8"))


# ═══════════════════════════════ ATAQUE 3 · los valores
@MODELADO
@pytest.mark.parametrize(
    "actual,forecast",
    [(float("nan"), 2.0), (1.0, float("nan")), (1.0, float("inf")), (float("-inf"), 2.0)],
)
def test_un_valor_no_finito_no_pasa(actual, forecast) -> None:
    """★ `audit()` comprobaba que estuvieran las filas correctas, no que dijeran algo."""
    from vp_model import persist_forecasts as pf

    fila = {"model": "ets", "country": "mexico", "category": "F1", "date": "2024-01-01"}
    with pytest.raises(pf.HoldoutForecastsError, match="no finito"):
        pf.audit([{**fila, "actual": actual, "forecast": forecast}], {("ets", "mexico", "F1", "2024-01-01")})


@MODELADO
def test_valores_finitos_pasan() -> None:
    """Contraprueba: sin ella, «no finito» podría ser el veredicto de todo."""
    from vp_model import persist_forecasts as pf

    fila = {"model": "ets", "country": "mexico", "category": "F1", "date": "2024-01-01", "actual": 1.0, "forecast": 2.0}
    assert pf.audit([fila], {("ets", "mexico", "F1", "2024-01-01")})["n_rows"] == 1


# ═══════════════════════════════ el esquema exacto y el gate de completitud
@MODELADO
def test_una_cabecera_distinta_no_pasa(escena) -> None:
    """Columnas de más, de menos o en otro orden no son este artefacto aunque el hash cuadre."""
    from vp_model import artifact_receipt as ar
    from vp_model import persist_forecasts as pf

    reports, cid, sha, psha = escena()
    destino = reports / "eval" / "holdout_forecasts_FAD.csv"
    destino.write_text("model,country,category,date,forecast,actual\nets,mexico,F1,2024-01-01,2.0,1.0\n", "utf-8")
    ar.seal(
        destino,
        schema=pf.SCHEMA,
        campaign_id=cid,
        code_sha=sha,
        panel_sha256=psha,
        protocol={**pf.PROTOCOL, "block": "family"},
        coverage={"n_rows": 1, "n_keys": 1, "models": ["ets"], "n_models": 1},
        expected_keys={("ets", "mexico", "F1", "2024-01-01")},
        extra={"pool": list(pf.HOLDOUT_POOL_MODELS), "table": "FAD"},
    )
    with pytest.raises(pf.HoldoutForecastsError, match="cabecera"):
        pf.read_accredited("FAD", reports=reports)


def test_el_gate_de_completitud_exige_los_dos_recibos() -> None:
    """Contaba filas de un artefacto que nadie acreditaba."""
    import tools.check_campaign_completeness as cc

    patrones = [p for p, _w, _f in cc.EXPECTED_INPUTS]
    for t in ("FAD", "DFF"):
        assert f"reports/eval/holdout_forecasts_{t}.csv.receipt.json" in patrones


def test_el_gate_caza_un_recibo_que_miente(tmp_path: Path, monkeypatch) -> None:
    """Cobertura declarada ≠ conjunto esperado, y un CSV que cambió tras el sellado."""
    import tools.check_campaign_completeness as cc

    monkeypatch.setattr(cc, "ROOT", tmp_path)
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    csv = tmp_path / "reports" / "eval" / "holdout_forecasts_FAD.csv"
    csv.write_text("model,country,category,date,actual,forecast\nets,mexico,F1,2024-01-01,1.0,2.0\n", "utf-8")
    recibo = csv.with_name(csv.name + ".receipt.json")
    recibo.write_text(
        json.dumps(
            {
                "schema": "holdout-forecasts-receipt/1",
                "campaign_id": "c",
                "code_sha": "a" * 40,
                "panel_sha256": "sha256:x",
                "expected_keys_sha256": "deadbeef",
                "n_expected_keys": 5400,
                "coverage": {"n_keys": 1},
                "artifact_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    probs = cc._check_holdout_receipt(recibo, sealed_sha="b" * 40)
    assert any("cubre 1 claves" in p for p in probs)
    assert any("cambió desde el sellado" in p for p in probs)
    assert any("code_sha" in p for p in probs)


# ═══════════════════════════════ SARIMA comprimido: la decisión del autor, con su round-trip
@MODELADO
def test_sarima_comprimido_conserva_su_prediccion(tmp_path: Path) -> None:
    """★ Decisión del autor: SARIMA se queda en `PERSISTED_MODELS` con `joblib compress=3`
    (462.99 MiB → 180.71 MiB, 61 % menos, 50/50 conservando predicción). Aquí se comprueba el
    round-trip exacto sobre una serie corta: ajustar, guardar comprimido, cargar y predecir igual.
    """
    import joblib
    import numpy as np

    from vp_model import dataset, models

    # ⚠️ Serie REAL del panel, no sintética. Mi primera versión usaba una rampa con un seno y
    # statsmodels emitía «Non-invertible starting seasonal moving average»; con el contrato de
    # warnings de la suite eso es un error. Y una serie mal condicionada tampoco probaría el
    # round-trip que hace la campaña, que ajusta sobre estas mismas series.
    ts = models.to_timeseries(dataset.load_series("mexico", "F1", "FAD"))
    modelo = models.build_model("sarima", table="FAD")
    modelo.fit(ts)
    antes = modelo.predict(3).values().ravel()

    ruta = tmp_path / "model.pkl"
    joblib.dump(modelo, ruta, compress=3)
    despues = joblib.load(ruta).predict(3).values().ravel()
    np.testing.assert_allclose(antes, despues, rtol=0, atol=0)


def test_el_productor_guarda_comprimido() -> None:
    """Por AST: que el nivel esté escrito, no que el comentario lo prometa."""
    import ast

    arbol = ast.parse((RAIZ / "experiments" / "save_finalists.py").read_text(encoding="utf-8"))
    niveles = [
        kw.value.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "dump"
        for kw in n.keywords
        if kw.arg == "compress" and isinstance(kw.value, ast.Constant)
    ]
    assert niveles == [3], f"joblib.dump sin compress=3: {niveles}"
