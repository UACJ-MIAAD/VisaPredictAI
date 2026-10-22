"""The shadow uses the same bounded numerical retry as deployed SARIMA."""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("xgboost")
pytest.importorskip("darts")

from experiments import freeze_shadow as shadow
from vp_model.champion import Recipe


class ReachedRelaxedModel(Exception):
    pass


def test_lu_failure_reaches_the_governed_second_factory(monkeypatch):
    series = pd.Series(np.arange(160), index=pd.date_range("2010-01-01", periods=160, freq="MS"))
    monkeypatch.setattr(shadow.dataset, "load_series", lambda *args: series)
    calls = []

    class Model:
        def __init__(self, relaxed):
            self.relaxed = relaxed

        def historical_forecasts(self, *args, **kwargs):
            calls.append(self.relaxed)
            if self.relaxed:
                raise ReachedRelaxedModel
            raise np.linalg.LinAlgError("LU decomposition error")

    monkeypatch.setattr(shadow.models, "build_model", lambda name: Model(False))
    monkeypatch.setattr(shadow.models, "build_relaxed_sarima", lambda: Model(True))
    with pytest.raises(ReachedRelaxedModel):
        shadow._series_shadow(Recipe(("sarima",)), "mexico", "EB3_OW", "DFF")
    assert calls == [False, True]


@pytest.mark.parametrize(
    "error,recipe", [(ValueError("invalid data"), ("sarima",)), (np.linalg.LinAlgError("LU"), ("ets",))]
)
def test_unrelated_failures_do_not_retry(monkeypatch, error, recipe):
    series = pd.Series(np.arange(160), index=pd.date_range("2010-01-01", periods=160, freq="MS"))
    monkeypatch.setattr(shadow.dataset, "load_series", lambda *args: series)
    calls = []

    class Model:
        def historical_forecasts(self, *args, **kwargs):
            calls.append(1)
            raise error

    monkeypatch.setattr(shadow.models, "build_model", lambda name: Model())
    monkeypatch.setattr(shadow.models, "build_relaxed_sarima", lambda: pytest.fail("unexpected numerical retry"))
    with pytest.raises(type(error), match=str(error)):
        shadow._series_shadow(Recipe(recipe), "mexico", "EB3_OW", "DFF")
    assert calls == [1]


def test_second_failure_is_not_retried_indefinitely(monkeypatch):
    calls = []

    def compute(*args, **kwargs):
        calls.append(kwargs.get("relaxed_sarima", False))
        raise np.linalg.LinAlgError("still invalid")

    monkeypatch.setattr(shadow, "_compute_series_shadow", compute)
    with pytest.raises(np.linalg.LinAlgError, match="still invalid"):
        shadow._series_shadow(Recipe(("sarima",)), "mexico", "EB3_OW", "DFF")
    assert calls == [False, True]


@pytest.mark.parametrize("retry", [False, True])
def test_success_keeps_rows_and_records_only_an_actual_retry(monkeypatch, retry):
    calls = []

    def compute(*args, **kwargs):
        calls.append(kwargs.get("relaxed_sarima", False))
        if retry and not kwargs.get("relaxed_sarima"):
            raise np.linalg.LinAlgError("LU")
        return [{"days": 123}]

    monkeypatch.setattr(shadow, "_compute_series_shadow", compute)
    rows = shadow._series_shadow(Recipe(("sarima",)), "mexico", "EB3_OW", "DFF")
    assert rows[0]["days"] == 123
    assert ("numerical_stabilization" in rows[0]) is retry
    assert calls == ([False, True] if retry else [False])
