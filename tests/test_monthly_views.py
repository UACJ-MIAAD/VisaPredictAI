"""Numeric ER refresh preserves geometry and fails on missing/ambiguous bindings."""

from pathlib import Path

import pytest

from tools.refresh_monthly_views import render_er

SOURCE = '<svg><g id="fact"><text>raw_value · 12,345 filas</text></g><g id="mart"><text>10,000 filas · listo para modelar</text></g></svg>'


def test_counts_come_from_current_facts_without_changing_diagram_geometry() -> None:
    actual = render_er(SOURCE, {"n_obs": 28061, "n_obs_F": 16220})
    assert "raw_value · 28,061 filas" in actual
    assert "16,220 filas · listo para modelar" in actual
    assert actual.replace("28,061", "12,345").replace("16,220", "10,000") == SOURCE
    assert render_er(actual, {"n_obs": 28061, "n_obs_F": 16220}) == actual


def test_real_diagram_bindings_match_the_current_canonical_facts() -> None:
    import json

    root = Path(__file__).resolve().parents[1]
    svg = (root / "docs/schema_er.svg").read_text()
    facts = json.loads((root / "reports/governance/key_facts.json").read_text())
    assert render_er(svg, facts) == svg


@pytest.mark.parametrize(
    "facts", [{"n_obs": True, "n_obs_F": 1}, {"n_obs": 2, "n_obs_F": 3}, {"n_obs": 0, "n_obs_F": 0}]
)
def test_invalid_counts_cannot_become_a_diagram(facts: dict) -> None:
    with pytest.raises(ValueError):
        render_er(SOURCE, facts)


@pytest.mark.parametrize("svg", [SOURCE + SOURCE, SOURCE.replace("raw_value ·", "other ·")])
def test_missing_or_ambiguous_binding_rejects(svg: str) -> None:
    with pytest.raises(ValueError, match="binding"):
        render_er(svg, {"n_obs": 28061, "n_obs_F": 16220})
