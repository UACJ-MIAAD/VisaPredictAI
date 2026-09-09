"""E3 · La campaña por cohorte no puede elegir su receta después de ver el resultado.

La baraja se pre-registra en ``docs/cohort_deck.json`` y el corredor solo acepta lo que está
allí: un control no asciende a primario, una receta nueva no entra, el universo no se ensancha y
un lane no puede contaminar a otro. Las receipts son el resultado —también cuando el lane
falla—, así que aquí se exige que estén completas y que nombren lo que de verdad se escribió.
"""

from __future__ import annotations

import json
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import experiments.run_global_deep as deep
from vp_model.deck import DECK_PATH, cargar_deck

ROOT = Path(__file__).resolve().parent.parent
COHORTS = ROOT / "reports" / "eval" / "series_cohorts.json"
POLICY = ROOT / "docs" / "COHORT_POLICY.md"
RECEIPTS = ROOT / "reports" / "campaign" / "e3"
CAMPANA_SELLADA = ROOT / "reports" / "campaign"


# ------------------------------------------------------------------ baraja pre-registrada
def test_la_baraja_existe_y_valida() -> None:
    deck = cargar_deck()
    assert deck.device == "cpu"
    assert deck.universe == "evaluable"
    assert set(deck.cohorts) == {"estable", "no_estable", "all"}
    assert deck.seeds == (1,)


def test_como_mucho_tres_recetas_primarias() -> None:
    deck = cargar_deck()
    assert len(deck.primarias()) <= deck.max_primary_per_cohort == 3
    assert {r.name for r in deck.primarias()} == {
        "deepar-legacy",
        "deepar-robust",
        "deepar-robust-levels",
    }


def test_los_controles_son_controles_y_no_pueden_ser_otra_cosa() -> None:
    deck = cargar_deck()
    nombres = {r.name for r in deck.controles()}
    assert {"control-bitcn", "control-patchtst", "control-tide", "control-gbm-lightgbm"} <= nombres
    assert all(not r.primary for r in deck.controles())
    # AutoDeepAR y AutoBiTCN quedan predeclarados y NO programados: la familia está cerrada.
    crudo = json.loads(DECK_PATH.read_text())
    assert set(crudo["controls_predeclared_not_scheduled"]) == {"AutoDeepAR", "AutoBiTCN"}


def test_una_receta_no_declarada_no_existe() -> None:
    """Recetas cerradas: la única puerta rechaza lo que no está pre-registrado."""
    deck = cargar_deck()
    with pytest.raises(KeyError, match="no está en la baraja"):
        deck.receta("deepar-mejorcito")


def test_una_baraja_con_cuatro_primarias_no_carga(tmp_path: Path) -> None:
    crudo = json.loads(DECK_PATH.read_text())
    crudo["recipes"]["deepar-extra"] = {
        "role": "primary",
        "model": "DeepAR",
        "space": "levels",
        "params": {},
        "notes": "colada después",
    }
    ruta = tmp_path / "deck.json"
    ruta.write_text(json.dumps(crudo))
    with pytest.raises(ValueError, match="recetas primarias"):
        cargar_deck(ruta)


def test_una_baraja_que_pida_gpu_no_carga(tmp_path: Path) -> None:
    crudo = json.loads(DECK_PATH.read_text())
    crudo["device"] = "cuda"
    ruta = tmp_path / "deck.json"
    ruta.write_text(json.dumps(crudo))
    with pytest.raises(ValueError, match="campaña es de CPU"):
        cargar_deck(ruta)


def test_lo_congelado_esta_congelado() -> None:
    f = cargar_deck().frozen
    assert "StudentT" in f["loss"]
    assert f["valid_loss"] == "MAE()"
    assert f["learning_rate"] == 1e-4
    assert f["scaler_type"] == "robust"
    assert f["gradient_clip_val"] == 1.0
    assert f["prediction_column"] == "DeepAR-median"
    assert f["accelerator"] == "cpu"
    assert f["early_stop_patience_steps"] == 5


def test_la_politica_esta_escrita_y_nombra_la_baraja() -> None:
    texto = POLICY.read_text()
    assert "cohort_deck.json" in texto
    for receta in cargar_deck().primarias():
        assert receta.name in texto
    assert "CPU" in texto


def test_la_baraja_sella_sus_entradas() -> None:
    inputs = cargar_deck().inputs
    assert len(inputs["commit"]) == 40
    for clave in ("series_cohorts.json", "cohort_scan.json", "locks/lockset.json"):
        assert len(inputs[clave]) == 64, clave


# --------------------------------------------------------------- universo y aislamiento
def test_el_universo_evaluable_filtra_por_cohorte() -> None:
    catalogo = json.loads(COHORTS.read_text())["series"]
    for tabla in ("FAD", "DFF"):
        esperado = {s["cohort"] for s in catalogo if s["table"] == tabla}
        todas = deep.cohorte_uids(tabla, "all")
        piezas = [deep.cohorte_uids(tabla, c) for c in sorted(esperado)]
        assert set().union(*piezas) == todas, tabla
        assert sum(len(p) for p in piezas) == len(todas), "las cohortes deben ser disjuntas"


def test_los_lanes_de_cohorte_no_comparten_series() -> None:
    """Contaminación entre lanes: estable y no_estable son conjuntos disjuntos."""
    for tabla in ("FAD", "DFF"):
        a = deep.cohorte_uids(tabla, "estable")
        b = deep.cohorte_uids(tabla, "no_estable")
        assert a and b
        assert not (a & b), f"{tabla}: {sorted(a & b)[:3]}"


def test_el_universo_no_se_ensancha_mas_alla_del_catalogo() -> None:
    catalogo = {f"{s['country']}/{s['block']}/{s['category']}" for s in json.loads(COHORTS.read_text())["series"]}
    for tabla in ("FAD", "DFF"):
        assert deep.cohorte_uids(tabla, "all") <= catalogo


def test_sin_universo_evaluable_no_se_filtra_nada() -> None:
    """El comportamiento histórico sigue disponible y explícito."""
    import inspect

    firma = inspect.signature(deep.load_panel)
    assert firma.parameters["universe"].default == "pilot"
    assert firma.parameters["cohort"].default == "all"


# ------------------------------------------------------------------------- receipts
def _receipts() -> list[Path]:
    return sorted(RECEIPTS.glob("receipt_*.json")) if RECEIPTS.exists() else []


CLAVES_RECEIPT = {
    "lane",
    "recipe",
    "recipe_role",
    "recipe_model",
    "recipe_space",
    "deck_version",
    "cohort",
    "table",
    "universe",
    "seed",
    "device",
    "n_series",
    "n_rows_written",
    "inputs",
    "env",
    "timing",
    "rss_max_bytes",
    "status",
    "models",
    "warnings",
    "exception",
    "outputs",
}


@pytest.mark.skipif(not _receipts(), reason="la campaña aún no ha corrido")
class TestReceiptsDeLaCampana:
    def test_ninguna_receipt_esta_incompleta(self) -> None:
        for ruta in _receipts():
            r = json.loads(ruta.read_text())
            faltan = CLAVES_RECEIPT - set(r)
            assert not faltan, f"{ruta.name} sin {sorted(faltan)}"

    def test_toda_receipt_declara_cpu_y_ninguna_gpu(self) -> None:
        for ruta in _receipts():
            r = json.loads(ruta.read_text())
            assert r["device"] == "cpu", ruta.name
            assert r.get("cuda_available") is not True or r["device"] == "cpu"

    def test_toda_receta_de_una_receipt_esta_en_la_baraja(self) -> None:
        deck = cargar_deck()
        for ruta in _receipts():
            r = json.loads(ruta.read_text())
            receta = deck.receta(r["recipe"])
            assert receta.role == r["recipe_role"]

    def test_las_salidas_declaradas_existen_y_cuadran(self) -> None:
        """Resultados parciales: lo que la receipt dice haber escrito tiene que estar."""
        import hashlib

        for ruta in _receipts():
            r = json.loads(ruta.read_text())
            for salida in r["outputs"]:
                f = ROOT / salida["path"]
                assert f.exists(), f"{ruta.name} declara {salida['path']} y no está"
                assert f.stat().st_size == salida["bytes"]
                assert hashlib.sha256(f.read_bytes()).hexdigest() == salida["sha256"]

    def test_un_lane_fallido_tambien_deja_receipt(self) -> None:
        """Un fallo es resultado: si alguno falló, su receipt lo dice sin inventar salidas."""
        for ruta in _receipts():
            r = json.loads(ruta.read_text())
            if r["status"] == "failed":
                assert r["exception"], ruta.name
                assert all(m["forecasts"] == 0 for m in r["models"].values())

    def test_las_entradas_selladas_son_las_mismas_en_todos_los_lanes(self) -> None:
        """Reproducibilidad: todos los lanes vieron el mismo material de partida."""
        vistos = {json.dumps(json.loads(p.read_text())["inputs"], sort_keys=True) for p in _receipts()}
        assert len(vistos) == 1, "hay lanes con entradas distintas"

    def test_los_lanes_escriben_en_su_propio_directorio(self) -> None:
        """Aislamiento: E3 no pisa la procedencia de la campaña sellada."""
        for ruta in _receipts():
            for salida in json.loads(ruta.read_text())["outputs"]:
                p = salida["path"]
                assert p.startswith("reports/campaign/e3/") or "/e3_gbm_" in p or "e3_" in Path(p).name, p

    def test_ningun_lane_subio_pesos(self) -> None:
        """Nada de pesos grandes: los lanes escriben predicciones y recibos."""
        for ruta in _receipts():
            for salida in json.loads(ruta.read_text())["outputs"]:
                assert Path(salida["path"]).suffix in {".csv", ".json"}, salida["path"]
                assert salida["bytes"] < 50_000_000, salida["path"]


# -------------------------------------------------------------- constructor de recetas
@pytest.mark.skipif(find_spec("neuralforecast") is None, reason="el extra profundo vive en ante_nf")
def test_el_constructor_respeta_lo_congelado() -> None:
    deck = cargar_deck()
    m = deep.build_recipe(deck.receta("deepar-robust"), 18, 1)
    assert m.scaler_type == "robust"
    assert float(m.learning_rate) == pytest.approx(1e-4)
    assert m.trainer_kwargs.get("accelerator") == "cpu"


def test_el_constructor_rechaza_un_modelo_que_no_construye() -> None:
    from vp_model.deck import Receta

    falso = Receta(name="x", role="control", model="ModeloInventado", params={}, space="levels", notes="")
    with pytest.raises((ValueError, ImportError, ModuleNotFoundError)):
        deep.build_recipe(falso, 18, 1)


# ------------------------------------------------------------ resultados de la campaña
RESULTADOS = ROOT / "reports" / "eval" / "e3_cohort_campaign.json"


@pytest.mark.skipif(not RESULTADOS.exists(), reason="la campaña aún no ha corrido")
class TestResultadoDeLaCampana:
    def test_el_resultado_declara_su_baraja_y_su_estado(self) -> None:
        r = json.loads(RESULTADOS.read_text())
        assert r["deck_version"] == cargar_deck().version
        assert r["device"] == "cpu"
        assert r["status"] in {"exploratorio"}

    def test_cada_lane_del_resultado_tiene_su_receipt(self) -> None:
        r = json.loads(RESULTADOS.read_text())
        nombres = {p.name for p in _receipts()}
        for lane in r["lanes"]:
            assert lane["receipt"] in nombres, lane["receipt"]

    def test_los_primarios_se_distinguen_de_los_controles(self) -> None:
        r = json.loads(RESULTADOS.read_text())
        roles = {lane["recipe_role"] for lane in r["lanes"]}
        assert roles <= {"primary", "control"}
        assert "primary" in roles

    def test_la_comparacion_es_contra_el_naive1_de_la_cohorte(self) -> None:
        r = json.loads(RESULTADOS.read_text())
        for lane in r["lanes"]:
            if lane.get("mase_by_series"):
                assert "reference_mean_mase" in lane
                assert lane["reference"] == "naive1"


def test_los_datos_del_panel_no_se_tocaron() -> None:
    """La campaña lee; no reescribe el panel ni el catálogo de cohortes."""
    inputs = cargar_deck().inputs
    import hashlib

    for clave in ("series_cohorts.json",):
        actual = hashlib.sha256((ROOT / "reports" / "eval" / clave).read_bytes()).hexdigest()
        assert actual == inputs[clave], f"{clave} cambió respecto a lo sellado en la baraja"


@pytest.mark.skipif(find_spec("darts") is None, reason="el corredor GBM importa vp_model.metrics")
def test_el_gbm_filtra_por_cohorte() -> None:
    import experiments.run_global_gbm as gbm

    catalogo = json.loads(COHORTS.read_text())["series"]
    for tabla in ("FAD", "DFF"):
        esperado = {(s["country"], s["category"]) for s in catalogo if s["table"] == tabla and s["cohort"] == "estable"}
        assert gbm.cohorte_keys(tabla, "estable") == esperado
        assert gbm.cohorte_keys(tabla, "all") is None


def test_el_corredor_profundo_se_importa_sin_el_extra_profundo() -> None:
    """El módulo no puede exigir torch al importarse: el job base no lo tiene."""
    fuente = (ROOT / "experiments" / "run_global_deep.py").read_text()
    cabecera = fuente.split("def ", 1)[0]
    for prohibido in ("import torch", "from neuralforecast", "import neuralforecast"):
        assert prohibido not in cabecera, prohibido


def test_los_resultados_de_la_campana_no_pisan_la_campana_sellada() -> None:
    """Aislamiento estructural: E3 tiene su propio subdirectorio."""
    fuente = (ROOT / "experiments" / "run_global_deep.py").read_text()
    assert '"campaign" / "e3"' in fuente
    sellados = sorted(CAMPANA_SELLADA.glob("global_*.csv"))
    for f in sellados:
        assert "e3_" not in f.name
