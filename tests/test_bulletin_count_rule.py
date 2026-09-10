"""M33: «N boletines» es siempre la cobertura de meses del panel.

Una entrada del plan público decía «Producción quedó fresh con 300 boletines». Los 300 son
**instantáneas archivadas**; los meses del panel son `n_months` (298). La regla del guardián
exigía el determinante («los|the N boletines») y por eso dejó pasar esa forma durante semanas.

Aquí se prueba la regla ampliada sembrando cada frase en una COPIA del repositorio y corriendo
el guardián de verdad: dos que deben tumbarlo y dos controles que deben pasar.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "tools" / "consistency_rules.yml"
DELIVERABLE = "reports/latex/ProyectoI_VisaPredictAI.tex"
_latex_raw = os.environ.get("VP_LATEX_DIR")
LATEX_ROOT = (
    ((ROOT / _latex_raw) if _latex_raw and not Path(_latex_raw).is_absolute() else Path(_latex_raw))
    if _latex_raw
    else ROOT.parent / "VisaPredictAI_LaTeX"
)
LATEX_ROOT = LATEX_ROOT.resolve()


def _nombre_si_cuelga_del_repo(destino: Path) -> str | None:
    """Nombre de primer nivel de `destino` si vive DENTRO de `ROOT`; `None` si es hermano.

    En local el repositorio LaTeX es hermano del de datos; en CI se hace checkout en
    `ROOT/latex_repo` (`VP_LATEX_DIR`). Se deriva del valor efectivo en vez de teclear el
    nombre: si el workflow cambia la ruta del checkout, esto la sigue.
    """
    try:
        partes = destino.relative_to(ROOT).parts
    except ValueError:
        return None
    return partes[0] if partes else None


def _ignore_para(latex_root: Path):
    """Qué se deja fuera de la copia del repositorio de datos.

    Cuando el repositorio LaTeX cuelga del de datos hay que excluirlo de la PRIMERA copia: la
    SEGUNDA lo copia a propósito a `work/latex_repo` y, si ya estaba, `copytree` revienta con
    `FileExistsError`. En local no pasaba porque allí el repositorio es hermano.
    """
    anidado = _nombre_si_cuelga_del_repo(latex_root)
    return shutil.ignore_patterns(
        ".git",
        "ante",
        "ante_nf",
        ".vp_envs",
        "data",
        "models",
        "mlartifacts",
        "mlruns_staging",
        "node_modules",
        ".dvc",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        *((anidado,) if anidado else ()),
    )


IGNORE = _ignore_para(LATEX_ROOT)


def _n_months() -> int:
    return json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())["n_months"]


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("guard") / "repo"
    shutil.copytree(ROOT, work, symlinks=True, ignore=IGNORE)
    shutil.copytree(
        LATEX_ROOT,
        work / "latex_repo",
        ignore=shutil.ignore_patterns(".git", "*.pdf", "*.log", "*.aux", "*.out", "*.toc", "*.lof", "*.lot"),
    )
    return work


def _seed_and_run(repo: Path, phrase: str) -> subprocess.CompletedProcess:
    target = repo / "latex_repo" / DELIVERABLE
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(original + f"\n\n{phrase}\n", encoding="utf-8")
        env = {**os.environ, "VP_LATEX_DIR": "latex_repo"}
        return subprocess.run(
            [sys.executable, "tools/check_consistency.py"], cwd=repo, env=env, capture_output=True, text=True
        )
    finally:
        target.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(
    "phrase",
    [
        "Producción quedó fresh con 300 boletines y un release inmutable.",
        "Production became fresh with 300 bulletins and an immutable release.",
    ],
)
def test_a_wrong_bulletin_count_is_rejected_without_a_determiner(repo: Path, phrase: str) -> None:
    out = _seed_and_run(repo, phrase)
    assert out.returncode != 0, f"el guardián aceptó: {phrase}"
    assert "n_months" in out.stdout + out.stderr


@pytest.mark.parametrize("unidad", ["boletines", "bulletins"])
def test_the_canonical_count_passes(repo: Path, unidad: str) -> None:
    out = _seed_and_run(repo, f"El corte cubre {_n_months()} {unidad} completos.")
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("unidad", ["snapshots", "instantáneas"])
def test_snapshot_counts_are_a_different_fact_and_do_not_fall_in_the_rule(repo: Path, unidad: str) -> None:
    """300 instantáneas archivadas es OTRO hecho: la regla no puede alcanzarlo."""
    out = _seed_and_run(repo, f"El archivo conserva 300 {unidad} congeladas.")
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
    anidado = _ignore_para(ROOT / "latex_repo")(str(ROOT), entradas)
    assert "latex_repo" in anidado, "el checkout anidado debe quedar fuera de la primera copia"
    hermano = _ignore_para(ROOT.parent / "VisaPredictAI_LaTeX")(str(ROOT), entradas)
    assert "latex_repo" not in hermano, "con el repositorio hermano no hay nada que excluir"
    assert "reports" not in anidado and "tests" not in anidado, "no puede excluir de más"


def test_no_historical_literal_survives_in_the_rule_reasons() -> None:
    """Las razones documentaban «296», una cobertura de dos añadas atrás."""
    assert "296" not in RULES.read_text(encoding="utf-8")
