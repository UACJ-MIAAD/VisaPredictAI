"""M33: «N boletines» es siempre la cobertura de meses del panel.

Una entrada del plan público decía «Producción quedó fresh con 300 boletines». Los 300 son
**instantáneas archivadas**; los meses del panel son `n_months` (298). La regla del guardián
exigía el determinante («los|the N boletines») y por eso dejó pasar esa forma durante semanas.

Aquí se prueba la regla ampliada sembrando cada frase en una COPIA del repositorio y corriendo
el guardián de verdad: dos que deben tumbarlo y dos controles que deben pasar.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "tools" / "consistency_rules.yml"
DELIVERABLE = "reports/latex/ProyectoI_VisaPredictAI.tex"

IGNORE = shutil.ignore_patterns(
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
)


def _n_months() -> int:
    return json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())["n_months"]


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("guard") / "repo"
    shutil.copytree(ROOT, work, symlinks=True, ignore=IGNORE)
    return work


def _seed_and_run(repo: Path, phrase: str) -> subprocess.CompletedProcess:
    target = repo / DELIVERABLE
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(original + f"\n\n{phrase}\n", encoding="utf-8")
        return subprocess.run([sys.executable, "tools/check_consistency.py"], cwd=repo, capture_output=True, text=True)
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


def test_no_historical_literal_survives_in_the_rule_reasons() -> None:
    """Las razones documentaban «296», una cobertura de dos añadas atrás."""
    assert "296" not in RULES.read_text(encoding="utf-8")
