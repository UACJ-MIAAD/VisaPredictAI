"""RC-1: gate ESTRUCTURAL del temporal privado de CI. En Ubuntu el `/tmp` del runner es 01777 (world-writable) y las
fixtures de gobernanza bajo `tmp_path` fallan en B282 (GovernanceSnapshot rechaza un ancestro escribible por grupo/otros —
correcto, NO se relaja). `lint-and-test` y `model-tests` deben alojar el basetemp de pytest y sus artefactos temporales en
un directorio PRIVADO 0700 bajo el workspace, con `TMPDIR` en `GITHUB_ENV`, `--basetemp` explícito, cleanup `if: always()`
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
    assert 'mktemp -d "$GITHUB_WORKSPACE/.ci-private-' in run, f"{job}: el temporal debe crearse bajo el workspace"
    assert "chmod 0700" in run and 'install -d -m 0700 "$GOV_TMP/tmp"' in run, f"{job}: debe ser 0700"
    assert "TMPDIR=%s" in run and '>> "$GITHUB_ENV"' in run, f"{job}: TMPDIR debe exportarse a GITHUB_ENV"


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
