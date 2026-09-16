"""M74-E-R18 · el sello de entradas enumera los runners que el deck de E3 elige en tiempo de ejecución.

La quinta auditoría ciega (sobre `228c2c1`, dictamen APTO) dejó un hallazgo medio de procedencia: los lanes de
`run_e3_campaign.py` ejecutan `experiments/{runner}` por subprocess, con el runner leído de `docs/cohort_deck.json`;
`runbook_entrypoints` derivaba por regex sobre los `.sh` y por cierre de imports, así que `run_global_gbm.py` —seis
de los 42 lanes— quedaba fuera de `inputs.code` y el recibo no podía probar qué código los produjo. Ahora el deck es
la autoridad única del runner por defecto (`vp_model.deck.DEFAULT_RUNNER`), `Deck.runners()` los enumera, el sello
los incorpora y un runner declarado que no exista en el árbol cierra el preflight.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

from tools import campaign_preflight as pf  # noqa: E402
from vp_model import deck as deck_mod  # noqa: E402


def test_RED_todo_runner_de_los_lanes_de_e3_entra_a_los_entrypoints_y_al_cierre_del_sello() -> None:
    """Contra `228c2c1`: `experiments/run_global_gbm.py` no estaba ni en `scripts` ni en `inputs.code`."""
    import run_e3_campaign as e3

    lanes = e3.plan()
    assert len(lanes) == 42
    eps = pf.runbook_entrypoints()
    cierre = set(pf.code_closure(eps))
    for runner in sorted({f"experiments/{lane.runner}" for lane in lanes}):
        assert runner in eps["scripts"], f"{runner} corre por subprocess y no está en los entrypoints"
        assert runner in cierre, f"{runner} no entra al cierre sellado"


def test_el_deck_es_la_unica_autoridad_del_runner_y_cubre_el_defecto() -> None:
    import run_e3_campaign as e3

    d = deck_mod.cargar_deck()
    assert set(d.runners()) == {lane.runner for lane in e3.plan()}
    sin_runner = [r for r in d.recipes.values() if "runner" not in r.params]
    assert sin_runner and deck_mod.DEFAULT_RUNNER in d.runners()
    assert "run_global_gbm.py" in d.runners()


def test_RED_un_runner_declarado_que_no_existe_cierra_el_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: `code_closure` ignora en silencio un módulo sin archivo, así que se rechaza ANTES."""
    d = deck_mod.cargar_deck()
    nombre, receta = next(iter(d.recipes.items()))
    trucado = replace(
        d, recipes={**d.recipes, nombre: replace(receta, params={**receta.params, "runner": "no_existe.py"})}
    )
    monkeypatch.setattr(pf, "cargar_deck", lambda: trucado)
    with pytest.raises(pf.PreflightError, match="no_existe.py"):
        pf.runbook_entrypoints()


def test_un_runbook_sembrado_sin_e3_no_consulta_el_deck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: la ampliación sólo actúa cuando el runbook invoca al orquestador de E3."""
    monkeypatch.setattr(
        pf, "cargar_deck", lambda: (_ for _ in ()).throw(AssertionError("no debía leerse")), raising=False
    )
    guion = tmp_path / "rb.sh"
    guion.write_text("#!/bin/bash\n$ANTE experiments/run_champion_challenger.py\n", encoding="utf-8")
    assert pf.runbook_entrypoints(guion)["scripts"] == ["experiments/run_champion_challenger.py"]
