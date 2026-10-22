"""Figure producers must work without pre-existing gitignored output folders."""

from pathlib import Path

import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("statsmodels")

from experiments import make_eda_figures as figures
from vp_model import plots


def test_eda_pdf_creates_its_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "absent/latex/Figures"
    monkeypatch.setattr(figures, "FIG", directory)
    figures.fig_violin_country()
    assert (directory / "eda2_violin_country.pdf").stat().st_size > 1000


def test_eda_pdf_still_works_with_an_existing_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(figures, "FIG", tmp_path)
    figures.fig_violin_country()
    assert (tmp_path / "eda2_violin_country.pdf").stat().st_size > 1000


def test_plot_creates_its_contractual_latex_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plots, "OUTDIR", tmp_path / "reports/figures")
    fig, axis = plots.plt.subplots()
    axis.plot([0, 1], [0, 1])
    primary = plots._save(fig, "probe.png")
    assert primary.is_file()
    assert (tmp_path / "reports/latex/Figures/probe.png").read_bytes() == primary.read_bytes()
