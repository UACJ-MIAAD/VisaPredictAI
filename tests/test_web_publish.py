"""Tests de las piezas que corren DESATENDIDAS y publican (C4).

La mitad *freeze* que vivía aquí (validador/contrato de ``freeze_snapshots``)
migró a ``tests/test_freeze_snapshots.py`` con la costura A1 (Fetcher
inyectado); queda la lógica pura de ``generate_web_forecasts`` (ledger
inmutable + ensamble + cono). Sin red y sin BD: fixtures sintéticos.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))


# ---------------------------------------------------------- generate_web_forecasts
darts = pytest.importorskip("darts")  # la lógica vive junto a la capa de modelado
import generate_web_forecasts as gwf  # noqa: E402

ROW = {
    "origin": "2026-07",
    "h": 1,
    "country": "mexico",
    "category": "F1",
    "table": "FAD",
    "date": "2026-08-01",
    "days": 100,
    "lo80": 90,
    "hi80": 110,
    "lo95": 80,
    "hi95": 120,
    "band_method": "q_h",  # era tag (audit 4-jul): the ledger records how bands grew
}


def test_ledger_is_append_only_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(gwf, "REPORTS", tmp_path)
    gwf._append_log([ROW])
    gwf._append_log([{**ROW, "days": 999}])  # re-run con otro valor: NO debe sobrescribir (C3)
    led = pd.read_csv(tmp_path / "prospective" / "forecast_log.csv")
    assert len(led) == 1 and led.days.iloc[0] == 100
    gwf._append_log([{**ROW, "date": "2026-09-01", "h": 2}])  # fecha nueva sí se anexa
    assert len(pd.read_csv(tmp_path / "prospective" / "forecast_log.csv")) == 2


def test_ensemble_point_is_elementwise_median():
    import numpy as np

    out = gwf._ensemble_point([np.array([1.0, 10.0]), np.array([3.0, 30.0]), np.array([2.0, 20.0])])
    assert list(out) == [2.0, 20.0]


# ------------------------------------------- cone projection at publish time (AL5/F1)
def _vintage_rows(days: dict[tuple[str, str], int]) -> list[dict]:
    """Una añada mínima (bandas consistentes ±50/±100 alrededor del punto)."""
    return [
        {
            "origin": "2026-07",
            "h": 1,
            "country": country,
            "category": "F1",
            "table": table,
            "date": "2026-08-01",
            "days": d,
            "lo80": d - 50,
            "hi80": d + 50,
            "lo95": d - 100,
            "hi95": d + 100,
            "band_method": "q_h",
        }
        for (country, table), d in days.items()
    ]


def test_publisher_projects_seeded_fad_dff_violation_to_zero():
    from vp_model import cone

    rows = _vintage_rows(
        {
            ("mexico", "FAD"): 12_000,  # sembrada: FAD > DFF
            ("mexico", "DFF"): 11_900,
            ("all_chargeability", "FAD"): 15_000,
            ("all_chargeability", "DFF"): 15_500,
        }
    )
    out, counters = gwf._project_rows(rows)
    assert counters["cone_violations_pre"] == 1 and counters["cone_violations_post"] == 0
    frame = pd.DataFrame(out)
    assert cone.count_fad_dff_violations(frame) == 0 and cone.count_country_violations(frame) == 0
    mx = frame[frame["country"] == "mexico"].set_index("table")
    assert mx.loc["FAD", "days"] == 11_900 and mx.loc["DFF", "days"] == 12_000  # min/max del par
    # bandas desplazadas con el punto: el ancho calibrado se preserva y el output sigue entero
    assert ((frame["hi95"] - frame["lo95"]) == 200).all() and ((frame["hi80"] - frame["lo80"]) == 100).all()
    assert all(isinstance(r["days"], int) for r in out)


def test_publisher_projects_seeded_country_violation_to_zero():
    from vp_model import cone

    rows = _vintage_rows(
        {
            ("china", "FAD"): 15_400,  # sembrada: país > all_chargeability
            ("all_chargeability", "FAD"): 15_000,
        }
    )
    out, counters = gwf._project_rows(rows)
    assert counters["cone_violations_pre"] == 1 and counters["cone_violations_post"] == 0
    frame = pd.DataFrame(out)
    assert cone.count_country_violations(frame) == 0
    cn = frame[frame["country"] == "china"].iloc[0]
    assert cn["days"] == 15_000 and cn["hi95"] - cn["lo95"] == 200  # recortado a la referencia


def test_publisher_meta_exposes_cone_counters():
    _, counters = gwf._project_rows(_vintage_rows({("mexico", "FAD"): 12_000, ("mexico", "DFF"): 11_900}))
    meta = gwf._meta_payload({"FAD": "ETS"}, {"mexico/F1/FAD": {"mase": 0.1}}, counters)
    assert meta["cone_violations_pre"] == 1 and meta["cone_violations_post"] == 0
    assert meta["cone_violations_detail"]["fad_le_dff"]["pre"] == 1
    assert "cone_policy" in meta and meta["n_series"] == 1


def test_publisher_without_violations_is_byte_stable_passthrough():
    rows = _vintage_rows(
        {
            ("mexico", "FAD"): 11_000,
            ("mexico", "DFF"): 11_500,
            ("all_chargeability", "FAD"): 15_000,
            ("all_chargeability", "DFF"): 15_500,
        }
    )
    out, counters = gwf._project_rows(rows)
    assert counters["cone_violations_pre"] == 0 and counters["cone_violations_post"] == 0
    assert out == rows  # mismas filas, mismos valores/tipos
    # y el CSV que sirve la web es byte-idéntico al que saldría sin proyección
    assert pd.DataFrame(out)[gwf.WEB_COLS].to_csv(index=False) == pd.DataFrame(rows)[gwf.WEB_COLS].to_csv(index=False)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
