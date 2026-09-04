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

# ---------------------------------------------------------------------------
# D2-B: advisories visibles (marcadores estrictos, asunto, SES, issue único)
# ---------------------------------------------------------------------------
ADVISORY_SUMMARY = "/tmp/advisory_summary.json"
ADVISORY_STATE_REL = "reports/governance/advisory_state.json"


def _model_step() -> dict:
    """El bloque de modelado (id ``model``) es donde viven los dos advisories y su publish."""
    return next(s for s in _update_steps() if s.get("id") == "model")


def _model_text() -> str:
    return str(_model_step()["run"])


def test_both_advisories_stay_non_blocking_and_write_strict_markers():
    """Sus fallos no propagan (no hay `exit`/`set -e` que los eleve) y el marcador es exacto."""
    text = _model_text()
    for script, marker in (
        ("experiments/run_champion_challenger.py", "/tmp/champion_status.txt"),
        ("experiments/freeze_shadow.py", "/tmp/shadow_status.txt"),
    ):
        assert f"if python {script}; then" in text, f"{script} debe seguir siendo advisory (rama else, sin abortar)"
        assert f'echo -n "ok" > {marker}' in text
        assert f'echo -n "failed" > {marker}' in text
    # el bloque advisory jamás toca el gate de release ni el deploy
    idx = text.index("experiments/run_champion_challenger.py")
    block = text[idx : text.index("advisory_state.py")]
    assert "exit 1" not in block and "release gate" not in block


def test_advisory_state_is_updated_from_the_markers_and_is_not_blocking():
    text = _model_text()
    assert "python tools/advisory_state.py update" in text
    assert f"--state {ADVISORY_STATE_REL}" in text
    assert "--marker champion_challenger=/tmp/champion_status.txt" in text
    assert "--marker shadow_freeze=/tmp/shadow_status.txt" in text
    assert f"--out {ADVISORY_SUMMARY}" in text
    call = text[text.index("python tools/advisory_state.py") :]
    assert "::warning::" in call.split("\n\n")[0], "el fallo del estado debe degradar a warning, no bloquear"


def test_advisory_state_rides_the_model_publish_allowlist():
    """El estado pertenece a la fase model y ya viaja en su allowlist vigente (sin ampliarla)."""
    assert any(ADVISORY_STATE_REL.startswith(prefix) for prefix in ALLOWLIST["model"])
    text = _model_text()
    assert text.index("advisory_state.py") < text.index("--stage model"), (
        "el estado se actualiza antes del commit del bloque de modelado que lo publica"
    )


def _ses_step() -> dict:
    return next(s for s in _update_steps() if "SES" in s.get("name", ""))


def test_ses_subject_always_ends_with_the_derived_advisory_count():
    run = str(_ses_step()["run"])
    assert '[$ADVISORY_N advisories fallidos]"}' in run, "el asunto debe terminar en el sufijo con N"
    assert "ADVISORY_N=0" in run, "sin rebuild no corren advisories: N=0, sin fingir éxito"
    assert f"json.load(open('{ADVISORY_SUMMARY}'))['failed_count']" in run, "N sale del resumen de marcadores"
    assert "advisories fallidos" not in run.split("Subject")[0].replace("ADVISORY_N=0", ""), (
        "N no puede tipearse en texto libre"
    )


def test_ses_body_carries_both_advisory_statuses_and_the_prospective_gate():
    run = str(_ses_step()["run"])
    assert "Advisories: $ADVISORY_LINE" in run and "Gate prospectivo: n_pairs_live=$PAIRS_LINE" in run
    assert f"json.load(open('{ADVISORY_SUMMARY}'))['status_line']" in run
    assert f"json.load(open('{ADVISORY_SUMMARY}'))['n_pairs_live']" in run
    assert 'PAIRS_LINE="n/d"' in run, "ausente o inválido se muestra honestamente como n/d"
    assert "n_pairs_live=158" not in run, "el valor jamás se hardcodea"


# D7: la instrumentación mensual del scoring viaja al correo como una línea propia.
SCORING_SRC = ROOT / "experiments" / "score_forecasts.py"
TRACKING_ENV = "VP_SCORING_SUMMARY"


def _scoring_step() -> dict:
    return next(s for s in _update_steps() if "experiments/score_forecasts.py" in str(s.get("run", "")))


def _marker_path() -> str:
    """La ruta del marcador la fija el CRON (como los marcadores de advisories); aquí se lee
    de su propia orden para que el test no la re-tipee."""
    run = str(_scoring_step()["run"])
    line = next(ln for ln in run.splitlines() if "experiments/score_forecasts.py" in ln)
    assert line.strip().startswith(f"{TRACKING_ENV}="), "el cron pasa la ruta explícita al script"
    return line.strip().split("=", 1)[1].split()[0]


def test_ses_body_carries_the_monthly_tracking_line():
    run = str(_ses_step()["run"])
    marker = _marker_path()
    assert "Tracking mensual: $TRACKING_LINE" in run, "el resumen mensual tiene línea propia"
    assert f"[ -f {marker} ]" in run, "el correo lee EXACTAMENTE el marcador que el cron pidió escribir"
    assert f"head -n 1 {marker}" in run, "una sola línea: el correo nunca se parte"


def test_monthly_tracking_line_is_honest_without_a_marker():
    run = str(_ses_step()["run"])
    assert 'TRACKING_LINE="n/d (sin rebuild este run)"' in run
    for invented in ("n_scored=", "n_pairs_live=1", "MASE "):
        assert invented not in run.split("Subject")[0], f"{invented} jamás se tipea en el cron"


def test_tracking_marker_env_is_single_sourced_with_the_scoring_script():
    # Sin importar score_forecasts: este test corre en el job base, que no trae el extra `model`.
    src = SCORING_SRC.read_text(encoding="utf-8")
    assert f'SUMMARY_ENV = "{TRACKING_ENV}"' in src, "el cron y el script nombran la MISMA variable"
    assert "os.environ.get(SUMMARY_ENV" in src, "el script honra la ruta que le pasa el invocador"
    assert _marker_path().endswith(".txt")


def test_scoring_step_stays_blocking_so_a_failure_is_not_swallowed():
    step = _scoring_step()
    run = str(step["run"])
    assert "python experiments/score_forecasts.py\n" in run
    assert "score_forecasts.py || true" not in run, "un fallo del scoring debe seguir tumbando el paso"
    assert step.get("continue-on-error") is not True


def _advisory_issue_step() -> dict:
    return next(s for s in _update_steps() if "Advisory issue" in s.get("name", ""))


def test_advisory_issue_is_single_labelled_and_never_blocking():
    step = _advisory_issue_step()
    assert step.get("continue-on-error") is True and step.get("if") == "always()"
    assert step["uses"].endswith(GH_SCRIPT_SHA)
    script = step["with"]["script"]
    assert "labels: label" in script and "mlops-advisory" in script
    assert "issue_action" in script and "'open'" in script and "'close'" in script
    assert "ya abierto: no se duplica" in script, "corridas posteriores no duplican ni comentan a diario"
    assert "issues.update" in script and "state: 'closed'" in script, "la recuperación comenta una vez y cierra"
    assert "if (!fs.existsSync(path))" in script, "sin resumen (sin rebuild) no hace nada"


def test_failure_issue_and_source_blocked_issue_stay_separate_from_the_advisory_one():
    names = [s.get("name", "") for s in _update_steps()]
    assert sum("Advisory issue" in n for n in names) == 1
    assert any("Alert on failure" in n for n in names), "el issue de fallo real sigue existiendo"
    text = CRON.read_text(encoding="utf-8")
    assert "scrape-failure" in text and "mlops-advisory" in text and "source-blocked" in text


def test_advisory_label_is_ensured_before_the_issue_is_created():
    """M8-R1: la etiqueta no existía en el repo; crearla es parte del camino de apertura.

    Sin esto, `issues.create` con una etiqueta inexistente puede fallar y `continue-on-error`
    convertiría la alerta en un silencio.
    """
    script = _advisory_issue_step()["with"]["script"]
    get_label = script.index("issues.getLabel")
    create_label = script.index("issues.createLabel")
    create_issue = script.index("issues.create({")
    assert get_label < create_issue and create_label < create_issue, (
        "la etiqueta se comprueba y se crea ANTES de abrir el issue"
    )
    assert "e.status !== 404" in script, "solo la ausencia (404) dispara la creación"
    assert "e2.status !== 422" in script, "'ya existe' (422) es idempotente"
    assert "throw e" in script and "throw e2" in script, "cualquier otro error se propaga al paso"
    assert "color: 'B60205'" in script and "description:" in script, "color y descripción fijos"


def test_advisory_label_name_is_identical_everywhere():
    """Un solo nombre exacto para listar, crear y asignar."""
    script = _advisory_issue_step()["with"]["script"]
    assert script.count("const label = 'mlops-advisory';") == 1
    assert "labels: label," in script  # listForRepo
    assert "name: label" in script  # getLabel y createLabel
    assert "labels: [label]," in script  # issues.create
    assert "'mlops-advisory'" in script and script.count("mlops-advisory") == 1, (
        "el nombre vive en una sola constante, sin variantes tipeadas"
    )


def test_advisory_issue_step_stays_non_blocking_after_the_label_fix():
    step = _advisory_issue_step()
    assert step.get("continue-on-error") is True and step.get("if") == "always()"


def test_first_failure_opens_nothing_and_the_second_reaches_the_label_and_issue_path():
    """La transición que dispara el camino de etiqueta+issue es el SEGUNDO fallo consecutivo."""
    from tools import advisory_state as adv

    first = adv.next_state(None, {"champion_challenger": "failed", "shadow_freeze": "ok"}, "r1")
    assert adv.issue_action(first, None) == "none"
    second = adv.next_state(first, {"champion_challenger": "failed", "shadow_freeze": "ok"}, "r2")
    assert adv.issue_action(second, first) == "open"
    script = _advisory_issue_step()["with"]["script"]
    assert "if (action !== 'open' && action !== 'close')" in script, "solo open/close actúan"
