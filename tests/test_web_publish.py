"""Tests de las piezas que corren DESATENDIDAS y publican (C4).

Cubre lo que el audit encontró sin cobertura: el validador/contrato de
``freeze_snapshots`` (la única pieza con red, cuyo stdout gobierna el gate del cron)
y la lógica pura de ``generate_web_forecasts`` (ledger inmutable + ensamble).
Sin red y sin BD: fixtures sintéticos + monkeypatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

from pipeline import freeze_snapshots  # noqa: E402


# ---------------------------------------------------------------- freeze_snapshots
def test_looks_like_bulletin_accepts_real_and_rejects_junk():
    real = b"<html><h1>Visa Bulletin for July 2026</h1><table>All Chargeability Areas</table></html>"
    assert freeze_snapshots._looks_like_bulletin(real)
    # WAF/maintenance: sin 'chargeability' aunque el template del sitio diga Visa Bulletin
    assert not freeze_snapshots._looks_like_bulletin(b"<html><title>Access Denied</title>Reference #18</html>")
    assert not freeze_snapshots._looks_like_bulletin(b"<nav>Visa Bulletin</nav><h1>Page not found</h1>")
    # bytes no-UTF8 no deben explotar (errors='replace')
    assert not freeze_snapshots._looks_like_bulletin(b"\xff\xfe garbage")


def test_fetch_bytes_retries_invalid_content_then_raises(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        content = b"<html>maintenance</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        freeze_snapshots.requests, "get", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or FakeResp()
    )
    monkeypatch.setattr(freeze_snapshots.time, "sleep", lambda *_: None)
    with pytest.raises(ValueError, match="sin marcadores"):
        freeze_snapshots.fetch_bytes("http://x/visa-bulletin-for-july-2026.html")
    assert calls["n"] == freeze_snapshots.MAX_RETRIES  # reintenta como transitorio


def test_main_aborts_on_starved_index(monkeypatch, capsys):
    # A4: un índice con menos links que el piso conocido = markup cambiado → abortar
    monkeypatch.setattr(freeze_snapshots, "extract_month_links", lambda: ["only-one.html"])
    with pytest.raises(SystemExit):
        freeze_snapshots.main()


def test_main_stdout_contract_is_last_line_int(monkeypatch, tmp_path, capsys):
    # el gate del cron lee `tail -1` del stdout: DEBE ser un entero
    links = [f"visa-bulletin-for-month-{i}.html" for i in range(freeze_snapshots.MIN_INDEX_LINKS)]
    snap = tmp_path / "snapshots"
    snap.mkdir()
    for name in links:  # todos ya congelados → new=0, sin red
        (snap / name).write_bytes(b"x")
    monkeypatch.setattr(freeze_snapshots, "SNAP_DIR", snap)
    monkeypatch.setattr(freeze_snapshots, "extract_month_links", lambda: links)
    freeze_snapshots.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "0"


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
