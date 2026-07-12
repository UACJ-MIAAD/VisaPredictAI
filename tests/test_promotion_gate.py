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


EVIDENCE = {"scorecard_champion": "aaa111", "scorecard_shadow": "bbb222", "shadow_ledger": "ccc333"}


def _decision_file(tmp_path, *, policy=None, decision="promote", candidate=None, rehash=True):
    """Decision file bien formada por default; cada test muta UN campo (A-02/R0-01).
    ``rehash=True`` re-sella el candidate_hash tras la mutación (para probar que el
    CONTENIDO mutado falla por su propia regla); ``rehash=False`` deja el hash viejo
    (para probar que la manipulación del registro muere por hash)."""
    pol = policy if policy is not None else promotion.POLICY
    cand = {
        "champion": "theta+ets+sarima",
        "challenger": "naive1",
        "release_id": "2026-07-feedcafe0000",
        "vintages": ["2026-08", "2026-09", "2026-10"],
        "decided_at": "2026-07-12T00:00:00+00:00",
        "evidence": dict(EVIDENCE),
    }
    base_hash = promotion.candidate_hash(cand, pol)
    if candidate is not None:
        cand = {**cand, **candidate}
        cand = {k: v for k, v in cand.items() if v is not ...}  # Ellipsis = borrar campo
    cand["hash"] = promotion.candidate_hash(cand, pol) if rehash else base_hash
    path = tmp_path / "promotion_decision.json"
    path.write_text(
        json.dumps(
            {
                "policy": pol,
                "by_table": {"FAD": {"decision": decision, "reasons": [], "candidate": cand}},
            }
        )
    )
    return path


def _pin_release(monkeypatch, value="2026-07-feedcafe0000"):
    from vp_model import ledger

    monkeypatch.setattr(ledger, "current_release_id", lambda: value)
    monkeypatch.setattr(ledger, "panel_vintage", lambda path=None: "2026-12")
    monkeypatch.setattr(promotion, "evidence_hashes", lambda root=None: dict(EVIDENCE))


AUTH = dict(challenger="naive1", champion="theta+ets+sarima")


def test_authorize_fails_closed(tmp_path, monkeypatch) -> None:
    _pin_release(monkeypatch)
    ok, why = promotion.authorize("FAD", tmp_path / "nope.json", **AUTH)
    assert not ok and "fail closed" in why
    path = _decision_file(tmp_path, decision="retain")
    assert not promotion.authorize("FAD", path, **AUTH)[0]
    assert not promotion.authorize("DFF", path, **AUTH)[0]  # tabla no cubierta
    ok, why = promotion.authorize("FAD", _decision_file(tmp_path), **AUTH)
    assert ok and "pre-registrada" in why


def test_authorize_rejects_stale_policy_version(tmp_path, monkeypatch) -> None:
    """Reproduccion EXACTA del auditor (A-02): policy_version '0.0-stale' + promote
    era aceptada. Ahora la politica integra debe coincidir con la vigente."""
    _pin_release(monkeypatch)
    path = _decision_file(tmp_path, policy={"policy_version": "0.0-stale"})
    ok, why = promotion.authorize("FAD", path, **AUTH)
    assert not ok and "0.0-stale" in why


def test_authorize_rejects_edited_policy_param(tmp_path, monkeypatch) -> None:
    _pin_release(monkeypatch)
    tampered = {**promotion.POLICY, "material_margin": 0.0}  # misma version, umbral relajado
    ok, why = promotion.authorize("FAD", _decision_file(tmp_path, policy=tampered), **AUTH)
    assert not ok and "pol" in why.lower()


def test_authorize_rejects_foreign_challenger(tmp_path, monkeypatch) -> None:
    """El shadow_recipe ajeno del auditor: la decision NO es cheque al portador."""
    _pin_release(monkeypatch)
    path = _decision_file(tmp_path)
    ok, why = promotion.authorize("FAD", path, challenger="deep_forgery", champion="theta+ets+sarima")
    assert not ok and "deep_forgery" in why


def test_authorize_rejects_changed_champion(tmp_path, monkeypatch) -> None:
    _pin_release(monkeypatch)
    ok, why = promotion.authorize("FAD", _decision_file(tmp_path), challenger="naive1", champion="otro")
    assert not ok and "campe" in why.lower()


def test_authorize_rejects_replayed_decision_from_old_release(tmp_path, monkeypatch) -> None:
    _pin_release(monkeypatch, "2026-08-000000000000")  # release nuevo tras la decision
    ok, why = promotion.authorize("FAD", _decision_file(tmp_path), **AUTH)
    assert not ok and "release" in why


def test_authorize_rejects_future_vintages_and_bad_date(tmp_path, monkeypatch) -> None:
    """Reproducciones EXACTAS de R0-01: vintages ['2099-01'] y decided_at 'not-a-date'
    eran autorizados; ausentes tambien. Todo eso muere ahora."""
    _pin_release(monkeypatch)
    ok, why = promotion.authorize(
        "FAD", _decision_file(tmp_path, candidate={"vintages": ["2099-01", "2099-02", "2099-03"]}), **AUTH
    )
    assert not ok and "POSTERIORES" in why
    ok, why = promotion.authorize("FAD", _decision_file(tmp_path, candidate={"decided_at": "not-a-date"}), **AUTH)
    assert not ok and "ISO" in why
    ok, why = promotion.authorize(
        "FAD", _decision_file(tmp_path, candidate={"vintages": ..., "decided_at": ...}), **AUTH
    )
    assert not ok
    ok, why = promotion.authorize("FAD", _decision_file(tmp_path, candidate={"vintages": ["2026-08"]}), **AUTH)
    assert not ok and "mínimo" in why  # muestra de añadas bajo el piso de la política


def test_authorize_rejects_tampered_record_by_hash(tmp_path, monkeypatch) -> None:
    """R0-01: mutar el registro SIN re-sellar el hash muere por candidate_hash."""
    _pin_release(monkeypatch)
    path = _decision_file(tmp_path, candidate={"vintages": ["2026-05", "2026-06", "2026-07"]}, rehash=False)
    ok, why = promotion.authorize("FAD", path, **AUTH)
    assert not ok and "candidate_hash" in why


def test_authorize_rejects_evidence_drift(tmp_path, monkeypatch) -> None:
    """R0-01: si los scorecards/ledger en disco ya no son los de la decision, muere."""
    _pin_release(monkeypatch)
    path = _decision_file(tmp_path)
    monkeypatch.setattr(promotion, "evidence_hashes", lambda root=None: {**EVIDENCE, "shadow_ledger": "MUTADO"})
    ok, why = promotion.authorize("FAD", path, **AUTH)
    assert not ok and "evidencia" in why


def test_authorize_rejects_pre_a02_decision_without_candidate(tmp_path, monkeypatch) -> None:
    _pin_release(monkeypatch)
    path = tmp_path / "promotion_decision.json"
    path.write_text(
        json.dumps({"policy": promotion.POLICY, "by_table": {"FAD": {"decision": "promote", "reasons": []}}})
    )
    ok, why = promotion.authorize("FAD", path, **AUTH)
    assert not ok and "candidato" in why


def test_policy_is_preregistered_and_floors_positive() -> None:
    p = promotion.POLICY
    assert p["preregistered_with_zero_live_pairs"] is True
    assert p["modes_allowed"] == ["live"]
    assert p["min_pairs_per_band"] >= 30 and p["min_live_vintages"] >= 3
    assert set(promotion.DECISIONS) == {"promote", "retain", "extend-shadow", "reject"}
