"""RC-1: gate ESTRUCTURAL del temporal privado de CI. En Ubuntu el `/tmp` del runner es 01777 (world-writable) y las
fixtures de gobernanza bajo `tmp_path` fallan en B282 (GovernanceSnapshot rechaza un ancestro escribible por grupo/otros —
correcto, NO se relaja). `lint-and-test` y `model-tests` deben alojar el basetemp de pytest y sus artefactos temporales en
un directorio PRIVADO 0700 bajo $RUNNER_TEMP (fuera del checkout — dentro rompe tests que exigen tmp_path fuera de un repo git), con `TMPDIR` en `GITHUB_ENV`, `--basetemp` explícito, cleanup `if: always()`
y CERO rutas `/tmp/` sueltas en sus pasos. Este test falla sobre el SHA base `01df0c7` (que no prepara el temporal)."""

from __future__ import annotations

import pathlib

import pytest
import yaml

CI = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
JOBS = ("lint-and-test", "model-tests")


def _job_steps(job_name):
    doc = yaml.safe_load(CI.read_text())
    return doc["jobs"][job_name]["steps"]


def _runs(steps):
    return "\n".join(s["run"] for s in steps if isinstance(s, dict) and isinstance(s.get("run"), str))


@pytest.mark.parametrize("job", JOBS)
def test_private_temp_prepared_0700_with_tmpdir(job):
    steps = _job_steps(job)
    prep = [s for s in steps if isinstance(s, dict) and "Private CI temp" in str(s.get("name", ""))]
    assert len(prep) == 1, f"{job}: falta el paso 'Private CI temp' (RC-1)"
    run = prep[0]["run"]
    assert 'mktemp -d "$RUNNER_TEMP/.gov-' in run, (
        f"{job}: el temporal debe crearse bajo $RUNNER_TEMP (fuera del checkout)"
    )
    assert "chmod 0700" in run and 'install -d -m 0700 "$GOV_TMP/tmp"' in run, f"{job}: debe ser 0700"
    assert "TMPDIR=%s" in run and '>> "$GITHUB_ENV"' in run, f"{job}: TMPDIR debe exportarse a GITHUB_ENV"
    # regresión: NUNCA bajo el checkout — dentro del árbol git rompe tests que exigen tmp_path fuera de un repo
    # (test_sha_gate_no_git_repo / test_b19_receipt_outside_workspace / test_provenance_fallbacks).
    assert "$GITHUB_WORKSPACE" not in run, f"{job}: el temporal NO puede vivir dentro del checkout"


@pytest.mark.parametrize("job", JOBS)
def test_pytest_uses_private_basetemp_and_no_bare_tmp(job):
    steps = _job_steps(job)
    runs = _runs(steps)
    # cada invocación de pytest de estos jobs fija --basetemp bajo el temporal privado
    assert '--basetemp="$GOV_TMP/' in runs, f"{job}: pytest debe fijar --basetemp bajo el temporal privado"
    # cero rutas `/tmp/` sueltas (los artefactos temporales migraron a $GOV_TMP)
    for step in steps:
        run = step.get("run") if isinstance(step, dict) else None
        if isinstance(run, str):
            assert "/tmp/" not in run, f"{job}: ruta /tmp/ suelta en un paso (usar $GOV_TMP): {step.get('name')}"


@pytest.mark.parametrize("job", JOBS)
def test_cleanup_always(job):
    steps = _job_steps(job)
    cleanup = [s for s in steps if isinstance(s, dict) and "Cleanup private CI temp" in str(s.get("name", ""))]
    assert len(cleanup) == 1, f"{job}: falta el cleanup del temporal privado"
    assert cleanup[0].get("if") == "always()", f"{job}: el cleanup debe ser if: always()"
    assert 'rm -rf "$GOV_TMP"' in cleanup[0]["run"]


def test_lint_pytest_gate_uses_basetemp():
    # la puerta de cobertura de lint-and-test corre `pytest` con basetemp privado (no el /tmp por defecto de Linux).
    runs = _runs(_job_steps("lint-and-test"))
    assert 'pytest --basetemp="$GOV_TMP/pytest-lint"' in runs


def test_rc2_deep_lock_install_uses_a_private_venv():
    # RC-2: el stack deep se instala y ejecuta en un venv EFÍMERO PRIVADO 0700, NUNCA en el Python global del runner (cuyo
    # `lib` del toolcache es 0777/0775 → governed_import_identity rechaza toda identidad). Este test falla sobre `01df0c7`.
    steps = _job_steps("deep-lock-install")
    runs = _runs(steps)
    build = [s for s in steps if isinstance(s, dict) and "private deep venv" in str(s.get("name", "")).lower()]
    assert build, "falta el paso que construye el venv deep privado (RC-2)"
    b = build[0]["run"]
    assert 'python -m venv "$DEEP_ENV"' in b and "chmod 0700" in b, "el venv deep debe ser 0700"
    assert 'DEEP_ENV="$RUNNER_TEMP/.ci-deep-' in b, "el venv debe vivir bajo $RUNNER_TEMP (fuera del checkout)"
    assert "$GITHUB_WORKSPACE" not in b, "el venv deep NO puede vivir dentro del checkout"
    assert "export DEEP_ENV" in b, "DEEP_ENV debe EXPORTARSE (el preflight del MISMO paso lo lee de os.environ)"
    assert '"$DEEP_ENV/bin/python" tools/deep_env_preflight.py' in b, "debe correr el preflight con el python del venv"
    # install del lock + smoke + validador + negativo: SIEMPRE con el intérprete del venv
    assert '"$DEEP_ENV/bin/python" -m pip install --require-hashes -r' in runs, "el lock deep se instala en el venv"
    for tool in ("tools.deep_smoke", "tools.validate_deep_receipt"):
        assert f'"$DEEP_ENV/bin/python" -m {tool}' in runs, f"{tool} debe correr con el python del venv"
    # NUNCA una instalación/ejecución del stack deep con el `python` global
    assert (
        "\n          pip install --require-hashes -r" not in runs
        and "python -m tools.deep_smoke" not in runs.replace('"$DEEP_ENV/bin/python" -m tools.deep_smoke', "")
    ), "el stack deep no puede instalarse/ejecutarse con el python global del runner"
    cleanup = [s for s in steps if isinstance(s, dict) and "Cleanup private deep venv" in str(s.get("name", ""))]
    assert cleanup and cleanup[0].get("if") == "always()" and 'rm -rf "$DEEP_ENV"' in cleanup[0]["run"]


def test_rc2_preflight_fails_closed():
    # RC-2: el preflight es fail-closed — sin DEEP_ENV o con sys.prefix != DEEP_ENV (p.ej. el Python GLOBAL del runner)
    # sale != 0, así que el stack deep no puede correr fuera del venv privado.
    import os
    import subprocess
    import sys

    root = str(pathlib.Path(__file__).resolve().parent.parent)
    no_env = {k: val for k, val in os.environ.items() if k != "DEEP_ENV"}
    r = subprocess.run([sys.executable, "tools/deep_env_preflight.py"], capture_output=True, text=True, cwd=root, env=no_env)  # fmt: skip
    assert r.returncode == 1 and "DEEP_ENV no está" in r.stdout, r.stdout
    r2 = subprocess.run(
        [sys.executable, "tools/deep_env_preflight.py"],
        capture_output=True,
        text=True,
        cwd=root,
        env={**os.environ, "DEEP_ENV": "/nonexistent-prefix"},
    )
    assert r2.returncode == 1 and "sys.prefix" in r2.stdout, r2.stdout


def test_governed_read_under_world_writable_ancestor_still_rejected(tmp_path):
    # RC-1 (decisión): NO se relaja B282. Un árbol gobernado alojado bajo un ancestro 01777 (como el `/tmp` de Linux)
    # SIGUE rechazado — la solución es mover el basetemp a un 0700 privado, no aceptar el mundo-escribible.
    import os

    import tools.governance_snapshot as gs

    ww = tmp_path / "worldwritable"
    ww.mkdir()
    os.chmod(ww, 0o1777)  # simula /tmp 01777
    repo = ww / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "x.py").write_text("x\n")
    with pytest.raises(gs.GovernanceSnapshotError, match="escribible por grupo/otros"):
        with gs.GovernanceSnapshot(str(repo)) as snap:
            snap.read("tools/x.py")
