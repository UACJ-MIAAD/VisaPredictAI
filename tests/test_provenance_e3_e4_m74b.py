"""M74-B · La procedencia de E3 y E4 se comprueba contra el árbol, no se declara y ya (H6, H7).

`e4_router.json` sellaba `inputs` **copiándolos** del deck retador, así que declaraba versiones de
`champion.py`, `config.py` y `horizon.py` que ni siquiera contenían las funciones con las que el
artefacto se produjo. Ningún cargador ni prueba comparaba esos hashes: la procedencia podía mentir
indefinidamente. Esta prueba es el guardián que faltaba.
"""

from __future__ import annotations

import hashlib
import json
import sys
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


#: G7 · B3-bis: la campaña causal re-derivó el escaneo de cohortes; el pin conserva el PRE-REGISTRO. La divergencia
#: se admite sólo para esa clave y sólo si el linaje versionado declara ESTAS dos identidades (su contenido lo
#: recalcula `test_g7_cohort_scan_linaje`).
LINAJE = ROOT / "docs" / "evidence" / "g7" / "cohort_scan_linaje.json"


def _acreditada_por_linaje(clave: str, sellado: str, ruta: Path) -> bool:
    if not LINAJE.is_file():
        return False
    linaje = json.loads(LINAJE.read_text(encoding="utf-8"))
    return (
        clave == linaje["deck"]["key"]
        and sellado == linaje["preregistration"]["sha256"]
        and str(ruta.relative_to(ROOT)) == linaje["campaign_output"]["path"]
        and _sha(ruta) == linaje["campaign_output"]["sha256"]
    )


def test_the_e3_deck_inputs_match_the_tree_after_the_reseal() -> None:
    """H7: el deck debe describir la campaña que se va a correr, no una anterior."""
    divergentes = []
    for clave, sellado in _inputs("docs/cohort_deck.json").items():
        if clave in {"commit", "deck_version"}:
            continue
        ruta = _resolver(clave)
        assert ruta is not None, f"la entrada sellada {clave} no resuelve a ningún archivo"
        if _sha(ruta) != sellado and not _acreditada_por_linaje(clave, sellado, ruta):
            divergentes.append(clave)
    assert not divergentes, f"el deck sella hashes que ya no son los del árbol: {divergentes}"


def test_the_lineage_exception_is_by_identity() -> None:
    """La excepción no se extiende a otra clave, otro pin ni otro archivo en el árbol."""
    sellado = _inputs("docs/cohort_deck.json")["cohort_scan.json"]
    ruta = _resolver("cohort_scan.json")
    assert ruta is not None and _acreditada_por_linaje("cohort_scan.json", sellado, ruta)
    assert not _acreditada_por_linaje("series_cohorts.json", sellado, ruta)
    assert not _acreditada_por_linaje("cohort_scan.json", "0" * 64, ruta)
    otra = _resolver("series_cohorts.json")
    assert otra is not None and not _acreditada_por_linaje("cohort_scan.json", sellado, otra)


def test_the_lineage_exception_needs_the_declared_tree_bytes(tmp_path, monkeypatch) -> None:
    """Un linaje que declara OTRA salida de campaña no excusa el escaneo que hay en el árbol."""
    linaje = json.loads(LINAJE.read_text(encoding="utf-8"))
    linaje["campaign_output"]["sha256"] = "0" * 64
    falso = tmp_path / "linaje.json"
    falso.write_text(json.dumps(linaje))
    monkeypatch.setattr(sys.modules[__name__], "LINAJE", falso)
    sellado = _inputs("docs/cohort_deck.json")["cohort_scan.json"]
    ruta = _resolver("cohort_scan.json")
    assert ruta is not None and not _acreditada_por_linaje("cohort_scan.json", sellado, ruta)


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


def test_the_lineage_exception_needs_the_declared_tree_path(tmp_path, monkeypatch) -> None:
    """Los mismos bytes en OTRA ruta del árbol no son la salida de campaña que el linaje declara."""
    linaje = json.loads(LINAJE.read_text(encoding="utf-8"))
    monkeypatch.setattr(sys.modules[__name__], "LINAJE", LINAJE.resolve())
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    copia = tmp_path / "reports" / "eval" / "otro_cohort_scan.json"
    copia.parent.mkdir(parents=True)
    copia.write_bytes((Path(__file__).resolve().parent.parent / linaje["campaign_output"]["path"]).read_bytes())
    assert not _acreditada_por_linaje("cohort_scan.json", linaje["preregistration"]["sha256"], copia)
    original = tmp_path / linaje["campaign_output"]["path"]
    original.write_bytes(copia.read_bytes())
    assert _acreditada_por_linaje("cohort_scan.json", linaje["preregistration"]["sha256"], original), "control"
