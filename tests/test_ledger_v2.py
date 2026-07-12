"""Contrato del ledger v2 (A2, plan auditoría 2026-07-11).

Cubre la aceptación del paquete: append-only e idempotente; ``live`` exige target
desconocido al freeze; una receta nueva no colisiona con una añada ya congelada;
reintentos son no-op; y ``validate`` caza manipulación temporal y de contenido.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vp_model import ledger  # noqa: E402

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
    "band_method": "q_h",
}
# Identidad fija para tests deterministas (producción la deriva del estado real).
STAMP = {
    "vintage": "2026-07",
    "phash": "abc123def456",
    "sha": "deadbeef0000",
    "frozen_at": "2026-07-11T12:00:00+00:00",
    "deployment": "rel-test",
}


def _stamp(rows, mv="median(theta+ets+sarima)", **kw):
    return ledger.stamp_rows(rows, mv, **{**STAMP, **kw})


def test_stamp_live_only_when_target_unknown_at_freeze() -> None:
    live = _stamp([dict(ROW)])[0]
    assert live["evaluation_mode"] == "live"  # target 2026-08 > vintage 2026-07
    past = _stamp([{**ROW, "date": "2026-07-01"}])[0]
    assert past["evaluation_mode"] == "backfill"  # target ya publicado al freeze
    same = _stamp([{**ROW, "date": "2026-07-15"}])[0]
    assert same["evaluation_mode"] == "backfill"  # mismo mes que el vintage = conocido


def test_stamp_as_of_forces_backfill_even_for_future_targets() -> None:
    r = _stamp([dict(ROW)], as_of="2025-07")[0]
    assert r["evaluation_mode"] == "backfill"


def test_stamp_carries_deployment_id() -> None:
    """B1: cada fila nueva registra bajo qué release corrió el congelador."""
    r = _stamp([dict(ROW)])[0]
    assert r["deployment_id"] == "rel-test"


def test_stamp_carries_pipeline_run_id(monkeypatch) -> None:
    """C3: identidad jerárquica — la fila queda enlazada al run del pipeline."""
    monkeypatch.delenv("VP_PIPELINE_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert _stamp([dict(ROW)])[0]["pipeline_run_id"] == "local"
    monkeypatch.setenv("VP_PIPELINE_RUN_ID", "12345")
    assert _stamp([dict(ROW)])[0]["pipeline_run_id"] == "12345"


def test_stamp_model_version_dict_and_row_recipe() -> None:
    by_table = _stamp([dict(ROW)], mv={"FAD": "median(theta+ets+sarima)", "DFF": "sarima"})[0]
    assert by_table["model_version"] == "median(theta+ets+sarima)"
    shadow = _stamp([{**ROW, "recipe": "naive1"}], mv=None)[0]
    assert shadow["model_version"] == "naive1"


def test_forecast_id_is_deterministic_and_recipe_aware() -> None:
    a = _stamp([dict(ROW)])[0]
    b = _stamp([dict(ROW)])[0]
    assert a["forecast_id"] == b["forecast_id"]
    c = _stamp([dict(ROW)], mv="sarima")[0]
    assert c["forecast_id"] != a["forecast_id"]


def test_append_is_idempotent_and_immutable(tmp_path) -> None:
    path = tmp_path / "forecast_log.csv"
    ledger.append(path, _stamp([dict(ROW)]))
    # Reintento con otra identidad (re-run del cron: otro frozen_at/sha) — no-op.
    retry = _stamp([{**ROW, "days": 999}], frozen_at="2026-07-12T00:00:00+00:00", sha="cafecafe0000")
    ledger.append(path, retry)
    df = pd.read_csv(path)
    assert len(df) == 1
    assert df.days.iloc[0] == 100  # la fila congelada nunca se reescribe (C3)
    assert df.frozen_at.iloc[0] == "2026-07-11T12:00:00+00:00"  # identidad original intacta


def test_new_recipe_does_not_collide_with_frozen_vintage(tmp_path) -> None:
    path = tmp_path / "forecast_log.csv"
    ledger.append(path, _stamp([dict(ROW)]))
    # Cambio de receta sobre la MISMA añada: no reemplaza ni mezcla (no-op)...
    ledger.append(path, _stamp([dict(ROW)], mv="sarima"))
    df = pd.read_csv(path)
    assert len(df) == 1 and df.model_version.iloc[0] == "median(theta+ets+sarima)"
    # ...y estrena limpiamente en la añada siguiente.
    nxt = _stamp([{**ROW, "origin": "2026-08", "date": "2026-09-01"}], mv="sarima", vintage="2026-08")
    ledger.append(path, nxt)
    df = pd.read_csv(path)
    assert len(df) == 2 and set(df.model_version) == {"median(theta+ets+sarima)", "sarima"}


def test_validate_catches_temporal_manipulation() -> None:
    rows = _stamp([dict(ROW)])
    rows[0]["evaluation_mode"] = "live"
    rows[0]["date"] = "2026-06-01"  # target anterior al vintage pero etiquetado live
    rows[0]["forecast_id"] = ledger.forecast_id(rows[0])
    problems = ledger.validate(pd.DataFrame(rows))
    assert any("live" in p for p in problems)


def test_validate_catches_content_tampering() -> None:
    rows = _stamp([dict(ROW)])
    rows[0]["model_version"] = "otra-receta"  # se alteró tras el sello
    problems = ledger.validate(pd.DataFrame(rows))
    assert any("forecast_id" in p for p in problems)


def test_validate_catches_payload_mutation() -> None:
    """Auditoria 11-jul: mutar days/bandas en una fila congelada pasaba validate() == []
    (forecast_id solo cubre clave+receta). row_hash lo caza ahora."""
    rows = _stamp([dict(ROW), {**ROW, "date": "2026-09-01", "h": 2}])
    df = pd.DataFrame(rows)
    assert ledger.validate(df) == []
    df.loc[0, "days"] = 999999
    df.loc[1, "lo95"] = -999
    problems = ledger.validate(df)
    assert any("row_hash" in p for p in problems)


def test_row_hash_survives_csv_roundtrip(tmp_path) -> None:
    """La forma canonica de _norm_payload: un int sellado que el CSV relee como float
    entero (columna con NaN) debe re-derivar el MISMO hash."""
    r = {**ROW, "days": 18000, "lo95": float("nan")}
    rows = _stamp([r])
    path = tmp_path / "log.csv"
    ledger.append(path, rows)
    back = pd.read_csv(path)
    assert ledger.validate(back) == []
    assert ledger.row_hash(back.iloc[0].to_dict()) == rows[0]["row_hash"]


def test_validate_clean_ledger_passes(tmp_path) -> None:
    path = tmp_path / "forecast_log.csv"
    ledger.append(path, _stamp([dict(ROW), {**ROW, "date": "2026-09-01", "h": 2}]))
    assert ledger.validate(pd.read_csv(path)) == []


def test_real_ledgers_pass_v2_contract() -> None:
    """Integración: los ledgers migrados del repo cumplen el contrato v2 completo."""
    for name in ("forecast_log.csv", "forecast_log_shadow.csv"):
        path = ledger.ROOT / "reports" / "prospective" / name
        if not path.exists():
            continue
        df = pd.read_csv(path)
        assert ledger.validate(df) == [], f"{name} viola el contrato v2"
        assert df["frozen_at"].notna().all(), f"{name}: filas sin acta de nacimiento"
        assert set(df["evaluation_mode"].unique()) <= {"live", "backfill"}
