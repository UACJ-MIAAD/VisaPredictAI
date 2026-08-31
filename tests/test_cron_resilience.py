"""A3/A4/B5: el cron sobrevive a la fuente bloqueada sin mentir.

Guardián estructural de ``freeze_and_rebuild.yml`` y ``watchdog.yml`` (mismo
estilo que ``test_cron_app_auth.py``: sin ejecutar workflows, sin red, sin
issues reales) más el modo ``source`` de ``check_ingestion`` y el viaje del
feed en el allowlist de publicación. Reglas que fija cada test:

- ``skip_freeze`` omite EXCLUSIVAMENTE el acceso a la fuente: el sync S3, los
  gates, el CI del SHA exacto y el deploy corren idénticos;
- la señal de fuente bloqueada es honesta (línea SES derivada del estado D3,
  UN issue único que se cierra al recuperarse) y los fallos reales siguen
  fallando (Alert on failure intacto);
- watchdog sin estado registrado conserva el comportamiento previo;
- la gobernanza B0 no se toca (los tests de ``test_cron_app_auth`` siguen
  aplicando sobre el MISMO archivo: token App, gate de autoría, 4 pushes).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools import check_ingestion
from tools.cron_publish import ALLOWLIST

ROOT = Path(__file__).resolve().parents[1]
CRON = ROOT / ".github" / "workflows" / "freeze_and_rebuild.yml"
WATCHDOG = ROOT / ".github" / "workflows" / "watchdog.yml"
GH_SCRIPT_SHA = "ed597411d8f924073f98dfc5c65a23a2325f34cd"  # v8, pin ya vigente
STATE_REL = "reports/governance/ingestion_state.json"


def _cron() -> dict:
    return yaml.safe_load(CRON.read_text(encoding="utf-8"))


def _update_steps() -> list[dict]:
    return _cron()["jobs"]["update"]["steps"]


def _run_step() -> dict:
    return next(s for s in _update_steps() if s.get("id") == "run")


# ------------------------------------------------------------------ skip_freeze
def test_skip_freeze_input_is_boolean_and_defaults_to_false():
    on = _cron().get("on") or _cron().get(True)  # PyYAML parsea la clave `on` como True
    inp = on["workflow_dispatch"]["inputs"]["skip_freeze"]
    assert inp["type"] == "boolean" and inp["default"] is False


def test_skip_freeze_guards_only_the_source_access():
    run = _run_step()["run"]
    assert _run_step()["env"]["SKIP_FREEZE"] == "${{ inputs.skip_freeze }}"
    sync_pos = run.index("aws s3 sync s3://visapredictai-raw-snapshots/raw-html/ data/snapshots/")
    guard_pos = run.index('if [ "$SKIP_FREEZE" = "true" ]')
    freeze_pos = run.index("python -m pipeline.freeze_snapshots")
    assert sync_pos < guard_pos < freeze_pos, "el sync S3 debe correr SIEMPRE; solo el freeze se omite"
    # el guard pone new=0 y el freeze vive en el else
    guarded = run[guard_pos:freeze_pos]
    assert "new=0" in guarded and "else" in guarded


def test_skip_freeze_never_appears_in_any_step_condition():
    # gates, ciwait, release, deploy: sus `if:` no pueden depender de skip_freeze
    for step in _update_steps():
        assert "skip_freeze" not in str(step.get("if", "")), step.get("name")


# ------------------------------------------------------- estado de la fuente D3
def test_run_step_surfaces_the_source_status_output():
    run = _run_step()["run"]
    assert "check_ingestion.py --mode source" in run
    assert 'echo "source=' in run


def test_manual_snapshot_ingest_gets_an_honest_commit_message():
    run = _run_step()["run"]
    assert "from manual snapshot (source blocked)" in run
    assert '"$source_status" = "blocked"' in run
    assert "Self-heal: reingest panel through" in run  # la rama previa sobrevive


def test_state_feed_travels_in_the_data_publish_allowlist():
    assert STATE_REL in ALLOWLIST["data"]


def test_check_ingestion_source_mode_reads_state_or_absent(tmp_path, monkeypatch):
    import json
    from datetime import date

    from vp_data import ingestion_state as ist

    monkeypatch.setattr(check_ingestion, "STATE", tmp_path / "ingestion_state.json")
    assert check_ingestion.source_status() == "absent"
    blocked = ist.derive_state(
        status="blocked",
        reason="HTTP 403 tras el WAF",
        n_index_links=None,
        failed_links=[],
        panel_vintage="2026-07",
        today=date(2026, 8, 31),
        previous=None,
    )
    (tmp_path / "ingestion_state.json").write_text(json.dumps(blocked))
    assert check_ingestion.source_status() == "blocked"


def test_check_ingestion_source_mode_fails_closed_on_a_corrupt_feed(tmp_path, monkeypatch):
    # el cron consume este modo bajo set -e: un feed PRESENTE e inválido debe
    # tumbar la corrida (fallo real), jamás degradar a "absent"
    monkeypatch.setattr(check_ingestion, "STATE", tmp_path / "ingestion_state.json")
    (tmp_path / "ingestion_state.json").write_text("{truncado")
    with pytest.raises(ValueError):
        check_ingestion.source_status()


# ------------------------------------- publicación de transiciones sin rebuild
def test_state_change_publishes_even_without_a_rebuild_via_the_shared_push_site():
    run = _run_step()["run"]
    # UNA función compartida = el MISMO (y único) sitio de push de datos del paso
    assert "publish_data()" in run
    assert run.count("git push") == 1, "el paso run debe tener UN solo sitio de push (compartido)"
    # las TRES salidas sin rebuild (no-op, self-heal de modelado y freeze muerto)
    # publican el cambio de estado ANTES de su exit
    assert run.count('publish_data "Record source ingestion state: $source_status"') == 3
    noop_branch = run[: run.index("No new bulletin and panel up to date")]
    assert 'publish_data "Record source ingestion state' in noop_branch


def test_publish_data_is_defined_before_the_freeze_runs():
    # un freeze que muere (offline) debe poder publicar su estado: la función
    # tiene que existir ANTES de la invocación del freeze
    run = _run_step()["run"]
    assert run.index("publish_data()") < run.index("python -m pipeline.freeze_snapshots")


def test_a_dying_freeze_still_publishes_its_state_change_then_exits_red():
    # offline: _cli escribe el estado y relanza; el workflow captura el código,
    # publica el cambio (commit+gate+push por el sitio compartido) y devuelve el
    # fallo ORIGINAL — el job queda rojo, pero el feed viaja a main
    run = _run_step()["run"]
    assert "freeze_rc=" in run and 'exit "$freeze_rc"' in run
    fail_branch = run[run.index("freeze_rc=") : run.index('exit "$freeze_rc"')]
    assert "publish_data" in fail_branch
    assert "--mode source" in fail_branch  # el status del commit sale del feed, no se inventa


def test_ciwait_covers_the_state_commit_even_after_a_red_run():
    ciwait = next(s for s in _update_steps() if s.get("id") == "ciwait")
    cond = str(ciwait["if"])
    assert cond.startswith("always()") and "data_sha" in cond
    deploy = next(s for s in _update_steps() if "Netlify" in str(s.get("name", "")))
    assert "always()" not in str(deploy["if"]), "el deploy JAMÁS corre tras un run rojo"


def test_state_only_commit_sha_is_surfaced_and_waited_on_but_never_deployed():
    steps = _update_steps()
    run = _run_step()["run"]
    assert 'echo "data_sha=' in run  # el SHA del commit de datos/estado sale del paso
    ciwait = next(s for s in steps if s.get("id") == "ciwait")
    assert "data_sha" in str(ciwait["if"]), "un commit solo-de-estado también espera su CI"
    assert "data_sha" in ciwait["run"], "want debe caer al SHA del commit de estado si no hubo release"
    deploy = next(s for s in steps if "Netlify" in str(s.get("name", "")))
    assert "rebuilt == 'true'" in str(deploy["if"]) and "data_sha" not in str(deploy["if"]), (
        "sin rebuild JAMÁS hay deploy"
    )
    verify = next(s for s in steps if "deploy actually served" in str(s.get("name", "")))
    assert "rebuilt == 'true'" in str(verify["if"])


# ------------------------------------------------------------- señales honestas
def test_ses_body_carries_the_derived_source_line():
    ses = next(s for s in _update_steps() if "SES" in str(s.get("name", "")))
    assert "Fuente: $SOURCE_LINE" in ses["run"]
    assert STATE_REL in ses["run"]  # derivada del estado, jamás tipeada


def test_single_source_blocked_issue_step_opens_once_and_closes_on_recovery():
    steps = _update_steps()
    step = next(s for s in steps if "source-blocked" in str(s.get("name", "")).lower())
    assert step["uses"] == f"actions/github-script@{GH_SCRIPT_SHA}"
    script = step["with"]["script"]
    assert "existsSync" in script, "estado ausente debe ser no-op"
    assert "source-blocked" in script
    assert "open.length" in script, "issue único: jamás duplicar"
    assert "'closed'" in script and "ok" in script, "al recuperarse la fuente, el issue se cierra"


def test_real_failures_still_open_the_failure_issue():
    steps = _update_steps()
    alert = next(s for s in steps if str(s.get("name", "")).startswith("Alert on failure"))
    assert alert["if"] == "failure()"
    assert "scrape-failure" in alert["with"]["script"]


# ----------------------------------------------------------------- watchdog B5
def test_watchdog_reads_state_only_if_present_and_validates_before_consuming():
    wd = yaml.safe_load(WATCHDOG.read_text(encoding="utf-8"))
    steps = wd["jobs"]["check"]["steps"]
    state_step = next(s for s in steps if "ingesta" in str(s.get("name", "")).lower())
    script = state_step["with"]["script"]
    assert "existsSync" in script, "estado ausente = comportamiento previo"
    assert "core.notice" in script and "blocked" in script
    # feed PRESENTE e inválido = fallo real (única vía a setFailed); blocked = notice
    assert script.count("setFailed") == 1 and "CORRUPTO" in script
    assert "catch" in script and "schema" in script, "validar ANTES de consumir"
    # el chequeo legado (>4 días sin corrida verde) sobrevive intacto
    legacy = next(s for s in steps if "listWorkflowRuns" in str(s.get("with", {}).get("script", "")))
    assert "ageDays <= 4" in legacy["with"]["script"]


def test_cron_issue_step_validates_the_feed_before_consuming_it():
    step = next(s for s in _update_steps() if "source-blocked" in str(s.get("name", "")).lower())
    script = step["with"]["script"]
    assert "catch" in script and "schema" in script and "setFailed" in script


def test_watchdog_and_issue_steps_run_the_full_closed_schema_validation():
    # "schema y status" no basta: llaves extra, tipos incorrectos y formatos
    # inválidos también deben tumbar al consumidor (espejo del validate Python)
    wd = yaml.safe_load(WATCHDOG.read_text(encoding="utf-8"))
    wd_script = next(s for s in wd["jobs"]["check"]["steps"] if "ingesta" in str(s.get("name", "")).lower())["with"][
        "script"
    ]
    issue_script = next(s for s in _update_steps() if "source-blocked" in str(s.get("name", "")).lower())["with"][
        "script"
    ]
    for script in (wd_script, issue_script):
        assert "llave fuera del schema" in script  # set CERRADO de llaves
        assert "Number.isInteger" in script  # n_index_links: entero real
        assert "every" in script and "typeof" in script  # listas de strings, tipos
        assert "missing_months" in script and "failed_links" in script and "status_since" in script


def test_watchdog_state_notice_names_the_human_action():
    text = WATCHDOG.read_text(encoding="utf-8")
    assert "make ingest-manual" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
