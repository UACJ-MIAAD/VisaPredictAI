"""The writer's one-hour credential never bounds the read-only CI wait."""

import subprocess
from pathlib import Path

import yaml

CRON = Path(__file__).resolve().parents[1] / ".github/workflows/freeze_and_rebuild.yml"


def workflow():
    return yaml.safe_load(CRON.read_text())


def wait_step():
    return next(step for job in workflow()["jobs"].values() for step in job["steps"] if step.get("id") == "ciwait")


def test_a_50_minute_ci_is_not_rejected_by_the_wait_budget(tmp_path):
    # Execute the real shell with a virtual 50-minute CI (no wall-clock sleep).
    # The previous 2100-second timeout returns 124; the current bounded wait passes.
    script = wait_step()["run"]
    import re

    script = re.sub(r"\$\{\{[^}]+\}\}", "a" * 40, script).replace("/tmp/", str(tmp_path) + "/")
    shell = """
    gh() { if [ "$2" = "list" ]; then echo 42; else return 0; fi; }
    timeout() { budget=$1; shift; [ "$budget" -ge 3000 ] || return 124; "$@"; }
    """
    result = subprocess.run(["bash", "-c", shell + script], capture_output=True, text=True, errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CI VERDE" in (tmp_path / "ciwait.txt").read_text()


def test_writer_still_finishes_before_its_installation_token_expires():
    assert 0 < workflow()["jobs"]["update"]["timeout-minutes"] < 60


def test_delivery_has_no_writer_credentials_or_checkout():
    job = workflow()["jobs"]["delivery"]
    assert job["needs"] == "update"
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert job["timeout-minutes"] == 90
    assert not any("uses" in step for step in job["steps"])
    serialized = str(job)
    assert "cron-app-token" not in serialized and "git push" not in serialized
    assert "GH_REPO" in job["env"] and "GH_TOKEN" in job["env"]


def test_delivery_is_pinned_to_writer_outputs_and_never_deploys_a_red_or_state_only_run():
    jobs = workflow()["jobs"]
    outputs = jobs["update"]["outputs"]
    assert outputs["release_sha"] == "${{ steps.release.outputs.sha }}"
    assert outputs["release_id"] == "${{ steps.release.outputs.release_id }}"
    job = jobs["delivery"]
    assert "always()" in job["if"] and "data_sha" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    steps = job["steps"]
    deploy = next(step for step in steps if "Netlify build hook" in step.get("name", ""))
    verify = next(step for step in steps if "deploy actually served" in step.get("name", ""))
    for step in [deploy, verify]:
        condition = step["if"]
        for required in [
            "needs.update.result == 'success'",
            "rebuilt == 'true'",
            "release_outcome == 'success'",
            "steps.ciwait.outcome == 'success'",
        ]:
            assert required in condition
    assert "needs.update.outputs.release_sha" in wait_step()["run"]
    assert "needs.update.outputs.release_id" in verify["run"]
    assert "release_manifest.json" not in verify["run"]  # no checkout can substitute the reviewed output
    assert not verify.get("continue-on-error", False)


def test_ci_failure_never_becomes_a_successful_wait(tmp_path):
    import re

    script = re.sub(r"\$\{\{[^}]+\}\}", "a" * 40, wait_step()["run"]).replace("/tmp/", str(tmp_path) + "/")
    shell = 'gh() { if [ "$2" = "list" ]; then echo 42; else return 1; fi; }; timeout() { shift; "$@"; };\n'
    result = subprocess.run(["bash", "-c", shell + script], capture_output=True, text=True, errors="replace")
    assert result.returncode != 0
    assert "SIN deploy" in (tmp_path / "ciwait.txt").read_text()
