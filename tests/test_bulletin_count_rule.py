"""M33: «N boletines» es siempre la cobertura de meses del panel.

Una entrada del plan público decía «Producción quedó fresh con 300 boletines». Los 300 son
**instantáneas archivadas**; los meses del panel son `n_months` (298). La regla del guardián
exigía el determinante («los|the N boletines») y por eso dejó pasar esa forma durante semanas.

Aquí se prueba la regla ampliada sembrando cada frase en una COPIA del repositorio y corriendo
el guardián de verdad: dos que deben tumbarlo y dos controles que deben pasar. El andamiaje de la
copia (fixture y sembrador) vive en `tests/conftest.py`: lo comparten ya dos archivos de pruebas.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from tests.conftest import RAIZ, ignore_para, sembrar_y_correr

ROOT = RAIZ
RULES = ROOT / "tools" / "consistency_rules.yml"


def _n_months() -> int:
    return json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())["n_months"]


@pytest.mark.parametrize(
    "phrase",
    [
        "Producción quedó fresh con 300 boletines y un release inmutable.",
        "Production became fresh with 300 bulletins and an immutable release.",
    ],
)
def test_a_wrong_bulletin_count_is_rejected_without_a_determiner(repo_guardian: pathlib.Path, phrase: str) -> None:
    out = sembrar_y_correr(repo_guardian, phrase)
    assert out.returncode != 0, f"el guardián aceptó: {phrase}"
    assert "n_months" in out.stdout + out.stderr


@pytest.mark.parametrize("unidad", ["boletines", "bulletins"])
def test_the_canonical_count_passes(repo_guardian: pathlib.Path, unidad: str) -> None:
    out = sembrar_y_correr(repo_guardian, f"El corte cubre {_n_months()} {unidad} completos.")
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("unidad", ["snapshots", "instantáneas"])
def test_snapshot_counts_are_a_different_fact_and_do_not_fall_in_the_rule(
    repo_guardian: pathlib.Path, unidad: str
) -> None:
    """300 instantáneas archivadas es OTRO hecho: la regla no puede alcanzarlo."""
    out = sembrar_y_correr(repo_guardian, f"El archivo conserva 300 {unidad} congeladas.")
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_rule_no_longer_requires_a_determiner() -> None:
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    loose = [
        r
        for r in rules["numeric"]
        if r["fact"] == "n_months" and "boletines|bulletins" in r["label"] and "/" not in r["label"]
    ]
    assert len(loose) == 1
    label = loose[0]["label"]
    assert "los|the" not in label, "la regla seguía exigiendo determinante"
    assert "(?<![0-9])" in label, "sin la guarda, mordería los últimos dígitos de un número mayor"


def test_el_checkout_latex_anidado_queda_fuera_de_la_primera_copia() -> None:
    """M66-R1: en CI el repositorio LaTeX se clona en `ROOT/latex_repo`, dentro del de datos.

    La primera copia lo arrastraba y la segunda —la deliberada— moría con `FileExistsError`,
    seis veces, una por parametrización. Se comprueban los dos montajes: anidado (CI) y
    hermano (local), porque excluirlo siempre dejaría la copia incompleta en local.
    """
    entradas = ["reports", "tests", "tools", "latex_repo"]
    anidado = ignore_para(ROOT / "latex_repo")(str(ROOT), entradas)
    assert "latex_repo" in anidado, "el checkout anidado debe quedar fuera de la primera copia"
    hermano = ignore_para(ROOT.parent / "VisaPredictAI_LaTeX")(str(ROOT), entradas)
    assert "latex_repo" not in hermano, "con el repositorio hermano no hay nada que excluir"
    assert "reports" not in anidado and "tests" not in anidado, "no puede excluir de más"


def test_no_historical_literal_survives_in_the_rule_reasons() -> None:
    """Las razones documentaban «296», una cobertura de dos añadas atrás."""
    assert "296" not in RULES.read_text(encoding="utf-8")
