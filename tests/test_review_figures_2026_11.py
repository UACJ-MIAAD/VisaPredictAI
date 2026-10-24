"""Figures must say what the documents and the authoritative artifacts say (full PDF review, 23-oct-2026).

The critical-difference diagram has to draw the ranking reported by significance_summary.json
(DFF deduplicated to 14 distinct series), and the gap-policy figure may only describe short-gap
interpolation as descriptive: the modeling grid fills causally with the last observation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_cd_diagram_ranks_come_from_the_significance_summary(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    pytest.importorskip("scipy")
    from experiments import make_result_figures as mrf

    summary = {
        "ranking": {
            "DFF": {
                "n_series": 14,
                "n_series_raw": 25,
                "n_models": 3,
                "friedman_p": 1e-20,
                "avg_rank": {"theta": 2.1, "naive1": 1.4, "ets": 2.5},
            }
        }
    }
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "significance_summary.json").write_text(json.dumps(summary))
    monkeypatch.setattr(mrf, "REP", tmp_path)
    avg, cd, n, k, pval = mrf._avg_ranks("DFF")
    assert (n, k, pval) == (14, 3, 1e-20)
    assert list(avg.index) == ["naive1", "theta", "ets"]
    assert cd > 0


def test_cd_diagram_rejects_a_summary_whose_ranks_disagree_with_its_model_count(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    pytest.importorskip("scipy")
    from experiments import make_result_figures as mrf

    summary = {"ranking": {"FAD": {"n_series": 25, "n_models": 4, "friedman_p": 0.0, "avg_rank": {"naive1": 1.6}}}}
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "significance_summary.json").write_text(json.dumps(summary))
    monkeypatch.setattr(mrf, "REP", tmp_path)
    with pytest.raises(ValueError):
        mrf._avg_ranks("FAD")


@pytest.mark.parametrize("lang", ["es", "en"])
def test_gap_figure_limits_interpolation_to_the_descriptive_grid(lang, monkeypatch):
    pytest.importorskip("matplotlib")
    monkeypatch.syspath_prepend(str(ROOT / "experiments"))  # make_fe_figures imports _figkit as a script
    from experiments import make_fe_figures as fe

    texts = fe.TXT[lang]
    rendered = " ".join(
        [
            texts["f04_head"].format(cap=3, n=2),
            texts["f04_sub"].format(name="x", miss=1, runs=1, maxrun=9, cap=3),
            texts["f04_interp"].format(cap=3),
        ]
    ).lower()
    assert ("training grid" not in rendered) and ("rejilla de entrenamiento" not in rendered)
    assert ("descriptive" in rendered) or ("descriptiva" in rendered)
