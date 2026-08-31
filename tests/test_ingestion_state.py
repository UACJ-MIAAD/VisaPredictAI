"""``vp_data.ingestion_state`` (D3, schema v1): el estado de la fuente que
comparten cron, SES y watchdog.

Contrato: schema CERRADO (exactamente las 10 llaves, enums y formatos
validados fail-closed), escritura ATÓMICA y solo ante CAMBIO SEMÁNTICO del
payload (idéntico = no-op; sin ``last_attempt`` ni timestamps por corrida =
sin churn diario), derivación pura con reloj inyectado. Feed AUSENTE = nada
registrado (los consumidores degradan); feed PRESENTE e inválido = fallo real
(raise, bytes preservados).
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from vp_data import ingestion_state as ist


def _derive(**over):
    base = dict(
        status="ok",
        reason="",
        n_index_links=298,
        failed_links=[],
        panel_vintage="2026-07",
        today=date(2026, 8, 31),
        previous=None,
    )
    base.update(over)
    return ist.derive_state(**base)


# ------------------------------------------------------------------ derivación
def test_derived_state_is_schema_1_closed_and_valid():
    s = _derive()
    assert s["schema"] == 1
    assert set(s) == set(ist.KEYS)  # cerrado: ni una llave más, ni una menos
    assert "last_attempt" not in s
    assert ist.validate(s) == []


def test_expected_month_cutoff_policy_is_day_15():
    # política EXPLÍCITA de corte: el boletín del mes M+1 se publica a mediados
    # de M ⇒ desde el día 15 se espera YA el del mes siguiente (antes, el del
    # mes corriente — regla que subestimaba el atraso a fin de mes)
    assert _derive(today=date(2026, 8, 14))["expected_month"] == "2026-08"
    assert _derive(today=date(2026, 8, 15))["expected_month"] == "2026-09"
    assert _derive(today=date(2026, 8, 31))["expected_month"] == "2026-09"  # ago→sep
    assert _derive(today=date(2026, 12, 14))["expected_month"] == "2026-12"
    assert _derive(today=date(2026, 12, 20))["expected_month"] == "2027-01"  # dic→ene


def test_missing_months_span_panel_to_expected_across_year_boundary():
    s = _derive(panel_vintage="2025-11", today=date(2026, 2, 10))
    assert s["missing_months"] == ["2025-12", "2026-01", "2026-02"]
    # 31-ago con panel 2026-07: ya faltan agosto Y septiembre (fallo cazado por el autor)
    assert _derive(panel_vintage="2026-07", today=date(2026, 8, 31))["missing_months"] == ["2026-08", "2026-09"]
    assert _derive(panel_vintage="2026-08", today=date(2026, 8, 14))["missing_months"] == []
    assert _derive(panel_vintage=None)["missing_months"] == []  # panel desconocido: no inventar


def test_status_since_carries_within_a_streak_and_resets_on_transition():
    first = _derive(status="blocked", reason="HTTP 403 tras el WAF", today=date(2026, 8, 10))
    assert first["status_since"] == "2026-08-10"
    again = _derive(status="blocked", reason="HTTP 403 tras el WAF", today=date(2026, 8, 31), previous=first)
    assert again["status_since"] == "2026-08-10"  # misma racha: se conserva
    back = _derive(status="ok", today=date(2026, 9, 2), previous=again)
    assert back["status_since"] == "2026-09-02"  # transición: se reinicia


def test_last_success_date_marks_the_start_of_the_ok_streak():
    ok = _derive(today=date(2026, 7, 15))
    assert ok["last_success_date"] == "2026-07-15"
    still_ok = _derive(today=date(2026, 8, 1), previous=ok)
    assert still_ok["last_success_date"] == "2026-07-15"  # racha viva: sin churn diario
    blocked = _derive(status="blocked", reason="x", today=date(2026, 8, 10), previous=still_ok)
    assert blocked["last_success_date"] == "2026-07-15"  # se conserva durante el bloqueo


# ------------------------------------------------------------------ validación
def test_validate_rejects_open_schema_bad_enum_and_bad_formats():
    good = _derive()
    assert ist.validate({**good, "last_attempt": "2026-08-31"})  # llave extra: schema CERRADO
    incomplete = dict(good)
    incomplete.pop("reason")
    assert ist.validate(incomplete)
    assert ist.validate({**good, "status": "degraded"})  # fuera del enum
    assert ist.validate({**good, "expected_month": "08-2026"})
    assert ist.validate({**good, "status_since": "31/08/2026"})
    assert ist.validate({**good, "missing_months": "2026-08"})  # debe ser lista
    assert ist.validate({**good, "schema": 2})
    # bool es subclase de int en Python: True NO puede colarse como entero
    assert ist.validate({**good, "n_index_links": True})
    assert ist.validate({**good, "schema": True})
    assert ist.validate({**good, "failed_links": ["a.html", 3]})  # solo strings


# ---------------------------------------------------------- escritura/lectura
def test_write_only_on_transition_and_atomically(tmp_path):
    path = tmp_path / "governance" / "ingestion_state.json"
    s = _derive(status="blocked", reason="HTTP 403 tras el WAF")
    assert ist.write_if_transition(s, path) is True
    first_bytes = path.read_bytes()
    assert ist.write_if_transition(dict(s), path) is False  # payload idéntico: no-op
    assert path.read_bytes() == first_bytes
    assert not list(path.parent.glob("*.part"))  # atómico: sin restos
    s2 = _derive(status="ok", previous=s)
    assert ist.write_if_transition(s2, path) is True
    assert json.loads(path.read_text())["status"] == "ok"


def test_write_refuses_an_invalid_state_and_leaves_the_file_alone(tmp_path):
    path = tmp_path / "ingestion_state.json"
    ist.write_if_transition(_derive(), path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ist.write_if_transition({**_derive(), "status": "degraded"}, path)
    assert path.read_bytes() == before


def test_read_state_distinguishes_absent_from_present_but_invalid(tmp_path):
    # ausente = nada registrado (None); PRESENTE e inválido = fallo real (raise):
    # jamás convertir corrupción en "no hay estado" y dejar que alguien lo pise
    assert ist.read_state(tmp_path / "nope.json") is None
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{truncado")
    with pytest.raises(ValueError, match="ilegible"):
        ist.read_state(corrupt)
    nondict = tmp_path / "nd.json"
    nondict.write_text("[1, 2]")
    with pytest.raises(ValueError):
        ist.read_state(nondict)
    invalid = tmp_path / "inv.json"
    invalid.write_text(json.dumps({**_derive(), "last_attempt": "2026-08-31"}))
    with pytest.raises(ValueError, match="contrato"):
        ist.read_state(invalid)


def test_writer_never_overwrites_a_corrupt_feed_and_preserves_its_bytes(tmp_path):
    path = tmp_path / "ingestion_state.json"
    path.write_text("{truncado por un kill a mitad de escritura")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ist.write_if_transition(_derive(), path)
    assert path.read_bytes() == before  # evidencia forense intacta


def test_panel_vintage_scans_the_csv_without_pandas(tmp_path):
    csv = tmp_path / "panel.csv"
    csv.write_text("country,bulletin_date,value\nmx,2026-06-01,1\nmx,2026-07-01,2\nmx,2025-12-01,3\n")
    assert ist.panel_vintage(csv) == "2026-07"
    assert ist.panel_vintage(tmp_path / "nope.csv") is None


# ---------------------------------------------------------------- línea de SES
def test_summary_line_variants_are_honest_and_actionable():
    assert "sin estado" in ist.summary_line(None)
    ok = _derive()
    assert "OK" in ist.summary_line(ok) and "2026-07" in ist.summary_line(ok)
    lagging = _derive(panel_vintage="2026-07", today=date(2026, 8, 31))
    blocked = _derive(status="blocked", reason="HTTP 403 tras el WAF", panel_vintage="2026-07", today=date(2026, 8, 31))
    line = ist.summary_line(blocked)
    assert "BLOQUEADA" in line and "HTTP 403" in line and "2026-08" in line
    assert "make ingest-manual" in line  # la acción humana viaja en la señal
    offline = _derive(status="offline", reason="error de red: reset")
    assert "SIN RED" in ist.summary_line(offline)
    partial = _derive(status="partial", failed_links=["a.html", "b.html"])
    assert "2" in ist.summary_line(partial)
    assert lagging["missing_months"] == ["2026-08", "2026-09"]  # corte día 15: sep ya se espera


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
