"""M74-E-R22 · un lane de E3 entrena sobre el universo que la baraja pre-registró, no sobre el defecto del runner.

La novena auditoría ciega (sobre `2fab12c`) encontró que `run_e3_campaign.Lane.command` no pasa `--universe` y que
`run_global_deep.main()`, al aplicar la receta, fijaba espacio/modelos/auto/config pero NO el universo: el runner se
quedaba en su defecto `pilot`, con el que `load_panel` no restringe por cohorte. Los tres lanes de cohorte de una misma
(receta, tabla) eran el mismo entrenamiento sobre las 56 series piloto con tres etiquetas, mientras `docs/cohort_deck.json`
y los recibos rastreados de M61 declaran `evaluable` (11/30/41 series en FAD). La campaña habría terminado en verde
sellando E3/E5 bajo un protocolo distinto del pre-registrado. Ahora `aplicar_receta` toma el universo de la baraja.
"""

from __future__ import annotations

import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

pytestmark = pytest.mark.skipif(
    find_spec("lightgbm") is None or find_spec("darts") is None, reason="runners del extra model"
)


def test_RED_la_receta_fija_el_universo_de_la_baraja() -> None:
    """Contra `2fab12c`: `aplicar_receta` no existe y el universo se quedaba en `pilot`."""
    import run_e3_campaign as e3
    import run_global_deep as deep

    from vp_model.deck import cargar_deck

    deck = cargar_deck()
    assert deck.universe == "evaluable"
    lane = next(lane for lane in e3.plan() if lane.runner == "run_global_deep.py")
    args = deep.build_parser().parse_args(lane.command[2:])
    assert args.universe == "pilot"  # el defecto del runner, que es lo que corría en las tres campañas
    deep.aplicar_receta(args, deck.receta(lane.recipe), deck)
    assert args.universe == "evaluable" and args.diff == (deck.receta(lane.recipe).space == "diff")


def test_todos_los_lanes_deep_quedan_en_el_universo_evaluable() -> None:
    import run_e3_campaign as e3
    import run_global_deep as deep

    from vp_model.deck import cargar_deck

    deck = cargar_deck()
    lanes = [lane for lane in e3.plan() if lane.runner == "run_global_deep.py"]
    assert len(lanes) == 36
    for lane in lanes:
        args = deep.aplicar_receta(deep.build_parser().parse_args(lane.command[2:]), deck.receta(lane.recipe), deck)
        assert args.universe == deck.universe and args.cohort == lane.cohort


@pytest.mark.parametrize("cohorte", ["estable", "no_estable"])
def test_RED_con_el_universo_de_la_baraja_el_panel_del_lane_es_su_cohorte(cohorte: str) -> None:
    """Por conducta sobre el panel real: con `pilot` las tres cohortes ven las mismas 56 series de FAD; con el
    universo de la baraja cada lane ve exactamente `cohorte_uids(table, cohort)`."""
    import run_global_deep as deep

    from vp_model.deck import cargar_deck

    deck = cargar_deck()
    pilot = deep.load_panel("FAD", "both", cohort=cohorte, universe="pilot")["unique_id"].nunique()
    evaluable = deep.load_panel("FAD", "both", cohort=cohorte, universe=deck.universe)["unique_id"].nunique()
    esperado = len(deep.cohorte_uids("FAD", cohorte))
    assert esperado > 0 and evaluable == esperado < pilot, (pilot, evaluable, esperado)
