"""Contrato de la evaluación prospectiva (experiments/score_forecasts).

Prueba la lógica PURA de scoring (`_score_rows`) con datos sintéticos — sin BD ni
modelos — para garantizar que el conteo de pendientes, la cobertura 80/95 % y el
error escalado (MASE) se calculan bien. Es la base de toda la medición real, así que
no puede quedar sin test.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from vp_data import tracking as base_tracking

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # para que score_forecasts resuelva `import tracking` (módulo raíz)
_spec = importlib.util.spec_from_file_location("score_forecasts", ROOT / "experiments" / "score_forecasts.py")
assert _spec is not None and _spec.loader is not None  # narrow para mypy + falla claro si no carga
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)


def test_demo_selfcheck() -> None:
    sf.demo()  # asserts internos: pendiente, cobertura 80/95, MASE


def test_pending_when_target_not_realized() -> None:
    fc = pd.DataFrame(
        [
            {
                "origin": "2024-01",
                "h": 1,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2099-01-01",
                "days": 1000,
                "lo80": 950,
                "hi80": 1050,
                "lo95": 900,
                "hi95": 1100,
            }
        ]
    )
    scored, pending = sf._score_rows(fc, {}, lambda *_: 100.0)
    assert scored == [] and pending == 1


def test_coverage_and_scaled_error() -> None:
    fc = pd.DataFrame(
        [
            {
                "origin": "2024-01",
                "h": 1,
                "country": "mexico",
                "category": "F1",
                "table": "FAD",
                "date": "2024-02-01",
                "days": 1000,
                "lo80": 990,
                "hi80": 1010,
                "lo95": 900,
                "hi95": 1100,
            }
        ]
    )
    # real = 1060 → fuera de [990,1010] (in80=0) pero dentro de [900,1100] (in95=1); |error|=60, escala=120 → MASE 0.5
    scored, pending = sf._score_rows(fc, {("mexico", "F1", "FAD", "2024-02-01"): 1060.0}, lambda *_: 120.0)
    assert pending == 0 and len(scored) == 1
    s = scored[0]
    assert s["abs_err"] == 60 and s["in80"] == 0 and s["in95"] == 1
    assert abs(s["scaled_err"] - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# D7 · instrumentación mensual de la corrida completa (``run``)
#
# Contrato: UN registro ``web_forecast_scoring`` por invocación (nunca uno global
# más uno por horizonte), emitido también cuando no hay nada puntuado, cuando falta
# el ledger y cuando el scoring falla; un marcador de resumen de UNA línea siempre;
# y salidas byte-idénticas a las de la rama base con el mismo fixture.
# ---------------------------------------------------------------------------

MARKER_ENV = "VP_SCORING_SUMMARY"

OUTPUTS = (
    "forecast_scorecard.csv",
    "forecast_scorecard_meta.json",
    "forecast_scorecard_shadow.csv",
    "forecast_scorecard_shadow_meta.json",
    "prospective_head_to_head.json",
)

# Maestro dorado: sha256 de las cinco salidas que la rama BASE produce con el fixture
# controlado de abajo. La instrumentación observa — no puede mover un byte de lo que
# el pipeline publica. Regenerar SOLO si cambia deliberadamente el contenido publicado.
BASELINE_OUTPUT_SHA256 = {
    "forecast_scorecard.csv": "9877b5f7b5d60050009817270d37aef6eeda65bc06171fdfc19357fc6aa5c744",
    "forecast_scorecard_meta.json": "a9f6cd93e3d313d023288b039228399f2b2ba4193a60b646a38e63e067069168",
    "forecast_scorecard_shadow.csv": "3f451b734884a8deb9b5109d6d872d6ec25e92cf398a49fd5c3cce79a631077c",
    "forecast_scorecard_shadow_meta.json": "42481258b74ed6cea02396fbdb17ad86c72254401ca22495b75587c0d1109d28",
    "prospective_head_to_head.json": "5f408272e4a104a0604807b0062cd402d311ede9ea25301ea4ce0d2763e72850",
}

_BANDS = {"lo80": 950, "hi80": 1050, "lo95": 900, "hi95": 1100}

# (origin, h, country, category, table, date, days, evaluation_mode)
_CHAMP_ROWS = [
    ("2024-01", 1, "mexico", "F1", "FAD", "2024-02-01", 1000, "backfill"),
    ("2024-01", 2, "mexico", "F1", "FAD", "2024-03-01", 1000, "backfill"),
    ("2024-01", 3, "mexico", "F1", "FAD", "2099-01-01", 1000, "backfill"),  # pendiente
    ("2024-01", 1, "china", "F4", "FAD", "2024-02-01", 800, "backfill"),  # sin escala → n_no_scale
    ("2024-02", 1, "india", "EB2", "DFF", "2024-03-01", 500, "live"),
]
# El sombra repite las MISMAS claves de pareo (menos la serie sin escala) con otros días.
_SHADOW_ROWS = [
    ("2024-01", 1, "mexico", "F1", "FAD", "2024-02-01", 1020, "backfill"),
    ("2024-01", 2, "mexico", "F1", "FAD", "2024-03-01", 1040, "backfill"),
    ("2024-02", 1, "india", "EB2", "DFF", "2024-03-01", 515, "live"),
]
_ACTUALS = {
    ("mexico", "F1", "FAD", "2024-02-01"): 1010.0,
    ("mexico", "F1", "FAD", "2024-03-01"): 1060.0,
    ("china", "F4", "FAD", "2024-02-01"): 830.0,
    ("india", "EB2", "DFF", "2024-03-01"): 520.0,
}


def _frame(rows: list[tuple], model_version: str, recipe: str | None = None) -> pd.DataFrame:
    out = []
    for origin, h, country, category, table, date, days, mode in rows:
        row = {
            "origin": origin,
            "h": h,
            "country": country,
            "category": category,
            "table": table,
            "date": date,
            "days": days,
            **_BANDS,
            "evaluation_mode": mode,
            "model_version": model_version,
        }
        if recipe is not None:
            row["recipe"] = recipe
        out.append(row)
    return pd.DataFrame(out)


def _fake_load_series(country: str, category: str, table: str):
    if country == "china":  # ejercita la rama sin escala naïve (n_no_scale)
        raise RuntimeError("sin serie para china/F4")
    return SimpleNamespace(name=f"{country}/{category}/{table}")


@pytest.fixture()
def scoring_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fixture controlado: ledgers sintéticos, escala fija y ``log_run`` capturado.

    Parchea ``vp_data.tracking.log_run`` (la fuente única de los records) para que el
    conteo valga igual en la rama base y en esta: ambas resuelven el atributo en el
    módulo al llamar."""
    prosp = tmp_path / "prospective"
    prosp.mkdir(parents=True)
    _frame(_CHAMP_ROWS, "champion-v1").to_csv(prosp / "forecast_log.csv", index=False)
    _frame(_SHADOW_ROWS, "shadow-v1", recipe="theta").to_csv(prosp / "forecast_log_shadow.csv", index=False)

    monkeypatch.setattr(sf, "REPORTS", tmp_path)
    monkeypatch.setattr(sf.dataset, "actuals_F", lambda: dict(_ACTUALS))
    monkeypatch.setattr(sf.dataset, "load_series", _fake_load_series)
    monkeypatch.setattr(sf.metrics, "naive_scale_before", lambda _s, _c: 100.0)

    marker = tmp_path / "tracking_marker.txt"
    monkeypatch.setenv(MARKER_ENV, str(marker))

    records: list[dict] = []

    def _capture(*args, **kwargs) -> dict:
        keys = ("experiment", "run_name", "params", "metrics")
        rec = dict(zip(keys, args, strict=False))
        rec.update(kwargs)
        records.append(rec)
        return rec

    monkeypatch.setattr(base_tracking, "log_run", _capture)
    return SimpleNamespace(root=tmp_path, prospective=prosp, marker=marker, records=records)


def _only_record(env) -> dict:
    assert len(env.records) == 1, f"se esperaba UN registro por invocación, hubo {len(env.records)}"
    return env.records[0]


def test_one_record_per_invocation_not_one_per_horizon(scoring_env) -> None:
    sf.run()
    rec = _only_record(scoring_env)
    assert rec["experiment"] == "web_forecast_scoring"


def test_record_carries_the_minimum_metric_contract(scoring_env) -> None:
    sf.run()
    m = _only_record(scoring_env)["metrics"]
    for key in ("n_scored", "n_pending", "n_no_scale", "n_pairs", "n_pairs_live"):
        assert key in m, key
    assert m["n_scored"] == 4 and m["n_pending"] == 1 and m["n_no_scale"] == 1
    assert m["n_pairs"] == 3 and m["n_pairs_live"] == 1
    for key in ("mae_days", "mase", "cov80", "cov95"):
        assert key in m, key


def test_zero_realized_targets_still_emits_one_record(scoring_env) -> None:
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sf.dataset, "actuals_F", dict)
    try:
        sf.run()
    finally:
        monkey.undo()
    m = _only_record(scoring_env)["metrics"]
    assert m["n_scored"] == 0 and m["n_pending"] == 5
    assert "mase" not in m, "sin filas puntuadas no se inventa un MASE"


def test_missing_ledger_still_emits_one_record(scoring_env) -> None:
    (scoring_env.prospective / "forecast_log.csv").unlink()
    assert sf.run() is None
    rec = _only_record(scoring_env)
    assert rec["metrics"]["n_scored"] == 0
    assert rec["telemetry"]["status"] == "ok"
    assert any("ledger" in w for w in rec["telemetry"]["warnings"])


def test_scoring_failure_is_recorded_as_failed_and_reraised(scoring_env) -> None:
    def _boom() -> dict:
        raise RuntimeError("actuals no disponibles")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sf.dataset, "actuals_F", _boom)
    try:
        with pytest.raises(RuntimeError, match="actuals no disponibles"):
            sf.run()
    finally:
        monkey.undo()
    tel = _only_record(scoring_env)["telemetry"]
    assert tel["status"] == "failed"
    assert tel["exception"]["type"] == "RuntimeError"
    assert tel["exception"]["message"] == "actuals no disponibles"


def test_primary_exception_survives_a_failing_log_run(scoring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict:
        raise RuntimeError("fallo primario del scoring")

    def _log_boom(*_a, **_k):
        raise OSError("staging JSONL no escribible")

    monkeypatch.setattr(sf.dataset, "actuals_F", _boom)
    monkeypatch.setattr(base_tracking, "log_run", _log_boom)
    with pytest.raises(RuntimeError, match="fallo primario del scoring"):
        sf.run()


def _marker_text(env) -> str:
    assert env.marker.exists(), "el marcador de resumen debe existir"
    return env.marker.read_text(encoding="utf-8")


def test_summary_marker_is_a_single_line_on_success(scoring_env) -> None:
    sf.run()
    text = _marker_text(scoring_env)
    assert text.count("\n") <= 1 and text.strip().count("\n") == 0
    line = text.strip()
    assert line.startswith("ok")
    for token in ("n_scored=4", "n_pending=1", "n_no_scale=1", "n_pairs=3", "n_pairs_live=1"):
        assert token in line, token
    assert "MASE" in line and "cob95" in line


def test_summary_marker_exists_and_is_one_line_on_failure(scoring_env) -> None:
    def _boom() -> dict:
        raise ValueError("linea1\nlinea2")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sf.dataset, "actuals_F", _boom)
    try:
        with pytest.raises(ValueError):
            sf.run()
    finally:
        monkey.undo()
    line = _marker_text(scoring_env).strip()
    assert "\n" not in line, "el marcador jamás puede ser multilínea"
    assert line.startswith("FALLO") and "ValueError" in line
    assert "n_scored=" in line


def test_summary_marker_is_derived_from_the_recorded_metrics(scoring_env) -> None:
    sf.run()
    m = _only_record(scoring_env)["metrics"]
    line = _marker_text(scoring_env).strip()
    assert f"n_scored={int(m['n_scored'])}" in line
    assert f"n_pairs_live={int(m['n_pairs_live'])}" in line


def test_marker_write_failure_never_breaks_a_successful_run(scoring_env, monkeypatch, capsys) -> None:
    """El marcador se escribe DESPUÉS del registro, así que su fallo ya no puede entrar en la
    telemetría: queda en stderr y el correo dirá n/d. Lo que no puede es romper la corrida."""
    monkeypatch.setenv(MARKER_ENV, str(scoring_env.root / "no-existe" / "marker.txt"))
    sf.run()
    assert _only_record(scoring_env)["telemetry"]["status"] == "ok"
    assert "marcador de resumen no escrito" in capsys.readouterr().err


def test_a_broken_summary_never_masks_the_primary_failure(scoring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict:
        raise RuntimeError("fallo primario del scoring")

    def _bad_line(*_a, **_k) -> str:
        raise ValueError("resumen roto")

    monkeypatch.setattr(sf.dataset, "actuals_F", _boom)
    monkeypatch.setattr(sf, "_summary_line", _bad_line)
    with pytest.raises(RuntimeError, match="fallo primario del scoring"):
        sf.run()


def test_artifacts_are_only_outputs_that_exist(scoring_env) -> None:
    sf.run()
    rec = _only_record(scoring_env)
    names = {Path(a).name for a in rec["artifacts"]}
    assert names == set(OUTPUTS)
    assert all(Path(a).exists() for a in rec["artifacts"])


def test_artifacts_skip_outputs_that_were_not_produced(scoring_env) -> None:
    (scoring_env.prospective / "forecast_log_shadow.csv").unlink()
    sf.run()
    rec = _only_record(scoring_env)
    names = {Path(a).name for a in rec["artifacts"]}
    assert names == {"forecast_scorecard.csv", "forecast_scorecard_meta.json"}
    assert rec["metrics"]["n_pairs"] == 0 and rec["metrics"]["n_pairs_live"] == 0


def test_telemetry_is_measured_not_hardcoded(scoring_env) -> None:
    sf.run()
    tel = _only_record(scoring_env)["telemetry"]
    assert set(tel) >= {"status", "duration_s", "rss_peak_mb", "gpu_mem_mb", "artifact_bytes", "warnings"}
    assert isinstance(tel["duration_s"], float) and tel["duration_s"] >= 0.0
    assert tel["rss_peak_mb"] is None or tel["rss_peak_mb"] > 0
    assert tel["artifact_bytes"] == sum((scoring_env.prospective / n).stat().st_size for n in OUTPUTS)
    # El script LEE la telemetría de `tracked.telemetry` para el marcador, pero no la fabrica:
    # medirla es competencia de vp_model.tracking. (Que los valores del marcador sean los
    # medidos lo prueba test_marker_carries_the_real_telemetry.)
    src = (ROOT / "experiments" / "score_forecasts.py").read_text(encoding="utf-8")
    assert "telemetry = {" not in src and "resource.getrusage" not in src
    assert "torch.cuda" not in src and "time.monotonic" not in src
    assert "tracked.telemetry" in src or "tel = tracked.telemetry" in src


def test_outputs_stay_byte_identical_to_the_baseline(scoring_env) -> None:
    sf.run()
    got = {name: hashlib.sha256((scoring_env.prospective / name).read_bytes()).hexdigest() for name in OUTPUTS}
    assert got == BASELINE_OUTPUT_SHA256


def test_head_to_head_json_still_has_no_instrumentation_fields(scoring_env) -> None:
    sf.run()
    h2h = json.loads((scoring_env.prospective / "prospective_head_to_head.json").read_text())
    assert h2h["n_pairs"] == 3
    assert "n_pairs_live" not in h2h, "n_pairs_live es telemetría; el artefacto publicado no cambia"


# --- M21-R1: cuatro defectos reales del primer intento -----------------------------


def test_marker_carries_the_real_telemetry(scoring_env) -> None:
    """RED 1: el marcador solo llevaba métricas; la telemetría medida no llegaba al correo."""
    sf.run()
    tel = _only_record(scoring_env)["telemetry"]
    line = _marker_text(scoring_env).strip()
    assert f"status={tel['status']}" in line
    assert f"{tel['duration_s']:.3f} s" in line
    assert ("RSS n/d" in line) if tel["rss_peak_mb"] is None else (f"RSS {tel['rss_peak_mb']} MB" in line)
    assert ("GPU n/a" in line) if tel["gpu_mem_mb"] is None else (f"GPU {tel['gpu_mem_mb']} MB" in line)
    assert f"artefactos {tel['artifact_bytes']} B" in line
    assert f"warnings={len(tel['warnings'])}" in line


def test_a_stale_output_from_a_previous_run_is_not_registered(scoring_env) -> None:
    """RED 2: sin ledger sombra esta corrida NO produce sus salidas; un archivo viejo en el
    disco no puede colarse como artefacto de esta invocación."""
    (scoring_env.prospective / "forecast_log_shadow.csv").unlink()
    stale = scoring_env.prospective / "forecast_scorecard_shadow.csv"
    stale.write_text("añada anterior\n", encoding="utf-8")
    (scoring_env.prospective / "prospective_head_to_head.json").write_text("{}\n", encoding="utf-8")
    sf.run()
    names = {Path(a).name for a in _only_record(scoring_env)["artifacts"]}
    assert names == {"forecast_scorecard.csv", "forecast_scorecard_meta.json"}
    assert stale.read_text(encoding="utf-8") == "añada anterior\n", "y tampoco se sobreescribe"


def test_a_failing_log_run_after_a_good_scoring_is_reported_as_failure(scoring_env, monkeypatch) -> None:
    """RED 3: el comando fallaba (track_run re-lanza) pero el marcador decía ok, porque se
    escribía ANTES de que el context-manager intentara registrar."""
    real = base_tracking.log_run
    captured: list[dict] = []

    def _log_then_fail(*args, **kwargs):
        captured.append(dict(zip(("experiment", "run_name", "params", "metrics"), args, strict=False)) | kwargs)
        raise OSError("staging JSONL no escribible")

    monkeypatch.setattr(base_tracking, "log_run", _log_then_fail)
    with pytest.raises(OSError, match="staging JSONL no escribible"):
        sf.run()
    assert real is not base_tracking.log_run
    assert len(captured) == 1, "un solo intento de registro por invocación"
    line = _marker_text(scoring_env).strip()
    assert line.startswith("FALLO") and "OSError" in line
    assert "status=failed" in line


def test_a_partial_run_registers_only_what_it_actually_wrote(scoring_env, monkeypatch) -> None:
    """RED 4: al fallar después de escribir salidas reales, el registro no las mencionaba."""

    def _boom(*_a, **_k) -> dict:
        raise RuntimeError("head-to-head roto")

    monkeypatch.setattr(sf, "_head_to_head", _boom)
    with pytest.raises(RuntimeError, match="head-to-head roto"):
        sf.run()
    rec = _only_record(scoring_env)
    names = {Path(a).name for a in rec["artifacts"]}
    assert names == {
        "forecast_scorecard.csv",
        "forecast_scorecard_meta.json",
        "forecast_scorecard_shadow.csv",
        "forecast_scorecard_shadow_meta.json",
    }
    assert not (scoring_env.prospective / "prospective_head_to_head.json").exists()
    assert rec["telemetry"]["artifact_bytes"] == sum((scoring_env.prospective / n).stat().st_size for n in names)


def test_marker_stays_one_line_and_json_safe_with_telemetry(scoring_env) -> None:
    sf.run()
    text = _marker_text(scoring_env)
    line = text.strip()
    assert "\n" not in line and len(line) <= 400
    assert '"' not in line and "\\" not in line


def test_tracking_stays_local_staging_without_remote_service() -> None:
    src = (ROOT / "experiments" / "score_forecasts.py").read_text(encoding="utf-8")
    assert "set_tracking_uri" not in src and "http://" not in src
    assert "mlflow.db" not in src and os.environ.get("MLFLOW_TRACKING_URI") in (None, "")


if __name__ == "__main__":
    test_demo_selfcheck()
    test_pending_when_target_not_realized()
    test_coverage_and_scaled_error()
    print("OK — test_forecast_scoring: 3/3")
