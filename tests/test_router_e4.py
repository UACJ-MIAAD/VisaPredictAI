"""E4 · El router no puede elegir su receta después de ver con qué se le juzga.

Las ocho formas de romperlo tienen aquí su prueba: fuga entre selección y evaluación, pool
alterado, receta no registrada, selección post-resultado, piso global en vez del de la cohorte,
familia de Holm equivocada, efecto por debajo del mínimo material y pérdida material en un
horizonte. Lo que no depende de las bibliotecas de modelado se comprueba en el job base.
"""

from __future__ import annotations

import json
from importlib.util import find_spec
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DECK = ROOT / "docs" / "challenger_deck.json"
POOL = ROOT / "reports" / "eval" / "e4_router_pool.json"
COHORTS = ROOT / "reports" / "eval" / "series_cohorts.json"
POLICY = ROOT / "docs" / "COHORT_POLICY.md"
RESULT = ROOT / "reports" / "eval" / "e4_router.json"
PROFUNDO = pytest.mark.skipif(find_spec("darts") is None, reason="vp_model.horizon importa darts")


def test_ninguna_prueba_del_job_base_importa_el_extra_de_modelado() -> None:
    """Cuarta reincidencia (M14, M48, M59, M62): el job base instala solo ``.[dev]``.

    Una prueba sin la marca ``PROFUNDO`` no puede importar scipy, darts, statsmodels, lightgbm,
    torch ni un módulo de ``vp_model`` que los arrastre. Esto lo comprueba la estructura del
    archivo, no mi memoria.
    """
    import ast

    pesados = {"scipy", "darts", "statsmodels", "lightgbm", "torch", "optuna"}
    permitidos_vp = {"vp_model.deck"}
    arbol = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    culpables: list[tuple[str, str]] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.FunctionDef):
            continue
        gateada = any(
            (isinstance(d, ast.Name) and d.id == "PROFUNDO")
            or (isinstance(d, ast.Attribute) and getattr(d.value, "id", "") == "PROFUNDO")
            for d in nodo.decorator_list
        )
        if gateada:
            continue
        for sub in ast.walk(nodo):
            modulo = getattr(sub, "module", None)
            if isinstance(sub, ast.ImportFrom) and modulo:
                raiz = modulo.split(".")[0]
                if raiz in pesados or (raiz == "vp_model" and modulo not in permitidos_vp):
                    culpables.append((nodo.name, modulo))
            if isinstance(sub, ast.Import):
                for alias in sub.names:
                    if alias.name.split(".")[0] in pesados:
                        culpables.append((nodo.name, alias.name))
    assert culpables == [], f"pruebas del job base con imports del extra: {culpables}"


# --------------------------------------------------------------- pre-registro (job base)
def test_la_baraja_declara_todo_lo_que_el_gate_necesita() -> None:
    d = json.loads(DECK.read_text())
    assert d["gate"]["material_margin"] == 0.005
    assert d["gate"]["holm_alpha"] == 0.05
    assert "BILATERAL" in d["gate"]["test"].upper()
    assert d["router"]["horizons"] == [3, 6, 12]
    assert d["router"]["allowed_material_loss"] == 0.0
    assert d["router"]["reference"].startswith("naive1")


def test_lo_unilateral_es_sensibilidad_y_nunca_gate() -> None:
    d = json.loads(DECK.read_text())
    assert "NUNCA" in d["gate"]["one_sided"].upper()
    fuente = (ROOT / "experiments" / "run_e4_router.py").read_text()
    assert 'alternative="greater"' in fuente
    # la regla de victoria NO puede leer la sensibilidad
    regla = fuente[fuente.index("def veredicto_de_celda") : fuente.index("class PoolAlterado")]
    assert "sensitivity" not in regla and "one_sided" not in regla and "greater" not in regla


def test_los_candidatos_del_router_excluyen_lo_prohibido() -> None:
    """Ni AutoDeepAR/AutoBiTCN ni el ganador de E3 ni control-bitcn pueden entrar."""
    d = json.loads(DECK.read_text())
    cand = set(d["router"]["candidates"])
    assert cand == {"naive1", "drift", "naive", "theta", "ets"}
    for prohibido in ("AutoDeepAR", "AutoBiTCN", "BiTCN", "deepar-robust", "control-bitcn", "DeepAR"):
        assert prohibido not in cand
    prohibiciones = " ".join(d["router"]["forbidden"]).lower()
    for texto in ("autodeepar", "control-bitcn", "e3", "hold-out", "promover"):
        assert texto in prohibiciones


def test_la_politica_declara_la_correccion_causal_de_E3() -> None:
    texto = POLICY.read_text()
    assert "no permite esa atribución" in texto
    assert "6/6" in texto and "3/6" in texto
    assert "diseño factorial" in texto


def test_la_seleccion_es_disjunta_de_la_evaluacion_por_escrito() -> None:
    d = json.loads(DECK.read_text())
    regla = d["router"]["selection_rule"]
    assert "ANTERIORES al hold-out" in regla
    assert "SOLO objetivos del hold-out" in regla
    assert "disjuntas" in regla


# ------------------------------------------------------------------------ pool exacto
def test_el_pool_persistido_tiene_las_74_series_de_E1() -> None:
    pool = json.loads(POOL.read_text())
    cohortes = json.loads(COHORTS.read_text())
    assert pool["n"] == cohortes["population"]["n_evaluable"] == 74
    a = {(s["country"], s["category"], s["table"]) for s in pool["series"]}
    b = {(s["country"], s["category"], s["table"]) for s in cohortes["series"]}
    assert a == b
    assert pool["by_cohort"] == {"estable": 39, "no_estable": 35}


@PROFUNDO
def test_un_pool_alterado_detiene_la_evaluacion(tmp_path: Path) -> None:
    """RED de pool alterado: quitar UNA serie ya no es el universo declarado."""
    import experiments.run_e4_router as e4

    doc = json.loads(POOL.read_text())
    doc["series"] = doc["series"][:-1]
    ruta = tmp_path / "pool.json"
    ruta.write_text(json.dumps(doc))
    with pytest.raises(e4.PoolAlterado, match="declara"):
        e4.cargar_pool(ruta)

    doc2 = json.loads(POOL.read_text())
    doc2["series"] = doc2["series"] + [dict(doc2["series"][0])]
    ruta2 = tmp_path / "pool2.json"
    ruta2.write_text(json.dumps(doc2))
    with pytest.raises(e4.PoolAlterado, match="repetidas"):
        e4.cargar_pool(ruta2)


@PROFUNDO
def test_un_pool_con_otra_serie_no_pasa_la_igualdad_de_conjuntos(tmp_path: Path) -> None:
    import experiments.run_e4_router as e4

    doc = json.loads(POOL.read_text())
    doc["series"][0] = {**doc["series"][0], "country": "narnia"}
    ruta = tmp_path / "pool.json"
    ruta.write_text(json.dumps(doc))
    with pytest.raises(e4.PoolAlterado, match="no coincide con el catálogo"):
        e4.cargar_pool(ruta)


# ---------------------------------------------------------------- la baraja como puerta
@PROFUNDO
def test_la_baraja_se_lee_solo_por_load_deck() -> None:
    from vp_model import champion

    d = champion.load_deck()
    assert d["router"]["horizons"] == [3, 6, 12]
    assert champion.deck_challengers("FAD")


@PROFUNDO
@pytest.mark.parametrize(
    "mutacion,patron",
    [
        ({"gate": {"material_margin": 0.001}}, "margen material"),
        ({"gate": {"holm_alpha": 0.10}}, "alfa de Holm"),
        ({"gate": {"test": "wilcoxon unilateral"}}, "no es bilateral"),
        ({"router": {"candidates": ["naive1", "BiTCN"]}}, "no son los congelados"),
        ({"router": {"horizons": [3, 6, 7]}}, "no están en config.HORIZONS"),
        ({"router": {"allowed_material_loss": 1.0}}, "no es cero"),
    ],
)
def test_una_baraja_manipulada_no_carga(tmp_path: Path, mutacion: dict, patron: str) -> None:
    """RED de receta no registrada y de umbral movido: la puerta falla cerrada."""
    from vp_model import champion

    d = json.loads(DECK.read_text())
    for seccion, cambios in mutacion.items():
        d[seccion] = {**d[seccion], **cambios}
    ruta = tmp_path / "deck.json"
    ruta.write_text(json.dumps(d))
    with pytest.raises(ValueError, match=patron):
        champion.load_deck(ruta)


# ------------------------------------------------------ selección leakage-free (el corazón)
@PROFUNDO
def test_la_ventana_parte_los_objetivos_en_el_tiempo() -> None:
    from vp_model import horizon

    sel = horizon.mase_by_horizon("naive1", "mexico", "F1", "FAD", 6, window="selection")
    hold = horizon.mase_by_horizon("naive1", "mexico", "F1", "FAD", 6, window="holdout")
    todo = horizon.mase_by_horizon("naive1", "mexico", "F1", "FAD", 6, window="all")
    assert sel and hold and todo
    assert sel != hold, "si las dos ventanas coinciden, no hay separación temporal"
    with pytest.raises(ValueError, match="window debe ser"):
        horizon.mase_by_horizon("naive1", "mexico", "F1", "FAD", 6, window="futuro")


@PROFUNDO
def test_el_router_elige_por_seleccion_y_NO_por_holdout() -> None:
    """RED de selección post-resultado: si el pick usara el hold-out, sería otro."""
    from vp_model import horizon

    cohortes = {("a", "X"): "estable", ("b", "X"): "estable"}
    # 'malo' es el mejor en SELECCIÓN y el peor en HOLD-OUT: elegir por hold-out lo descartaría.
    grid = {
        ("malo", "a", "X"): {"selection": {3: 0.10}, "holdout": {3: 9.00}},
        ("malo", "b", "X"): {"selection": {3: 0.10}, "holdout": {3: 9.00}},
        ("bueno", "a", "X"): {"selection": {3: 0.90}, "holdout": {3: 0.01}},
        ("bueno", "b", "X"): {"selection": {3: 0.90}, "holdout": {3: 0.01}},
    }
    receta = horizon.fit_router("FAD", 3, cohortes, grid, ("malo", "bueno"))
    assert receta.by_cohort["estable"] == "malo", "el router miró el hold-out"
    mase = horizon.router_series_mase(receta, cohortes, grid)
    assert list(mase) == [9.0, 9.0], "la evaluación debe usar el hold-out del modelo ELEGIDO"


@PROFUNDO
def test_el_router_es_inmutable() -> None:
    from vp_model import horizon

    r = horizon.RouterRecipe(table="FAD", horizon=3, by_cohort={"estable": "drift"})
    with pytest.raises((AttributeError, TypeError)):
        r.table = "DFF"  # type: ignore[misc]
    assert "estable→drift" in r.name


@PROFUNDO
def test_cada_serie_se_puntua_con_el_modelo_de_SU_cohorte() -> None:
    """RED de piso global: mezclar cohortes cambiaría el resultado."""
    from vp_model import horizon

    cohortes = {("a", "X"): "estable", ("b", "X"): "no_estable"}
    grid = {
        ("m1", "a", "X"): {"selection": {3: 0.1}, "holdout": {3: 0.5}},
        ("m1", "b", "X"): {"selection": {3: 0.9}, "holdout": {3: 0.6}},
        ("m2", "a", "X"): {"selection": {3: 0.9}, "holdout": {3: 0.7}},
        ("m2", "b", "X"): {"selection": {3: 0.1}, "holdout": {3: 0.8}},
    }
    receta = horizon.fit_router("FAD", 3, cohortes, grid, ("m1", "m2"))
    assert receta.by_cohort == {"estable": "m1", "no_estable": "m2"}
    mase = horizon.router_series_mase(receta, cohortes, grid)
    assert mase[("a", "X")] == 0.5 and mase[("b", "X")] == 0.8


# ------------------------------------------- la regla de victoria, con entradas sintéticas
def _fila(h: int, efecto: float, rechaza: bool) -> dict:
    return {"h": h, "effect": efecto, "holm_reject": rechaza}


@PROFUNDO
def test_ganar_exige_ganancia_material_y_Holm_en_LOS_TRES() -> None:
    import experiments.run_e4_router as e4

    tres = [_fila(3, 0.02, True), _fila(6, 0.02, True), _fila(12, 0.02, True)]
    assert e4.veredicto_de_celda(tres, 0.005, 0.0)[0] == "gana_al_naive1"


@PROFUNDO
@pytest.mark.parametrize(
    "filas,esperado,motivo",
    [
        (
            [_fila(3, 0.004, True), _fila(6, 0.02, True), _fila(12, 0.02, True)],
            "sin_evidencia",
            "RED de efecto insuficiente: 0.004 < 0.005 no es material",
        ),
        (
            [_fila(3, 0.02, False), _fila(6, 0.02, True), _fila(12, 0.02, True)],
            "sin_evidencia",
            "RED de Holm: si un horizonte no rechaza, no se gana",
        ),
        (
            [_fila(3, -0.006, True), _fila(6, 0.02, True), _fila(12, 0.02, True)],
            "pierde_material",
            "RED de pérdida: una sola pérdida material lo impide",
        ),
        (
            [_fila(3, 0.02, True), _fila(6, 0.02, True), _fila(12, -0.02, True)],
            "pierde_material",
            "la pérdida cuenta en CUALQUIER horizonte, también el largo",
        ),
        ([], "sin_evidencia", "sin horizontes no se gana por omisión"),
    ],
)
def test_la_regla_de_victoria_rechaza_cada_atajo(filas, esperado, motivo) -> None:
    import experiments.run_e4_router as e4

    assert e4.veredicto_de_celda(filas, 0.005, 0.0)[0] == esperado, motivo


@PROFUNDO
def test_el_umbral_material_es_exactamente_0_005() -> None:
    import experiments.run_e4_router as e4

    justo = [_fila(h, 0.005, True) for h in (3, 6, 12)]
    assert e4.veredicto_de_celda(justo, 0.005, 0.0)[0] == "gana_al_naive1"
    apenas = [_fila(h, 0.0049999, True) for h in (3, 6, 12)]
    assert e4.veredicto_de_celda(apenas, 0.005, 0.0)[0] == "sin_evidencia"


@PROFUNDO
def test_la_ventana_de_seleccion_excluye_de_verdad_el_holdout() -> None:
    """RED de fuga: si la ventana no filtrara, selección y «todo» coincidirían."""
    from vp_model import horizon

    sel = horizon.mase_by_horizon("naive1", "mexico", "F1", "FAD", 6, window="selection")
    todo = horizon.mase_by_horizon("naive1", "mexico", "F1", "FAD", 6, window="all")
    hold = horizon.mase_by_horizon("naive1", "mexico", "F1", "FAD", 6, window="holdout")
    assert sel and todo and hold
    assert sel != todo, "la selección no puede coincidir con la serie completa"
    assert hold != todo, "el hold-out no puede coincidir con la serie completa"


# ------------------------------------------------------------------ resultado publicado
@pytest.mark.skipif(not RESULT.exists(), reason="E4 aún no ha corrido")
class TestResultadoE4:
    def _d(self) -> dict:
        return json.loads(RESULT.read_text())

    def test_el_informe_declara_el_gate_congelado(self) -> None:
        g = self._d()["gate"]
        assert g["material_margin"] == 0.005 and g["holm_alpha"] == 0.05
        assert "BILATERAL" in g["test"].upper()
        assert g["allowed_material_loss"] == 0.0
        assert self._d()["horizons"] == [3, 6, 12]

    def test_el_pool_del_informe_es_el_persistido(self) -> None:
        import hashlib

        d = self._d()
        assert d["pool"]["n"] == 74
        assert d["pool"]["sha256"] == hashlib.sha256(POOL.read_bytes()).hexdigest()

    def test_la_familia_de_holm_son_los_tres_horizontes(self) -> None:
        """RED de Holm incorrecto: cada celda ajusta sobre exactamente {3,6,12}.

        Holm se recalcula AQUÍ, en Python puro, en vez de llamar a la misma función que produjo
        el artefacto: una prueba que invoca la implementación que quiere verificar solo comprueba
        que esa función es determinista. Y de paso corre en el job base, que no trae scipy.
        """
        for c in self._d()["cells"]:
            assert [f["h"] for f in c["horizons"]] == [3, 6, 12]
            orden = sorted(c["horizons"], key=lambda f: f["wilcoxon_p_two_sided"])
            m, previo = len(orden), 0.0
            for i, f in enumerate(orden):
                previo = max(previo, min(1.0, (m - i) * f["wilcoxon_p_two_sided"]))
                assert f["holm_p"] == pytest.approx(previo, abs=1e-9), (c["cohort"], f["h"])
                assert f["holm_reject"] is bool(previo < 0.05)

    def test_un_efecto_insuficiente_no_es_ganancia_material(self) -> None:
        """RED de efecto insuficiente: por debajo de 0.005 no cuenta, aunque sea positivo."""
        for c in self._d()["cells"]:
            for f in c["horizons"]:
                assert f["material_gain"] is bool(f["effect"] >= 0.005)
                assert f["material_loss"] is bool(f["effect"] <= -0.005)

    def test_una_perdida_material_en_un_horizonte_impide_ganar(self) -> None:
        """RED de pérdida en un horizonte: basta una para que la celda no gane."""
        for c in self._d()["cells"]:
            if c["material_loss_horizons"]:
                assert c["verdict"] != "gana_al_naive1", c["cohort"]

    def test_ganar_exige_los_TRES_horizontes(self) -> None:
        for c in self._d()["cells"]:
            gana = all(f["material_gain"] and f["holm_reject"] for f in c["horizons"])
            assert (c["verdict"] == "gana_al_naive1") == (gana and not c["material_loss_horizons"])

    def test_el_router_no_se_promueve_pase_lo_que_pase(self) -> None:
        for c in self._d()["cells"]:
            assert c["promotion"].startswith("NO")
        assert self._d()["status"] == "exploratorio"

    def test_el_router_solo_usa_candidatos_registrados(self) -> None:
        d = self._d()
        permitidos = set(d["candidates"])
        for c in d["cells"]:
            for f in c["horizons"]:
                assert f["model"] in permitidos, f["model"]
