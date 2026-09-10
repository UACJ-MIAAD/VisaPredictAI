"""G2 · La amenaza al suministro de datos queda documentada, y la promesa retirada no vuelve.

El Apéndice A.9 del entregable cerraba diciendo que el sistema se mantiene «al día **sin
intervención manual**». Dejó de ser cierto cuando la fuente primaria puso sus páginas tras un
cortafuegos que rechaza clientes automatizados y los últimos meses entraron por una ingesta
manual gobernada. F2 ya prohibía esa promesa **en el sitio**; aquí se cierra el mismo hueco en
los documentos, que es donde vivía de verdad.

Las pruebas siembran la frase en una COPIA del repositorio y corren el guardián de verdad, con
el repositorio documental montado como en CI. El andamiaje vive en `tests/conftest.py`.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from tests.conftest import DELIVERABLE, LATEX_ROOT, RAIZ, sembrar_y_correr

RULES = RAIZ / "tools" / "consistency_rules.yml"
ENTREGABLE = LATEX_ROOT / DELIVERABLE


def _regla() -> dict:
    reglas = yaml.safe_load(RULES.read_text(encoding="utf-8"))["forbidden"]
    coincidencias = [r for r in reglas if "intervenci" in r["pattern"]]
    assert len(coincidencias) == 1, "debe haber exactamente una regla para la promesa de operación desatendida"
    return coincidencias[0]


@pytest.mark.parametrize(
    "frase",
    [
        # la redacción exacta que se retiró del Apéndice A.9
        "Así, el sistema se mantiene reproducible, auditado y al día sin intervención manual.",
        # la misma con el acento escapado, que es como estaba escrita en esa región del .tex
        "El sistema se mantiene al d\\'ia sin intervenci\\'on manual.",
        "El pipeline opera sin necesidad de intervención humana.",
    ],
)
def test_prometer_operacion_desatendida_tumba_al_guardian(repo_guardian: pathlib.Path, frase: str) -> None:
    salida = sembrar_y_correr(repo_guardian, frase)
    assert salida.returncode != 0, f"el guardián aceptó la promesa: {frase}"
    assert "ingestion_state" in salida.stdout + salida.stderr, "el diagnóstico debe apuntar al feed canónico"


@pytest.mark.parametrize(
    "frase",
    [
        # el texto nuevo de §2.3 habla del proceso desatendido y de la ingesta manual SIN prometer nada
        "Cuando su cortafuegos rechaza al cliente del proceso desatendido, la ingesta pasa a un "
        "procedimiento manual gobernado.",
        "La ingesta del mes pasó a ser semiautomática y conserva el congelado inmutable.",
        "El aprendizaje automático no interviene en la validación del mes.",
    ],
)
def test_hablar_de_la_ingesta_manual_no_es_prometer_automatismo(repo_guardian: pathlib.Path, frase: str) -> None:
    """La regla persigue la promesa, no la palabra: describir el proceso debe seguir siendo legal."""
    salida = sembrar_y_correr(repo_guardian, frase)
    assert salida.returncode == 0, salida.stdout + salida.stderr


def test_la_regla_no_esta_cableada_a_un_solo_documento() -> None:
    """Un guardián con el alcance de un archivo cubre un archivo (lección de M64)."""
    grupos = set(_regla()["in"])
    assert {"deliverable", "paper", "proposal"} <= grupos
    assert "docs" in grupos and "governance" in grupos


def test_la_razon_de_la_regla_nombra_la_autoridad_del_estado() -> None:
    """El estado de la fuente no se teclea en ningún sitio: se lee del feed canónico."""
    assert "ingestion_state.json" in _regla()["reason"]


def test_el_entregable_ya_no_promete_operacion_desatendida() -> None:
    assert "sin intervenci" not in ENTREGABLE.read_text(encoding="utf-8")


def test_el_entregable_documenta_la_amenaza_al_suministro() -> None:
    """Retirar la promesa sin documentar la amenaza dejaría el hueco, no lo cerraría."""
    texto = ENTREGABLE.read_text(encoding="utf-8")
    assert "Amenazas a la validez del suministro de datos" in texto, "falta el párrafo de §2.3"
    # una vez en §2.3 y otra en la lista de limitaciones de Conclusiones
    assert texto.count("semiautom") >= 2, "la amenaza debe quedar también en la lista de limitaciones"
    assert "cortafuegos" in texto, "la causa del bloqueo debe estar nombrada"


def test_la_amenaza_no_teclea_el_estado_volatil_de_la_fuente() -> None:
    """Un estado que cambia no puede quedar fijado en el documento: ni la fecha ni el código HTTP."""
    texto = ENTREGABLE.read_text(encoding="utf-8")
    ini = texto.index("Amenazas a la validez del suministro de datos")
    parrafo = texto[ini : texto.index("\\chapter{Conclusiones}")]
    for volatil in ("403", "status_since", "2026-09-02", "6-ago", "6 de agosto"):
        assert volatil not in parrafo, f"«{volatil}» es volátil y no puede quedar tecleado en la prosa"
