"""Bindings are expanded, not exempted from the existing numeric guardian."""

import json
from pathlib import Path

import pytest

from tests.conftest import RAIZ, sembrar_y_correr
from tools.census_bindings import census_values, render_census


def real_values():
    return census_values(
        json.loads((RAIZ / "reports/governance/key_facts.json").read_text()),
        json.loads((RAIZ / "reports/eda/eda_facts.json").read_text()),
    )


def test_real_census_formats_without_retyping_numbers():
    values = real_values()
    assert (
        render_census("{{census:n_obs:comma}} / {{census:n_months}}", values)
        == f"{values['n_obs']:,} / {values['n_months']}"
    )


@pytest.mark.parametrize("source", ["{{census:typo}}", "{{census:n_obs:invalid}}", "{{census:date_last:comma}}"])
def test_unknown_and_malformed_bindings_fail(source):
    with pytest.raises(ValueError):
        render_census(source, real_values())


def test_invalid_census_never_becomes_text():
    facts = json.loads((RAIZ / "reports/governance/key_facts.json").read_text())
    eda = json.loads((RAIZ / "reports/eda/eda_facts.json").read_text())
    with pytest.raises(ValueError):
        census_values({**facts, "n_obs": True}, eda)
    eda["series"].pop()
    with pytest.raises(ValueError):
        census_values(facts, eda)


def test_a_valid_binding_passes_the_actual_guardian(repo_guardian: Path):
    result = sembrar_y_correr(repo_guardian, "{{census:n_obs:comma}} observaciones del panel.")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_wrong_binding_is_still_a_numeric_violation(repo_guardian: Path):
    result = sembrar_y_correr(repo_guardian, "{{census:n_obs_F:comma}} observaciones del panel.")
    assert result.returncode != 0
    assert "NUMERIC" in result.stdout and "n_obs" in result.stdout


def test_an_unknown_binding_blocks_the_actual_guardian(repo_guardian: Path):
    result = sembrar_y_correr(repo_guardian, "{{census:unknown}} observaciones del panel.")
    assert result.returncode != 0
    assert "BINDING" in result.stdout


def test_cli_finds_its_own_helper_without_an_editable_install(repo_guardian: Path):
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-I", "tools/check_consistency.py"],
        cwd=repo_guardian,
        env={**os.environ, "VP_LATEX_DIR": "latex_repo"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
