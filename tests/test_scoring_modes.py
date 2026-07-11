"""A3: los agregados del scoring JAMÁS combinan backfill y live; head-to-head por pares.

Cubre la aceptación del paquete: agregados separados por evaluation_mode (overall anclado
a backfill), scoring del sombra con la misma maquinaria, y comparación campeón-vs-sombra
solo entre pares del mismo modo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))

import score_forecasts as sf  # noqa: E402


def _scored_row(mode: str, h: int = 1, scaled: float = 0.1, table: str = "FAD", origin: str = "2024-07") -> dict:
    return {
        "origin": origin,
        "h": h,
        "country": "mexico",
        "category": "F1",
        "table": table,
        "target": f"2026-{h:02d}-01",
        "pred": 1000,
        "actual": 990.0,
        "error": 10,
        "abs_err": 10.0,
        "scaled_err": scaled,
        "in80": 1,
        "in95": 1,
        "evaluation_mode": mode,
        "model_version": "median(theta+ets+sarima)",
    }


def test_mode_blocks_never_blend_backfill_and_live() -> None:
    sdf = pd.DataFrame([_scored_row("backfill", scaled=0.1), _scored_row("live", h=2, scaled=0.9)])
    back, by_mode = sf._mode_blocks(sdf)
    assert set(back["evaluation_mode"]) == {"backfill"}  # overall se ancla a backfill
    assert sf._agg(back)["mase"] == 0.1  # sin dilución por la fila live (0.9)
    assert set(by_mode) == {"backfill", "live"}
    assert by_mode["live"]["overall"]["mase"] == 0.9  # live reporta aparte, sin mezclarse


def test_mode_blocks_degrade_without_mode_column() -> None:
    sdf = pd.DataFrame([{k: v for k, v in _scored_row("x").items() if k != "evaluation_mode"}])
    back, by_mode = sf._mode_blocks(sdf)
    assert len(back) == 1 and by_mode == {}  # frames pre-v2 no revientan el scoring


def test_score_rows_carries_mode_and_recipe() -> None:
    fc = pd.DataFrame(
        [
            {
                "origin": "2024-01",
                "h": 1,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2024-02-01",
                "days": 1000,
                "lo80": 950,
                "hi80": 1050,
                "lo95": 900,
                "hi95": 1100,
                "evaluation_mode": "backfill",
                "model_version": "sarima",
            }
        ]
    )
    scored, _ = sf._score_rows(fc, {("mexico", "F1", "FAD", "2024-02-01"): 1010.0}, lambda *_: 100.0)
    assert scored[0]["evaluation_mode"] == "backfill" and scored[0]["model_version"] == "sarima"


def test_head_to_head_pairs_same_mode_only() -> None:
    champ = pd.DataFrame(
        [
            _scored_row("backfill", h=1, scaled=0.2),
            _scored_row("live", h=2, scaled=0.3),
            _scored_row("backfill", h=3, scaled=0.4),
        ]
    )
    shadow = pd.DataFrame(
        [
            {**_scored_row("backfill", h=1, scaled=0.1), "model_version": "naive1"},
            {**_scored_row("backfill", h=2, scaled=0.5), "model_version": "naive1"},  # champ dice live → mixto
        ]
    )
    out = sf._head_to_head(champ, shadow)
    assert out["n_pairs"] == 2  # h=3 no tiene par en el sombra
    assert out["n_mixed_mode_excluded"] == 1  # el par h=2 cruza modos y se excluye
    blk = out["by_table"]["FAD"]["backfill"]
    assert blk["n"] == 1 and blk["insufficient_n"] is True
    assert blk["champion"]["mase"] == 0.2 and blk["shadow"]["mase"] == 0.1
    assert blk["shadow"]["model_version"] == ["naive1"]
    assert blk["by_horizon"][1]["shadow_mase"] == 0.1


def test_head_to_head_empty_sides() -> None:
    assert sf._head_to_head(pd.DataFrame(), pd.DataFrame())["n_pairs"] == 0


def test_real_outputs_respect_mode_separation() -> None:
    """Integración: el meta real declara el alcance A3 y sus modos no se mezclan."""
    import json

    meta_path = sf.REPORTS / "prospective" / "forecast_scorecard_meta.json"
    if not meta_path.exists():
        return
    meta = json.loads(meta_path.read_text())
    if "by_mode" not in meta:  # aún no re-puntuado tras A3
        return
    assert "SOLO" in meta["aggregation_scope"]
    n_by_mode = sum(b["overall"]["n"] for b in meta["by_mode"].values())
    assert n_by_mode == meta["n_scored"]  # los modos particionan el scored, sin traslape
    if "backfill" in meta["by_mode"]:
        assert meta["overall"]["n"] == meta["by_mode"]["backfill"]["overall"]["n"]
