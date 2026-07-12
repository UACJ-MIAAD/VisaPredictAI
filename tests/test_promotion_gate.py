"""A4: el gate de promoción pre-registrado — la muestra insuficiente NUNCA promueve.

Cubre la aceptación del paquete: decisiones promote/retain/extend-shadow/reject, solo
pares live autorizan, fail-closed en la autorización humana, y el colapso de réplicas
del corte mundial.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vp_model import promotion  # noqa: E402


def _pairs_frame(
    mode: str = "live",
    factor: float = 0.5,
    n_series: int = 12,
    vintages: tuple[str, ...] = ("2026-08", "2026-09", "2026-10"),
    factor_by_h=None,
) -> pd.DataFrame:
    rows = []
    for origin in vintages:
        for s in range(n_series):
            ce = 0.2 + 0.01 * s
            for h in range(1, 13):
                f = factor_by_h(h) if factor_by_h else factor
                rows.append(
                    {
                        "origin": origin,
                        "country": f"pais{s}",
                        "category": "F1",
                        "table": "FAD",
                        "target": f"{origin}-t{h:02d}",
                        "h": h,
                        "actual_champ": 100.0,
                        "pred_champ": 100.0 + s,
                        "pred_shadow": 99.0,
                        "scaled_err_champ": ce,
                        "scaled_err_shadow": ce * f + 0.001 * s,
                        "in95_champ": 1,
                        "in95_shadow": 1,
                        "evaluation_mode_champ": mode,
                        "evaluation_mode_shadow": mode,
                    }
                )
    return pd.DataFrame(rows)


def test_backfill_pairs_never_promote() -> None:
    out = promotion.decide(_pairs_frame(mode="backfill", factor=0.1))  # margen brutal, pero backfill
    assert out["n_pairs_live"] == 0
    assert out["by_table"]["FAD"]["decision"] == "extend-shadow"


def test_insufficient_vintages_extends_shadow() -> None:
    out = promotion.decide(_pairs_frame(vintages=("2026-08",)))
    entry = out["by_table"]["FAD"]
    assert entry["decision"] == "extend-shadow"
    assert any("añadas live" in r for r in entry["reasons"])


def test_insufficient_band_pairs_extends_shadow() -> None:
    out = promotion.decide(_pairs_frame(n_series=2))  # 2×3 añadas×3 h = 18 pares/banda < 30
    entry = out["by_table"]["FAD"]
    assert entry["decision"] == "extend-shadow"
    assert any("muestra insuficiente" in r for r in entry["reasons"])


def test_dominant_live_challenger_promotes() -> None:
    pytest.importorskip("scipy")
    out = promotion.decide(_pairs_frame(factor=0.5))
    entry = out["by_table"]["FAD"]
    assert entry["decision"] == "promote"
    assert all(b["significantly_better"] for b in entry["by_band"].values())
    assert any("aprobación humana" in r for r in entry["reasons"])


def test_significant_regression_rejects() -> None:
    pytest.importorskip("scipy")
    out = promotion.decide(_pairs_frame(factor_by_h=lambda h: 0.5 if h <= 6 else 1.4))
    assert out["by_table"]["FAD"]["decision"] == "reject"


def test_reject_requires_holm_adjusted_worse() -> None:
    """Auditoria 11-jul: el rechazo evaluaba p_worse CRUDO en multiples bandas (falso
    positivo familiar posible). La decision debe usar la familia Holm-ajustada; el
    veredicto expone holm_p_worse/significantly_worse por banda."""
    pytest.importorskip("scipy")
    out = promotion.decide(_pairs_frame(factor=1.30))  # retador uniformemente peor
    res = out["by_table"]["FAD"]
    assert res["decision"] == "reject"
    for s_ in res["by_band"].values():
        assert "holm_p_worse" in s_ and "significantly_worse" in s_
        assert s_["holm_p_worse"] >= s_["p_worse"]  # Holm nunca hace mas facil rechazar


def test_immaterial_margin_retains() -> None:
    pytest.importorskip("scipy")
    out = promotion.decide(_pairs_frame(factor=0.97))  # mejora 3% < margen material 10%
    entry = out["by_table"]["FAD"]
    assert entry["decision"] == "retain"


def test_dedup_collapses_world_replicas() -> None:
    base = {
        "origin": "2026-08",
        "category": "F4",
        "table": "FAD",
        "h": 1,
        "scaled_err_champ": 0.1,
        "scaled_err_shadow": 0.05,
        "in95_champ": 1,
        "in95_shadow": 1,
    }
    rows = []
    for country, pred in (("all_chargeability", 100.0), ("mexico", 100.0), ("india", 200.0)):
        rows.append(
            {**base, "country": country, "target": "t1", "actual_champ": 50.0, "pred_champ": pred, "pred_shadow": pred}
        )
    dd = promotion._dedup_pairs(pd.DataFrame(rows))
    kept = set(dd["country"])
    assert "all_chargeability" in kept and "india" in kept
    assert "mexico" not in kept  # réplica exacta del corte mundial → colapsa


def test_authorize_fails_closed(tmp_path) -> None:
    path = tmp_path / "promotion_decision.json"
    ok, why = promotion.authorize("FAD", path)
    assert not ok and "fail closed" in why
    path.write_text(
        json.dumps({"policy": {"policy_version": "1.0"}, "by_table": {"FAD": {"decision": "retain", "reasons": ["x"]}}})
    )
    ok, _ = promotion.authorize("FAD", path)
    assert not ok
    ok, _ = promotion.authorize("DFF", path)  # tabla no cubierta
    assert not ok
    path.write_text(
        json.dumps({"policy": {"policy_version": "1.0"}, "by_table": {"FAD": {"decision": "promote", "reasons": []}}})
    )
    ok, why = promotion.authorize("FAD", path)
    assert ok and "pre-registrada" in why


def test_policy_is_preregistered_and_floors_positive() -> None:
    p = promotion.POLICY
    assert p["preregistered_with_zero_live_pairs"] is True
    assert p["modes_allowed"] == ["live"]
    assert p["min_pairs_per_band"] >= 30 and p["min_live_vintages"] >= 3
    assert set(promotion.DECISIONS) == {"promote", "retain", "extend-shadow", "reject"}
