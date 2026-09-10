"""G4 · Lo que el paper dice de las cohortes queda atado al artefacto que lo sostiene.

El paper incorpora el resultado negativo de la segmentación por estabilidad (E1-E4) sin teclear
una sola cifra: la afirmación es cualitativa porque el hallazgo lo es. Para que no se despegue de
su fuente, estas pruebas leen `reports/eval/e4_router.json` y `reports/governance/e5_facts.json`
y exigen que sigan diciendo lo mismo: **ningún router de cohorte gana**.

**Por qué una prueba y no una regla del guardián.** Se evaluó añadir un tripwire `forbidden` sobre
la afirmación contraria y se descartó: los nombres de las macros derivadas contienen los propios
verbos (`\\cohortFactRouterGana`, `\\cohortFactScanBate`) y las frases verdaderas del corpus son
negaciones («No cohort router clears it», «de N celdas, 0 ganan»), de modo que un patrón por
polaridad dispararía contra prosa correcta. La condición que importa es **numérica y vive en el
artefacto**, así que se comprueba ahí, que es donde puede expresarse exacta.
"""

from __future__ import annotations

import json

from tests.conftest import LATEX_ROOT, RAIZ

PAPER = LATEX_ROOT / "reports/paper_micai/paper.tex"
ENTREGABLE = LATEX_ROOT / "reports/latex/ProyectoI_VisaPredictAI.tex"
E5 = RAIZ / "reports/governance/e5_facts.json"
E4 = RAIZ / "reports/eval/e4_router.json"


def _e5() -> dict:
    return json.loads(E5.read_text())


def _prosa(ruta) -> str:
    """Texto con los saltos de línea colapsados: el `.tex` parte las frases donde le conviene."""
    return " ".join(ruta.read_text(encoding="utf-8").split())


def test_el_artefacto_sigue_diciendo_que_ningun_router_gana() -> None:
    """Si esto cambia, la prosa del paper y del entregable deja de ser cierta y hay que revisarla."""
    f = _e5()
    assert f["RouterGana"] == 0, "un router ganó: el paper afirma lo contrario y debe reescribirse"
    veredictos = json.loads(E4.read_text())["cells"]
    assert all(c["verdict"] != "gana_al_naive1" for c in veredictos)


def test_el_gate_del_router_sigue_exigiendo_los_tres_horizontes() -> None:
    """El paper dice «at three, six and twelve months simultaneously»: es el gate, no una elección."""
    gate = json.loads(E4.read_text())
    assert sorted(gate["horizons"]) == [3, 6, 12]
    assert gate["gate"]["material_margin"] > 0 and gate["gate"]["holm_alpha"] > 0


def test_el_paper_publica_el_resultado_negativo() -> None:
    texto = _prosa(PAPER)
    assert "Does segmenting the panel help" in texto, "falta el párrafo de cohortes"
    assert "No cohort router clears it" in texto, "falta la conclusión negativa"
    assert "not in either table, not for" in texto, "la negación debe cubrir las dos tablas y las dos cohortes"


def test_el_paper_marca_las_cohortes_como_exploratorias() -> None:
    """E1-E4 son exploratorias: el paper no puede presentarlas como confirmación."""
    texto = _prosa(PAPER)
    assert "exploratory" in texto
    assert "do not license a promotion decision" in texto


def test_el_paper_no_teclea_cifras_de_cohortes() -> None:
    """La afirmación es cualitativa a propósito: las cifras viven en el entregable, derivadas."""
    texto = _prosa(PAPER)
    ini = texto.index("Does segmenting the panel help")
    parrafo = texto[ini : texto.index("Threats to validity", ini)]
    f = _e5()
    for cifra in (str(f["RouterMargen"]), str(f["NEstable"]), str(f["NNoEstable"]), str(f["RouterCeldas"])):
        assert cifra not in parrafo, f"«{cifra}» se tecleó en el paper en vez de derivarse"


def test_el_descargo_provisional_sigue_en_pie() -> None:
    """Sólo F2-causal puede retirarlo, y el protocolo vigente no es ése."""
    import yaml

    reglas = yaml.safe_load((RAIZ / "tools" / "consistency_rules.yml").read_text(encoding="utf-8"))
    assert reglas["retro_protocol"] == "pre-F1", "el protocolo cambió: revisar el descargo antes de tocar el paper"
    assert reglas["provisional_caveat"]["pattern"] in _prosa(PAPER)


def test_el_entregable_conserva_su_version_derivada_del_mismo_hallazgo() -> None:
    """G3: la subsección de E5 ya existía; el paper la acompaña, no la sustituye."""
    texto = _prosa(ENTREGABLE)
    assert "Cohortes de estabilidad y enrutado por cohorte" in texto
    assert "\\input{cohortes.tex}" in texto and "\\input{cohorts_facts}" in texto
    assert "cohortFactRouterGana" in texto, "la cifra del entregable debe seguir siendo una macro derivada"
