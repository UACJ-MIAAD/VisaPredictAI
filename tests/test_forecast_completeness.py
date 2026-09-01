"""Regresiones A7.3-R2: universo modelable, contracción y fallos numéricos.

Los tests de ``run`` son deliberadamente conductuales: ejercitan el publicador con
un catálogo sintético. Así reproducen el defecto de producción (una serie corta se
añadía al expected antes de intentar modelarla) sin depender de una API nueva.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("darts")

from experiments import generate_web_forecasts as gwf  # noqa: E402
from vp_model import config  # noqa: E402

ROW = {
    "origin": "2026-09",
    "h": 1,
    "country": "mexico",
    "category": "F1",
    "table": "FAD",
    "date": "2026-10-01",
    "days": 100,
    "lo80": 90,
    "hi80": 110,
    "lo95": 80,
    "hi95": 120,
    "band_method": "q_h",
}


class _FakeTs:
    def __init__(self, n: int):
        self._n = n

    def __len__(self) -> int:
        return self._n


def _fake_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    current: dict[str, int],
    produced: set[str],
    previous: set[str],
) -> None:
    """Ejecuta ``run`` con FAD sintético; ``current`` es categoría→n meses."""

    reports = tmp_path / "reports"
    prospective = reports / "prospective"
    prospective.mkdir(parents=True)
    (prospective / "web_forecasts_meta.json").write_text(
        json.dumps({"n_series": len(previous), "series": {k: {} for k in sorted(previous)}}) + "\n"
    )

    monkeypatch.setattr(gwf, "REPORTS", reports)
    monkeypatch.setattr(gwf, "BOOTSTRAP_MIN_CATALOG", {"FAD": 0}, raising=False)
    monkeypatch.setattr(gwf, "BOOTSTRAP_MIN_ELIGIBLE", {"FAD": 0}, raising=False)
    monkeypatch.setattr(config, "TABLES", ("FAD",))
    monkeypatch.setattr(gwf.config, "TABLES", ("FAD",))
    monkeypatch.setattr(gwf.config, "PILOT_COUNTRIES", ("mexico",))
    recipe = SimpleNamespace(name="test-recipe", models=("naive1",))
    monkeypatch.setattr(gwf.champion, "load_manifest", lambda: {"FAD": recipe})
    monkeypatch.setattr(gwf, "_load_pi_scales", lambda: None)
    monkeypatch.setattr(gwf, "_load_aci_gamma", lambda: {"FAD": 0.05})
    monkeypatch.setattr(gwf, "_ledger_hits", lambda: {})

    def list_series(*, table, block, countries):
        assert table == "FAD" and countries == ("mexico",)
        rows = [{"country": "mexico", "category": cat} for cat in current] if block == "family" else []
        return pd.DataFrame(rows, columns=["country", "category"])

    monkeypatch.setattr(gwf.dataset, "list_series", list_series)
    monkeypatch.setattr(
        gwf.dataset,
        "load_series",
        lambda country, category, table: pd.Series(
            range(current[category]),
            index=pd.date_range("2000-01-01", periods=current[category], freq="MS"),
            dtype="float64",
        ),
    )
    monkeypatch.setattr(gwf.models, "to_timeseries", lambda raw: _FakeTs(len(raw)))

    def series_forecast(country, category, table, *args):
        key = f"{country}/{category}/{table}"
        if key not in produced:
            return None
        return ([{**ROW, "country": country, "category": category, "table": table}], {key: {"models": ["naive1"]}})

    monkeypatch.setattr(gwf, "_series_forecast", series_forecast)
    monkeypatch.setattr(
        gwf,
        "_project_rows",
        lambda rows: (rows, {"cone_violations_pre": 0, "cone_violations_post": 0, "cone_violations_detail": {}}),
    )

    def append_log(rows, **kwargs):
        path = prospective / "forecast_log.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        return path

    monkeypatch.setattr(gwf, "_append_log", append_log)
    monkeypatch.setattr(gwf.ledger, "validate", lambda frame: [])
    monkeypatch.setattr(gwf.ledger, "load_completeness_allowlist", lambda: {})
    gwf.run()


def test_structurally_short_series_is_not_required_from_the_model_run(monkeypatch, tmp_path) -> None:
    """RED del run 33460926423: catálogo != universo modelable."""

    long_key = "mexico/F1/FAD"
    _fake_run(
        monkeypatch,
        tmp_path,
        current={"F1": 100, "F2": 10},
        produced={long_key},
        previous={long_key},
    )


def test_previous_eligible_key_cannot_disappear_even_if_current_count_is_unchanged(monkeypatch, tmp_path) -> None:
    """Un parser que cambia A por B no puede pasar gracias a que ambos conjuntos miden uno."""

    with pytest.raises(SystemExit, match="contracci.n"):
        _fake_run(
            monkeypatch,
            tmp_path,
            current={"F2": 100},
            produced={"mexico/F2/FAD"},
            previous={"mexico/F1/FAD"},
        )


def test_eligible_model_failure_remains_fail_closed(monkeypatch, tmp_path) -> None:
    key = "mexico/F1/FAD"
    with pytest.raises(SystemExit, match="AUSENTE|tabla completa"):
        _fake_run(monkeypatch, tmp_path, current={"F1": 100}, produced=set(), previous={key})


def test_linalg_failure_retries_once_with_governed_sarima_stabilization(monkeypatch) -> None:
    calls: list[bool] = []

    def compute(*args, relaxed_sarima=False, **kwargs):
        calls.append(relaxed_sarima)
        if not relaxed_sarima:
            raise np.linalg.LinAlgError("LU decomposition error.")
        return ([ROW], {"mexico/F1/FAD": {"models": ["theta", "ets", "sarima"]}})

    monkeypatch.setattr(gwf, "_compute_series_forecast", compute)
    out = gwf._series_forecast(
        "mexico",
        "F1",
        "FAD",
        None,
        {"FAD": ("theta", "ets", "sarima")},
        None,
        {"FAD": 0.05},
        {},
    )
    assert out is not None and calls == [False, True]
    assert out[1]["mexico/F1/FAD"]["numerical_stabilization"] == "sarima_relaxed_stationarity_invertibility"


def test_non_numerical_model_failure_is_not_retried(monkeypatch) -> None:
    calls = 0

    def compute(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("modelo roto")

    monkeypatch.setattr(gwf, "_compute_series_forecast", compute)
    assert gwf._series_forecast("mexico", "F1", "FAD", None, {"FAD": ("sarima",)}, None, {"FAD": 0.05}, {}) is None
    assert calls == 1


def test_relaxed_retry_failure_is_not_retried_a_third_time(monkeypatch) -> None:
    """El escape numérico es uno solo y sigue fallando cerrado si no estabiliza."""

    calls: list[bool] = []

    def compute(*args, relaxed_sarima=False, **kwargs):
        calls.append(relaxed_sarima)
        raise np.linalg.LinAlgError("singular en ambos intentos")

    monkeypatch.setattr(gwf, "_compute_series_forecast", compute)
    assert gwf._series_forecast("mexico", "F1", "FAD", None, {"FAD": ("sarima",)}, None, {"FAD": 0.05}, {}) is None
    assert calls == [False, True]


def test_get_logger_returns_visible_child_and_does_not_duplicate_handlers(caplog) -> None:
    first = config.get_logger("web_forecasts")
    second = config.get_logger("web_forecasts")
    qualified = config.get_logger("vp_model.walkforward")
    assert first is second
    assert first.name == "vp_model.web_forecasts"
    assert qualified.name == "vp_model.walkforward"
    assert first.getEffectiveLevel() <= logging.INFO
    handlers = [h for h in logging.getLogger("vp_model").handlers if getattr(h, "_vp_model", False)]
    assert len(handlers) == 1
    with caplog.at_level(logging.INFO, logger="vp_model.web_forecasts"):
        first.info("visible-a73-r2")
    assert "visible-a73-r2" in caplog.text


def test_eligibility_snapshot_roundtrips_and_detects_digest_tamper(tmp_path) -> None:
    structural = {
        "mexico/F2/FAD": gwf.SeriesEligibility(
            key="mexico/F2/FAD",
            table="FAD",
            n_obs=10,
            min_required=90,
            eligible=False,
            reason="too_short",
        )
    }
    payload = gwf._eligibility_payload(
        {"mexico/F1/FAD", "mexico/F2/FAD"},
        {"mexico/F1/FAD"},
        structural,
    )
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"series": {"mexico/F1/FAD": {}}, "eligibility": payload}))
    previous_catalogue, previous_eligible = gwf._previous_universe(meta)
    assert previous_catalogue["FAD"] == {"mexico/F1/FAD", "mexico/F2/FAD"}
    assert previous_eligible["FAD"] == {"mexico/F1/FAD"}
    assert payload["structural_ineligible"]["FAD"] == {"count": 1, "reasons": {"too_short": 1}}

    payload["eligible"]["FAD"]["sha256"] = "0" * 64
    meta.write_text(json.dumps({"series": {}, "eligibility": payload}))
    with pytest.raises(ValueError, match="sha256"):
        gwf._previous_universe(meta)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update({"extra": {}}), "schema abierto"),
        (lambda payload: payload["eligible"]["FAD"].update({"count": True}), "count no coincide"),
        (lambda payload: payload["criterion"].update({"FAD": True}), "criterion.FAD"),
    ],
)
def test_eligibility_snapshot_rejects_open_schema_and_boolean_integers(tmp_path, mutation, message) -> None:
    """El baseline que protege contracciones no acepta extensiones ni bool como int."""

    item = gwf.SeriesEligibility(
        key="mexico/F2/FAD",
        table="FAD",
        n_obs=10,
        min_required=90,
        eligible=False,
        reason="too_short",
    )
    payload = gwf._eligibility_payload(
        {"mexico/F1/FAD", "mexico/F2/FAD"},
        {"mexico/F1/FAD"},
        {item.key: item},
    )
    mutation(payload)
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"series": {}, "eligibility": payload}))
    with pytest.raises(ValueError, match=message):
        gwf._previous_universe(meta)


def test_universe_floor_and_previous_set_are_independent_guards(monkeypatch) -> None:
    monkeypatch.setattr(gwf.config, "TABLES", ("FAD",))
    monkeypatch.setattr(gwf, "BOOTSTRAP_MIN_CATALOG", {"FAD": 2})
    monkeypatch.setattr(gwf, "BOOTSTRAP_MIN_ELIGIBLE", {"FAD": 1})
    problems = gwf._universe_problems(
        {"mexico/F2/FAD"},
        {"mexico/F2/FAD"},
        {"FAD": {"mexico/F1/FAD"}},
        {"FAD": {"mexico/F1/FAD"}},
    )
    assert any("catálogo 1 < piso" in problem for problem in problems)
    assert any("contracción del catálogo" in problem for problem in problems)
    assert any("contracción del universo elegible" in problem for problem in problems)
