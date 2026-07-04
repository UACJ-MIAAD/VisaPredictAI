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

import freeze_snapshots  # noqa: E402


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
}


def test_ledger_is_append_only_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(gwf, "REPORTS", tmp_path)
    gwf._append_log([ROW])
    gwf._append_log([{**ROW, "days": 999}])  # re-run con otro valor: NO debe sobrescribir (C3)
    led = pd.read_csv(tmp_path / "forecast_log.csv")
    assert len(led) == 1 and led.days.iloc[0] == 100
    gwf._append_log([{**ROW, "date": "2026-09-01", "h": 2}])  # fecha nueva sí se anexa
    assert len(pd.read_csv(tmp_path / "forecast_log.csv")) == 2


def test_ensemble_point_is_elementwise_median():
    import numpy as np

    out = gwf._ensemble_point([np.array([1.0, 10.0]), np.array([3.0, 30.0]), np.array([2.0, 20.0])])
    assert list(out) == [2.0, 20.0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
