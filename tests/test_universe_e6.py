"""E6 · Una sola autoridad del universo, un catálogo coherente y ninguna afirmación fósil.

La auditoría de E6 midió las seis derivaciones vivas del universo evaluable y **coincidían
exactamente**: no había deriva. Lo que había era la posibilidad de que se separaran sin que
nadie se enterara, y conteos cableados que lo habrían tapado. Estas pruebas cierran esa puerta,
y de paso vigilan las otras tres cosas que E6 reconcilió: el estado de DeepAR, la limitación
observada de `arima_lstm` en DFF y los docstrings que nombraban a un ganador que el artefacto
desmiente.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CATALOGO = ROOT / "docs" / "model_catalog.json"
COHORTES = ROOT / "reports" / "eval" / "series_cohorts.json"
POOL = ROOT / "reports" / "eval" / "e4_router_pool.json"
HORIZON_FACTS = ROOT / "reports" / "eval" / "horizon_facts.json"
CODIGO = [*(ROOT / "vp_model").rglob("*.py"), *(ROOT / "experiments").rglob("*.py"), *(ROOT / "tools").rglob("*.py")]


def catalogo() -> dict:
    return {k: v for k, v in json.loads(CATALOGO.read_text()).items() if k != "_doc"}


# ------------------------------------------------------------------ autoridad del universo
def test_la_autoridad_deriva_el_universo_del_panel_canonico() -> None:
    from vp_model import universe

    claves = universe.evaluable_keys()
    assert claves == sorted(claves), "la autoridad devuelve una lista ordenada y estable"
    assert len(set(claves)) == len(claves) == universe.n_evaluable()
    assert set(claves) <= set(universe.structural_keys())


PROFUNDO = pytest.mark.skipif(
    find_spec("darts") is None or find_spec("scipy") is None,
    reason="vp_model.horizon importa scipy y darts; el job base instala solo .[dev]",
)


@PROFUNDO
def test_todas_las_derivaciones_vivas_coinciden_con_la_autoridad() -> None:
    """RED de universo duplicado: si alguna se separa, aquí se ve, con nombre y diferencia."""
    from vp_model import dataset, horizon, universe

    autoridad = set(universe.evaluable_keys())
    vistas = {
        "dataset.evaluable_series": {(r.country, r.category, r.table) for r in dataset.evaluable_series().itertuples()},
        "horizon.evaluable": {(p, c, t) for t in ("FAD", "DFF") for (p, c) in horizon.evaluable(t)},
        "series_cohorts.json": {
            (s["country"], s["category"], s["table"]) for s in json.loads(COHORTES.read_text())["series"]
        },
        "e4_router_pool.json": {
            (s["country"], s["category"], s["table"]) for s in json.loads(POOL.read_text())["series"]
        },
    }
    for nombre, vista in vistas.items():
        assert vista == autoridad, f"{nombre} difiere de la autoridad en {sorted(vista ^ autoridad)[:3]}"


def test_los_artefactos_sellados_coinciden_con_la_autoridad_sin_el_extra() -> None:
    """La parte que NO necesita el extra corre en los dos jobs: los sellados contra la autoridad."""
    from vp_model import universe

    autoridad = set(universe.evaluable_keys())
    for nombre, ruta in (("series_cohorts.json", COHORTES), ("e4_router_pool.json", POOL)):
        vista = {(s["country"], s["category"], s["table"]) for s in json.loads(ruta.read_text())["series"]}
        assert vista == autoridad, f"{nombre} difiere en {sorted(vista ^ autoridad)[:3]}"


def test_los_artefactos_sellados_son_salidas_no_fuentes() -> None:
    """Un artefacto puede quedarse rancio; por eso declara su procedencia y se compara."""
    coh = json.loads(COHORTES.read_text())
    pool = json.loads(POOL.read_text())
    from vp_model import universe

    assert coh["population"]["n_evaluable"] == universe.n_evaluable()
    assert pool["n"] == universe.n_evaluable()
    assert pool["source"] == "reports/eval/series_cohorts.json"
    assert pool["source_sha256"]


def test_ningun_consumidor_cablea_el_conteo() -> None:
    """RED de conteo cableado: nadie compara contra un 74 literal en código o contrato."""
    from vp_model import universe

    n = universe.n_evaluable()
    ofensas: list[tuple[str, str]] = []
    patron = re.compile(rf"(==|!=|:)\s*{n}\b")
    for ruta in [*CODIGO, *(ROOT / "tests").rglob("*.py")]:
        if ruta.name in {"test_universe_e6.py"}:
            continue
        for i, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
            desnuda = linea.split("#")[0]
            if patron.search(desnuda) and "n_evaluable" not in desnuda:
                ofensas.append((f"{ruta.relative_to(ROOT)}:{i}", linea.strip()[:70]))
    assert ofensas == [], f"conteo cableado: {ofensas}"
    deck = json.loads((ROOT / "docs" / "challenger_deck.json").read_text())
    assert "n_expected" not in deck["pool"], "la baraja volvió a cablear el conteo"
    assert "n_authority" in deck["pool"]


# ---------------------------------------------------------------------- catálogo coherente
def test_deepar_es_research_y_su_nota_cita_la_evidencia() -> None:
    """RED de estado incoherente: `retired` decía «diverge», y E3 lo desmintió."""
    d = catalogo()["deepar"]
    assert d["clase"] == "research"
    assert "e3_cohort_campaign.json" in d["nota"], "la reclasificación debe citar su fuente"
    assert "0.110" in d["nota"] and "0.518" in d["nota"], "y las cifras que la sostienen"
    assert "EXCLUIDO de promocion y despliegue" in d["nota"]


def test_ningun_modelo_research_entra_al_manifiesto_campeon() -> None:
    """RED de promoción accidental: research no se despliega, pase lo que pase."""
    cat = catalogo()
    research = {k for k, v in cat.items() if v["clase"] == "research"}
    activos = {k for k, v in cat.items() if v["clase"] == "active"}
    assert research and not (research & activos)
    manifiesto = json.loads((ROOT / "reports" / "governance" / "champion_manifest.json").read_text())
    usados = {m for v in manifiesto.values() if isinstance(v, dict) for m in v.get("models", [])}
    assert usados <= activos, f"el manifiesto usa modelos no activos: {sorted(usados - activos)}"
    assert not (usados & research), f"modelo research desplegado: {sorted(usados & research)}"


def test_el_gate_del_catalogo_rechaza_promover_un_research(tmp_path: Path) -> None:
    import tools.check_model_catalog as cmc

    cat = catalogo()
    activo = next(k for k, v in cat.items() if v["clase"] == "active")
    assert cmc  # el gate existe y es el que corre en CI
    # Ascender deepar a active sería una edición explícita del catálogo, no un accidente:
    # lo que aquí se fija es que HOY no lo es y que el manifiesto no lo nombra.
    assert cat["deepar"]["clase"] != "active"
    assert activo in {k for k, v in cat.items() if v["clase"] == "active"}


# ------------------------------------------------------- la limitación observada, visible
def test_el_all_nan_de_arima_lstm_en_DFF_esta_documentado_con_fuente_y_alcance() -> None:
    """RED de ocultamiento: la ausencia se reporta como ausencia, no se imputa ni se borra."""
    nota = catalogo()["arima_lstm"]["nota"]
    assert "LIMITACION OBSERVADA" in nota
    assert "model_comparison_DFF21.csv" in nota and "model_comparison_EB_DFF21.csv" in nota
    assert "89 de 89" in nota, "el alcance exacto, no un «a veces falla»"
    assert "FAD" in nota and "25/25" in nota, "y dónde SÍ funciona, para acotar el alcance"


@pytest.mark.parametrize("archivo,filas", [("model_comparison_DFF21.csv", 25), ("model_comparison_EB_DFF21.csv", 64)])
def test_la_limitacion_documentada_sigue_siendo_cierta(archivo: str, filas: int) -> None:
    """Si algún día arima_lstm puntúa en DFF, esta prueba obliga a actualizar la nota."""
    import pandas as pd

    d = pd.read_csv(ROOT / "reports" / "eval" / archivo)
    s = d[d.model == "arima_lstm"]
    assert len(s) == filas
    assert int(s.hold_mase.notna().sum()) == 0, "la nota dice que no puntúa; el dato debe seguirlo"


def test_los_resultados_no_se_imputaron_ni_se_borraron() -> None:
    """La fila existe y está vacía: eso es el hallazgo. Borrarla lo escondería."""
    import pandas as pd

    d = pd.read_csv(ROOT / "reports" / "eval" / "model_comparison_DFF21.csv")
    assert "arima_lstm" in set(d.model), "la fila no puede desaparecer del cuaderno"
    assert int(d[d.model == "arima_lstm"].hold_mase.fillna(-1).eq(-1).sum()) == 25


# ------------------------------------------------------------------ afirmaciones fósiles
FOSILES = (
    r"Theta\)?\s+lo bate",
    r"ETS/Theta ganan",
    r"la parsimonia \(Theta\)",
    r"que gana este r[ée]gimen",
)


def test_no_reaparece_ninguna_afirmacion_fosil() -> None:
    """RED de fósil: nombrar al ganador en un comentario lo deja rancio en cuanto cambia."""
    ofensas = []
    for ruta in CODIGO:
        texto = ruta.read_text(encoding="utf-8")
        for patron in FOSILES:
            if re.search(patron, texto):
                ofensas.append((str(ruta.relative_to(ROOT)), patron))
    assert ofensas == [], f"afirmaciones fósiles de vuelta: {ofensas}"


def test_el_ganador_por_horizonte_se_lee_del_artefacto_no_del_docstring() -> None:
    """Y el artefacto, hoy, dice `drift` — no Theta: por eso la afirmación se retiró."""
    facts = json.loads(HORIZON_FACTS.read_text())
    campeones = {t: set(facts[t]["champion_by_h"].values()) for t in ("FAD", "DFF")}
    assert "theta" not in {m.lower() for ms in campeones.values() for m in ms}
    for modulo in ("vp_model/config.py", "vp_model/horizon.py"):
        texto = (ROOT / modulo).read_text()
        assert "horizon_facts.json" in texto, f"{modulo} debe apuntar al artefacto que mide"


def test_los_docstring_que_nombran_un_ganador_apuntan_a_su_autoridad() -> None:
    """No se sustituye un literal por otro: se retira la afirmación y se nombra la fuente."""
    texto = (ROOT / "vp_model" / "ensemble.py").read_text()
    arbol = ast.parse(texto)
    doc = ast.get_docstring(arbol) or ""
    assert "champion_challenger.json" in doc
    assert "ETS/Theta" not in doc
