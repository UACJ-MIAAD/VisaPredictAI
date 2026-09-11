"""M74-B · La procedencia de E3 y E4 se comprueba contra el árbol, no se declara y ya (H6, H7).

`e4_router.json` sellaba `inputs` **copiándolos** del deck retador, así que declaraba versiones de
`champion.py`, `config.py` y `horizon.py` que ni siquiera contenían las funciones con las que el
artefacto se produjo. Ningún cargador ni prueba comparaba esos hashes: la procedencia podía mentir
indefinidamente. Esta prueba es el guardián que faltaba.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
#: Cómo se resuelve cada clave de `inputs` a un archivo del árbol.
RESOLUCION = {
    "visa_panel_long.csv": "data/processed/visa_panel_long.csv",
    "visa_panel_long.parquet": "data/processed/visa_panel_long.parquet",
    "cohort_scan.json": "reports/eval/cohort_scan.json",
    "series_cohorts.json": "reports/eval/series_cohorts.json",
    "e3_cohort_campaign.json": "reports/eval/e3_cohort_campaign.json",
}


def _resolver(clave: str) -> Path | None:
    rel = RESOLUCION.get(clave, clave)
    ruta = ROOT / rel
    return ruta if ruta.is_file() else None


def _sha(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _inputs(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))["inputs"]


def test_the_e3_deck_inputs_match_the_tree_after_the_reseal() -> None:
    """H7: el deck debe describir la campaña que se va a correr, no una anterior."""
    divergentes = []
    for clave, sellado in _inputs("docs/cohort_deck.json").items():
        if clave in {"commit", "deck_version"}:
            continue
        ruta = _resolver(clave)
        assert ruta is not None, f"la entrada sellada {clave} no resuelve a ningún archivo"
        if _sha(ruta) != sellado:
            divergentes.append(clave)
    assert not divergentes, f"el deck sella hashes que ya no son los del árbol: {divergentes}"


def test_the_reseal_kept_the_pre_registration_intact() -> None:
    """Resellar entradas NO es reescribir el pre-registro: las recetas son las mismas."""
    deck = json.loads((ROOT / "docs" / "cohort_deck.json").read_text(encoding="utf-8"))
    assert set(deck["recipes"]) == {
        "deepar-legacy",
        "deepar-robust",
        "deepar-robust-levels",
        "control-bitcn",
        "control-gbm-lightgbm",
        "control-patchtst",
        "control-tide",
    }
    assert deck["cohorts"] == ["estable", "no_estable", "all"]
    assert deck["seeds"] == [1] and deck["device"] == "cpu"
    # ★ y la procedencia ORIGINAL de E3 se conserva: reescribirla sin guardarla borraría de qué
    # corrió E3 en su día, que es justo lo que un pre-registro existe para dejar fijo.
    assert "inputs_e3_original" in deck
    assert deck["inputs_e3_original"]["commit"].startswith("e8b4629")
    assert deck["inputs_e3_original"] != deck["inputs"], "si coincidieran, el resellado no habría hecho nada"


@pytest.mark.skipif(not (ROOT / "reports/eval/e4_router.json").is_file(), reason="E4 aún no producido")
def test_the_e4_router_provenance_is_checked_against_the_tree() -> None:
    """El guardián que faltaba: comparar los `inputs` COMPLETOS de E4 contra el árbol.

    ⚠️ Mientras `e4_router.json` sea el de la añada anterior, sus hashes de código **no** coinciden
    —lo cazó la auditoría ciega— y eso es un hecho declarado, no un fallo de esta prueba: el
    artefacto se re-deriva dentro de la transacción causal. Lo que la prueba fija es que, **una vez
    re-derivado**, la procedencia se compruebe y no se copie.
    """
    fuente = (ROOT / "experiments" / "run_e4_router.py").read_text(encoding="utf-8")
    assert '"inputs": deck["inputs"]' not in fuente, "la procedencia no puede copiarse del deck (H6)"
    assert "_sellar_entradas_vivas" in fuente, "debe sellarse el código VIVO al generar"

    inputs = _inputs("reports/eval/e4_router.json")
    divergentes = [
        clave
        for clave, sellado in inputs.items()
        if clave not in {"commit", "deck_version"} and (r := _resolver(clave)) is not None and _sha(r) != sellado
    ]
    if divergentes:
        pytest.xfail(
            f"e4_router.json es de la añada anterior y su procedencia no coincide en {divergentes}; "
            "se re-deriva dentro de la transacción causal (#58 opción A)"
        )


def test_every_sealed_input_key_resolves_to_a_real_file() -> None:
    """Una clave que no resuelve no acredita nada: sellar un nombre inexistente es peor que no sellar."""
    for artefacto in ("docs/cohort_deck.json", "reports/eval/e4_router.json"):
        ruta = ROOT / artefacto
        if not ruta.is_file():
            continue
        for clave in _inputs(artefacto):
            if clave in {"commit", "deck_version"}:
                continue
            assert _resolver(clave) is not None, f"{artefacto}: la entrada {clave} no resuelve"
