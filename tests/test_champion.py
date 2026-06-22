"""Contrato del harness campeón–retador (rápido: lee forecasts persistidos, no reentrena)."""

from __future__ import annotations

import pytest

pytest.importorskip("darts")

from vp_model import champion  # noqa: E402


def test_champion_recipe_mase_finite_and_beats_naive() -> None:
    champ = champion.load_manifest()["FAD"]
    s = champion.recipe_series_mase("FAD", champ)
    assert len(s) > 10
    assert (s > 0).all() and s.notna().all()
    assert s.mean() < 1.0  # le gana al naïve (denominador del MASE)


def test_evaluate_wellformed_and_gated() -> None:
    champs = champion.load_manifest()
    for table in ("FAD", "DFF"):
        v = champion.evaluate(table, champs[table])
        assert 0 < v.champion_mean < 0.5
        assert v.challengers, "debe evaluar al menos un retador"
        for c in v.challengers:
            assert {"challenger", "mean", "wilcoxon_p", "holm_p", "promotable"} <= set(c)
            assert 0.0 <= c["holm_p"] <= 1.0
        # el gate es coherente: si hay promovido, es promovible y mejora en media
        if v.promote is not None:
            assert v.promote["promotable"]
            assert v.promote["mean_margin_vs_champion"] >= champion.MATERIAL_MARGIN


def test_manifest_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(champion, "MANIFEST", tmp_path / "m.json")
    original = {"FAD": champion.Recipe(("theta", "ets"), "median"), "DFF": champion.Recipe(("sarima",), "median")}
    champion.save_manifest(original)
    back = champion.load_manifest()
    assert back["FAD"].models == ("theta", "ets") and back["FAD"].agg == "median"
    assert back["DFF"].models == ("sarima",)
