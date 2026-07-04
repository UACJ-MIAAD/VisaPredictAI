"""Contracts for the MLOps-brutal epic (AO4-AO9, AP4/AP5, AM5).

Covers: public tracking.git_state (+ compat alias), model-card degradation fix,
horizon-matched drift baseline + coverage n-floor, drift history / re-campaign trigger,
Recipe serialization roundtrip, shadow best-challenger selection and the immutable
shadow ledger. Darts-dependent tests importorskip (base CI job has no modeling stack).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "experiments" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- AP5: tracking.git_state public + alias ---------------------------------


def test_git_state_public_and_alias() -> None:
    from vp_data import tracking

    sha, dirty = tracking.git_state()
    assert isinstance(sha, str) and sha
    assert isinstance(dirty, bool)
    assert tracking._git is tracking.git_state  # backwards-compat alias


# --- AP5: model card degrades cleanly without key_facts ----------------------


def test_model_card_fmt_and_degraded_build(tmp_path) -> None:
    mod = _load_script("build_model_card")
    assert mod._fmt(27611) == "27,611"
    assert mod._fmt("n/d") == "n/d"  # the C1 degradation sentinel must not crash
    mod.REPORTS = tmp_path  # no governance JSONs at all -> every _load returns {}
    try:
        md = mod.build()  # used to raise ValueError on f"{'n/d':,}"
    finally:
        mod.REPORTS = ROOT / "reports"
    assert "n/d" in md
    assert (tmp_path / "governance" / "MODEL_CARD.md").exists()


# --- AO7: horizon-matched drift baseline + coverage floor --------------------


def _scorecard(rows: list[dict], base: Path) -> None:
    (base / "prospective").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(base / "prospective" / "forecast_scorecard.csv", index=False)


def test_drift_baseline_is_horizon_matched(tmp_path) -> None:
    mod = _load_script("check_drift")
    rows = [{"origin": "2025-01", "h": h, "scaled_err": h * 0.1, "in95": 1} for h in range(1, 13)]
    rows += [{"origin": "2025-06", "h": h, "scaled_err": 0.5, "in95": 0} for h in (1, 2, 3)]
    _scorecard(rows, tmp_path)
    mod.REPORTS = tmp_path
    try:
        p = mod._performance_and_coverage()
    finally:
        mod.REPORTS = ROOT / "reports"
    assert p["status"] == "ok"
    assert p["horizons_matched"] == [1, 2, 3]
    # baseline = mean over 2025-01 at h in {1,2,3} only = 0.2 (the all-h mean 0.65 was the bug)
    assert p["baseline_mase"] == pytest.approx(0.2)
    assert p["latest_mase"] == pytest.approx(0.5)
    assert p["perf_ratio"] == pytest.approx(2.5)
    assert p["latest_n"] == 3
    # n=3 < 30: coverage must NOT be judged drift even though cov95 is 0.0
    assert p["coverage_evaluated"] is False
    assert p["coverage_drift"] is False
    assert p["performance_drift"] is True  # 2.5 > 1.5


def test_drift_history_streak_and_recampaign(tmp_path) -> None:
    mod = _load_script("check_drift")
    mod.REPORTS = tmp_path
    try:
        base = {"status": "ok", "perf_ratio": 2.0, "latest_n": 40}
        out = mod._update_history({**base, "latest_vintage": "2025-01", "performance_drift": True})
        assert out == {"consecutive_perf_drift": 1, "recampaign_due": False}
        # re-check of the SAME vintage updates in place, never double-counts
        out = mod._update_history({**base, "latest_vintage": "2025-01", "performance_drift": True})
        assert out["consecutive_perf_drift"] == 1
        out = mod._update_history({**base, "latest_vintage": "2025-02", "performance_drift": True})
        assert out == {"consecutive_perf_drift": 2, "recampaign_due": False}
        out = mod._update_history({**base, "latest_vintage": "2025-03", "performance_drift": True})
        assert out == {"consecutive_perf_drift": 3, "recampaign_due": True}
        # a clean vintage resets the streak
        out = mod._update_history({**base, "latest_vintage": "2025-04", "performance_drift": False})
        assert out == {"consecutive_perf_drift": 0, "recampaign_due": False}
        history = tmp_path / "governance" / "drift_history.jsonl"
        recs = [json.loads(line) for line in history.read_text().splitlines()]
        assert [r["vintage"] for r in recs] == ["2025-01", "2025-02", "2025-03", "2025-04"]
    finally:
        mod.REPORTS = ROOT / "reports"


# --- AP4: Recipe travels serialized ------------------------------------------


def test_recipe_from_dict_roundtrip() -> None:
    pytest.importorskip("darts")
    from dataclasses import asdict

    from vp_model import champion

    r = champion.Recipe(("theta", "ets", "sarima"), "mean")
    back = champion.recipe_from_dict(json.loads(json.dumps(asdict(r))))  # via JSON like the verdict
    assert back == r and back.name == "mean(theta+ets+sarima)"
    assert champion.recipe_from_dict({"models": ["sarima"]}).agg == "median"  # default agg


def test_evaluate_rows_carry_recipe() -> None:
    pytest.importorskip("darts")
    from vp_model import champion

    champ = champion.load_manifest()["FAD"]
    v = champion.evaluate("FAD", champ)
    for c in v.challengers:
        assert isinstance(c.get("recipe"), dict)
        assert list(c["recipe"]) >= ["models"]  # serialized Recipe, not just the pretty name
    if v.promote is not None:
        assert champion.recipe_from_dict(v.promote["recipe"]).name == v.promote["challenger"]


# --- AM5: CRPS is informative-only and degrades cleanly ----------------------


def test_crps_champion_degrades_to_none(tmp_path, monkeypatch) -> None:
    pytest.importorskip("darts")
    from vp_model import champion

    monkeypatch.setattr(champion, "REPORTS", tmp_path)
    assert champion.crps_champion("FAD") is None  # CSV absent
    (tmp_path / "eval").mkdir(parents=True)
    (tmp_path / "eval" / "crps_champion.csv").write_text("table,crps\nFAD,0.5\nFAD,0.7\nDFF,0.1\n")
    assert champion.crps_champion("FAD") == pytest.approx(0.6)
    assert champion.crps_champion("DFF") == pytest.approx(0.1)
    # when run_champion_crps.py's country=="ALL" aggregate row exists, it wins outright
    (tmp_path / "eval" / "crps_champion.csv").write_text(
        "table,country,crps\nFAD,mexico,100.0\nFAD,india,200.0\nFAD,ALL,150.0\n"
    )
    assert champion.crps_champion("FAD") == pytest.approx(150.0)
    (tmp_path / "eval" / "crps_champion.csv").write_text("unrelated_column\n1\n")
    assert champion.crps_champion("FAD") is None  # unexpected schema -> None, never raises


# --- AO6: shadow selection + immutable shadow ledger --------------------------


def test_best_challenger_selection() -> None:
    pytest.importorskip("darts")
    fs = _load_script("freeze_shadow")
    promoted = {"promote": {"challenger": "ets", "recipe": {"models": ["ets"], "agg": "median"}}}
    assert fs.best_challenger(promoted) == {"models": ["ets"], "agg": "median"}
    no_promote = {
        "champion": "median(theta+ets+sarima)",
        "promote": None,
        "challengers": [
            {"challenger": "median(theta+ets+sarima)", "mean": 0.10, "recipe": {"models": ["theta", "ets", "sarima"]}},
            {"challenger": "ets", "mean": 0.12, "recipe": {"models": ["ets"], "agg": "median"}},
            {"challenger": "theta", "mean": 0.15, "recipe": {"models": ["theta"], "agg": "median"}},
        ],
    }
    # skips the row that IS the champion; picks the lowest-mean remaining challenger
    assert fs.best_challenger(no_promote) == {"models": ["ets"], "agg": "median"}
    assert fs.best_challenger({"champion": "x", "promote": None, "challengers": []}) is None


def test_append_shadow_is_immutable(tmp_path, monkeypatch) -> None:
    pytest.importorskip("darts")
    fs = _load_script("freeze_shadow")
    monkeypatch.setattr(fs, "SHADOW_LOG", tmp_path / "forecast_log_shadow.csv")
    row = {
        "origin": "2026-07",
        "h": 1,
        "country": "mexico",
        "category": "F1",
        "table": "FAD",
        "date": "2026-08-01",
        "days": 1000,
        "lo80": 950,
        "hi80": 1050,
        "lo95": 900,
        "hi95": 1100,
        "shadow": True,
        "recipe": "ets",
        "hold_mase": 0.1,
    }
    fs.append_shadow([row])
    # C3: a re-run with a DIFFERENT prediction for the same (origin, series, date) is a no-op
    fs.append_shadow([{**row, "days": 9999}])
    df = pd.read_csv(tmp_path / "forecast_log_shadow.csv")
    assert len(df) == 1 and int(df.iloc[0].days) == 1000
    assert bool(df.iloc[0].shadow) is True


# --- AO5: birth certificate for the pickle manifest ---------------------------


def test_save_finalists_birth_certificate() -> None:
    pytest.importorskip("darts")
    sf = _load_script("save_finalists")
    birth = sf.birth_certificate()
    assert set(birth) == {"git_sha", "git_dirty", "panel_hash"}
    assert isinstance(birth["git_dirty"], bool)
    assert len(birth["panel_hash"]) == 12  # same md5[:12] convention as the model card
